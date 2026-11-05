import asyncio
import multiprocessing
import sqlite3
import threading
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

import pytest

from assessment.quota import Quota, QuotaExceeded


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.mark.parametrize("recover", [False, True])
def test_settlement_samples_clock_after_writer_lock(tmp_path: Path, recover: bool) -> None:
    clock = Clock()
    waiting = threading.Event()
    main_thread = threading.current_thread()

    class ObservedQuota(Quota):
        def _connect(self) -> sqlite3.Connection:
            db = super()._connect()
            if threading.current_thread() is not main_thread:

                def observe(statement: str) -> None:
                    if statement.startswith(("BEGIN IMMEDIATE", "UPDATE reservations")):
                        waiting.set()

                db.set_trace_callback(observe)
            return db

    quota = ObservedQuota(tmp_path / "quota.db", limit=100, window=2, clock=clock)
    quota.initialize()
    ticket = quota.reserve_sync("tenant", 70)

    def settle() -> None:
        if recover:
            quota.recover_offline()
        else:
            quota.finish_sync(ticket, 70)

    with closing(sqlite3.connect(quota.path, isolation_level=None)) as blocker:
        with ThreadPoolExecutor(max_workers=1) as pool:
            blocker.execute("BEGIN IMMEDIATE")
            try:
                pending = pool.submit(settle)
                assert waiting.wait(3)
                clock.now += 3
            finally:
                blocker.commit()
            pending.result(5)
    assert rows(quota.path) == [(70, clock.now)]
    with pytest.raises(QuotaExceeded):
        quota.reserve_sync("tenant", 100)
    clock.now += 2
    quota.reserve_sync("tenant", 100)


def admitted(path: str) -> bool:
    quota = Quota(Path(path), limit=100)
    try:
        quota.reserve_sync("tenant-a", 10)
        return True
    except QuotaExceeded:
        return False


def test_workers_can_initialize_fresh_database_together(tmp_path: Path) -> None:
    path = tmp_path / "quota.sqlite3"
    gate = threading.Barrier(12, timeout=5)

    def initialize(_: int) -> None:
        gate.wait()
        Quota(path).initialize()

    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(initialize, range(12)))
    assert Quota(path).reserve_sync("tenant", 10)


@pytest.mark.parametrize("processes", [False, True])
def test_atomic_admission_across_independent_connections(tmp_path: Path, processes: bool) -> None:
    path = tmp_path / "quota.sqlite3"
    Quota(path, limit=100).initialize()
    if processes:
        with ProcessPoolExecutor(
            max_workers=3, mp_context=multiprocessing.get_context("spawn")
        ) as pool:
            results = list(pool.map(admitted, [str(path)] * 24))
    else:
        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(admitted, [str(path)] * 24))
    assert sum(results) == 10


def rows(path: Path) -> list[tuple[int, float | None]]:
    with sqlite3.connect(path) as db:
        return db.execute("SELECT amount,finished FROM reservations ORDER BY rowid").fetchall()


def test_window_reconciliation_tenant_isolation_and_restart(tmp_path: Path) -> None:
    clock = Clock()
    path = tmp_path / "quota.sqlite3"
    quota = Quota(path, limit=100, clock=clock)
    quota.initialize()
    first = quota.reserve_sync("a", 80)
    quota.reserve_sync("b", 100)
    with pytest.raises(QuotaExceeded):
        quota.reserve_sync("a", 30)
    quota.finish_sync(first, 20)
    quota.finish_sync(first, 0)  # Settlement is idempotent.
    assert rows(path)[0][0] == 20
    quota.reserve_sync("a", 80)
    clock.now += 600
    # Active reservations remain charged across windows and restarts.
    restarted = Quota(path, limit=100, clock=clock)
    with pytest.raises(QuotaExceeded):
        restarted.reserve_sync("a", 30)
    restarted.reserve_sync("a", 20)
    assert len(rows(path)) == 3  # Only the settled, expired charge was evicted.
    assert restarted.recover_offline() == 3
    with pytest.raises(QuotaExceeded):
        restarted.reserve_sync("a", 1)
    clock.now += 59.999
    with pytest.raises(QuotaExceeded):
        restarted.reserve_sync("a", 1)
    clock.now += 0.001
    restarted.reserve_sync("a", 100)


def test_overrun_is_recorded_as_debt(tmp_path: Path) -> None:
    quota = Quota(tmp_path / "quota.sqlite3", limit=100)
    quota.initialize()
    ticket = quota.reserve_sync("tenant", 50)
    quota.finish_sync(ticket, 150)
    with pytest.raises(QuotaExceeded):
        quota.reserve_sync("tenant", 1)


def test_overrun_settles_and_remains_enforced_beyond_sqlite_integer_range(tmp_path: Path) -> None:
    clock = Clock()
    quota = Quota(tmp_path / "quota.sqlite3", limit=100, clock=clock)
    quota.initialize()
    tickets = [quota.reserve_sync("tenant", 10) for _ in range(2)]
    for ticket in tickets:
        quota.finish_sync(ticket, 2**63)
    assert rows(quota.path) == [(2**63 - 1, clock.now)] * 2
    with pytest.raises(QuotaExceeded):
        quota.reserve_sync("tenant", 1)
    clock.now += 60
    quota.reserve_sync("tenant", 100)


@pytest.mark.parametrize("repeated", [False, True])
async def test_cancel_during_admission_does_not_leave_unaccounted_commit(
    tmp_path: Path, repeated: bool
) -> None:
    entered, release = threading.Event(), threading.Event()

    class BlockedQuota(Quota):
        def reserve_sync(self, tenant: str, amount: int) -> str:
            entered.set()
            assert release.wait(timeout=5)
            return super().reserve_sync(tenant, amount)

    quota = BlockedQuota(tmp_path / "quota.sqlite3")
    quota.initialize()
    task = asyncio.create_task(quota.reserve("tenant", 50))
    assert await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    if repeated:
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert rows(quota.path)[0][0] == 50
    assert rows(quota.path)[0][1] is not None


@pytest.mark.parametrize("path", [Path(":memory:")])
def test_database_must_be_on_disk(path: Path) -> None:
    with pytest.raises(ValueError):
        Quota(path)
