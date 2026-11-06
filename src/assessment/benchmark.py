"""Reproduce scanner coverage, retained memory, and the exact token boundary."""

import asyncio
import json
import tracemalloc
from collections.abc import AsyncIterator
from pathlib import Path
from tempfile import TemporaryDirectory

from assessment.pii import REDACTED, PIIScanner
from assessment.provider import sse_deltas
from assessment.quota import Quota, QuotaExceeded

POSITIVES = (
    "demo@example.com",
    "first.last+tag@sub.example.org",
    "123@example.com",
    "123-45-6789",
    "123 45 6789",
    "4111111111111111",
    "4111 1111 1111 1111",
    "4111-1111-1111-1111",
)
CONTROLS = (
    "Order 4111111111111112",
    "Order 4111-1111-1111-1112",
    "Reference 12345678901234567890123456",
    "Study NCT06001122",
)


def corpus() -> dict[str, int]:
    results: dict[str, int] = {}
    for name, cases in (("positive", POSITIVES), ("control", CONTROLS)):
        correct = attempts = 0
        for case in cases:
            original = f"({case})!"
            expected = f"({REDACTED})!" if name == "positive" else original
            for split in range(len(original) + 1):
                scanner = PIIScanner()
                output = scanner.feed(original[:split]) + scanner.feed(original[split:])
                correct += output + scanner.finish() == expected
                attempts += 1
        results[f"{name}_passed"] = correct
        results[f"{name}_splits"] = attempts
        assert correct == attempts
    return results


async def memory(frames: int) -> dict[str, int]:
    text = "ordinary " * 450 + "demo@example.com; 123 45 6789; 4111 1111 1111 1111! carry"
    wire = f"data: {json.dumps({'delta': text})}\n\n".encode()

    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(frames):
            yield wire[:311]
            yield wire[311:]
        yield b"data: [DONE]\n\n"

    scanner = PIIScanner()
    high_water = markers = 0
    tracemalloc.start()
    try:
        async for event in sse_deltas(chunks()):
            markers += scanner.feed(event.delta).count(REDACTED)
            high_water = max(high_water, len(scanner.pending))
        markers += scanner.finish().count(REDACTED)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert markers == 3 * frames and high_water <= scanner.limit
    return {
        "text_bytes": len(text.encode()) * frames,
        "max_pending": high_water,
        "peak_python_bytes": peak,
        "redactions": markers,
    }


def quota_boundary(path: Path) -> dict[str, int | bool]:
    now = 1000.0
    quota = Quota(path, clock=lambda: now)
    quota.initialize()
    for amount in (49_999, 1):
        quota.finish_sync(quota.reserve_sync("bench", amount), amount)
    try:
        quota.reserve_sync("bench", 1)
    except QuotaExceeded:
        pass
    else:
        raise AssertionError("Token 50001 was admitted")
    now += 60
    assert quota.reserve_sync("bench", 50_000)
    return {"limit": 50_000, "token_50001_denied": True, "full_budget_after_60s": True}


async def main() -> None:
    print(json.dumps({"corpus": corpus()}))
    print(json.dumps({"stream": [await memory(16), await memory(512)]}))
    with TemporaryDirectory(prefix="assessment-bench-") as directory:
        print(json.dumps({"quota": quota_boundary(Path(directory) / "quota.db")}))


if __name__ == "__main__":
    asyncio.run(main())
