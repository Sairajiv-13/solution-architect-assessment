"""Atomic on-disk admissions. Active reservations have no time-based eviction."""

import asyncio
import sqlite3
import time
import uuid
from collections.abc import Callable
from contextlib import closing
from pathlib import Path

import anyio

MAX_SQLITE_INTEGER = 2**63 - 1


class QuotaExceeded(Exception):
    pass


async def join_worker[T](task: asyncio.Task[T]) -> T:
    # A second Task.cancel() must not cancel a task whose thread may still commit.
    while not task.done():
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()


class Quota:
    def __init__(
        self,
        path: Path,
        limit: int = 50_000,
        window: float = 60,
        clock: Callable[[], float] = time.time,
    ):
        if str(path) == ":memory:" or not 0 < limit <= MAX_SQLITE_INTEGER or window <= 0:
            raise ValueError(
                "An on-disk path, representable budget and positive window are required"
            )
        self.path, self.limit, self.window, self.clock = path, limit, window, clock

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.execute("PRAGMA busy_timeout=5000")
        return db

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db:
            deadline = time.monotonic() + 5
            while True:
                try:
                    mode = db.execute("PRAGMA journal_mode=WAL").fetchone()[0]
                    if mode != "wal":
                        raise RuntimeError("SQLite did not enable WAL mode")
                    break
                except sqlite3.OperationalError as exc:
                    # Changing journal mode can fail immediately during simultaneous startup.
                    code = getattr(exc, "sqlite_errorcode", 0) & 0xFF
                    remaining = deadline - time.monotonic()
                    if code not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) or remaining <= 0:
                        raise
                    time.sleep(min(0.025, remaining))
            db.execute("""CREATE TABLE IF NOT EXISTS reservations (
                id TEXT PRIMARY KEY, tenant TEXT NOT NULL, amount INTEGER NOT NULL CHECK(amount>=0),
                finished REAL
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS tenant_window ON reservations(tenant, finished)")
            db.execute(
                "CREATE INDEX IF NOT EXISTS reservation_expiry "
                "ON reservations(finished) WHERE finished IS NOT NULL"
            )

    def reserve_sync(self, tenant: str, amount: int) -> str:
        if amount <= 0:
            raise ValueError("Reservation must be positive")
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            cutoff = self.clock() - self.window
            db.execute(
                "DELETE FROM reservations WHERE finished IS NOT NULL AND finished<=?", (cutoff,)
            )
            remaining = self.limit - amount
            if remaining < 0:
                raise QuotaExceeded()
            # Several individually representable overrun charges can overflow SQLite SUM.
            for (charge,) in db.execute(
                "SELECT amount FROM reservations WHERE tenant=?", (tenant,)
            ):
                remaining -= charge
                if remaining < 0:
                    raise QuotaExceeded()
            ticket = uuid.uuid4().hex
            db.execute("INSERT INTO reservations VALUES (?,?,?,NULL)", (ticket, tenant, amount))
            db.commit()
            return ticket
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def finish_sync(self, ticket: str, actual: int) -> None:
        if actual < 0:
            raise ValueError("Negative usage")
        with closing(self._connect()) as db, db:
            # Take the lock before reading the clock so contention cannot shorten the window.
            db.execute("BEGIN IMMEDIATE")
            # First settlement wins, making cleanup safe to retry.
            db.execute(
                "UPDATE reservations SET amount=?, finished=? WHERE id=? AND finished IS NULL",
                # A larger charge has the same effect on every representable budget.
                (min(actual, MAX_SQLITE_INTEGER), self.clock(), ticket),
            )

    async def reserve(self, tenant: str, amount: int) -> str:
        task = asyncio.create_task(asyncio.to_thread(self.reserve_sync, tenant, amount))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # Cancellation cannot roll back a worker thread that may already have committed.
            with anyio.CancelScope(shield=True):
                try:
                    ticket = await join_worker(task)
                except QuotaExceeded:
                    raise asyncio.CancelledError() from None
                await self.finish(ticket, amount)
            raise

    async def finish(self, ticket: str, actual: int) -> None:
        with anyio.CancelScope(shield=True):
            task = asyncio.create_task(asyncio.to_thread(self.finish_sync, ticket, actual))
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                await join_worker(task)
                raise

    def recover_offline(self) -> int:
        """Only run with ALL gateway workers stopped; retain full abandoned charges."""
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            cursor = db.execute(
                "UPDATE reservations SET finished=? WHERE finished IS NULL", (self.clock(),)
            )
            return cursor.rowcount


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Offline recovery; stop ALL gateway workers first")
    parser.add_argument("path", type=Path)
    parser.add_argument("--workers-stopped", action="store_true", required=True)
    args = parser.parse_args()
    quota = Quota(args.path)
    quota.initialize()
    print(
        f"Settled {quota.recover_offline()} abandoned reservations at their full reserved amounts"
    )
