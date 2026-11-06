# Verification record

Executed November 5, 2026 on Windows x64 with CPython 3.12.14 and `uv` 0.13.7. The local host used the bundled Python interpreter (`UV_PYTHON`) and kept the virtual environment, temporary files, and tool caches in the workspace's `work` directory. Those host overrides are not required on a normal Python installation.

| Command | Observed outcome |
| --- | --- |
| `uv lock --offline` | Resolved 50 packages and updated the project version to 1.0.1. |
| `uv lock --check --offline` | Confirmed the updated lockfile against the cache. |
| `ruff check src tests` | All checks passed in the locked environment. |
| `ruff format --check src tests` | All 19 Python files already formatted. |
| `mypy` | No issues in 19 source and test files. |
| `pytest -q -p no:cacheprovider` | **145 passed**. Starlette reports a dependency deprecation warning for its AnyIO `BlockingPortal` alias. |
| `python -u -m assessment.demo` | Exited 0. Exercised the stdio handshake and both tools, JSON-RPC `-32602` and `-32001` errors, transparent tool listing, primary success, 429 fallback, 3-second timeout fallback, sanitized backup failure, and all three redactions. |
| `git diff --check` | Passed on the review changes. |

The offline suite uses real stdio subprocesses and an on-disk SQLite database, including independent processes contending for the same quota. HTTP failures use deterministic in-process transports. The demo exercises actual HTTP connections on loopback. [Hosted Linux and Windows runs](https://github.com/Sairajiv-13/solution-architect-assessment/actions)

The clean ZIP preflight installed the locked dependencies with `uv sync --locked --offline`. The hosted workflow runs the same project checks on Linux and Windows.

## Review regressions

| Reproduce with | Evidence |
| --- | --- |
| `pytest -q tests/test_mcp_stdio.py tests/test_gateway.py` | Invalid progress metadata and null parameters return `-32602` with no downstream call. An unpaired surrogate returns `-32700` over stdio. The MCP deadline returns HTTP 504 with the request ID and closes the response. |
| `pytest -q tests/test_pii.py` | Every split of `1 123@example.com!` produces `1 [REDACTED]!`. Tests cover overlapping matches, oversized candidates, and empty or ambiguous SSE events. |
| `pytest -q tests/test_quota.py` | Independent processes admit exactly ten reservations of ten tokens under a 100-token cap. A second connection holds the write lock while the test clock moves. Both settlement and offline recovery keep the resulting charge for the full window. |
| `pytest -q tests/test_gateway.py tests/test_router.py` | ASGI disconnects cancel stalled work and settle started generation attempts. Compressed upstream bodies are rejected before read. Buffered provider output that arrives past the deadline cannot reach the client. |

The compact SSN tests initially failed on v1.0.0. In the HTTP test, a synthetic nine-digit value arrived in two provider deltas and reached the client in plain text. The same test on v1.0.1 emits one redaction marker in its place. The offline quota recovery command settled one test reservation, then reported zero on a repeat run against the same database.

A loopback MCP upstream sent four bytes every 45 ms. In the review build, a 200 ms per-read timeout allowed HTTP 200 after 578 ms. With a 200 ms overall deadline, the same probe returned HTTP 504 and JSON-RPC `-32002` after 219 ms.

## Local streaming measurement

The demo timed seven requests from immediately before sending the HTTP request to parsing the first SSE text delta at the client. These are end-to-end **time to first safe output** samples in milliseconds:

```text
17.794  16.653  17.571  17.924  17.333  25.803  16.361
```

With the clinical bridge enabled and `cl100k_base` loaded from the verified cache, the median client time to first safe output was **17.571 ms**. The median direct mock time to its first text delta was **5.879 ms**, measured in separate requests. These are loopback measurements with synthetic text. The 429 fallback completed in **34.931 ms** and the 3,000 ms timeout fallback completed in **3,040.978 ms**, one request per scenario.

`uv run python -m assessment.benchmark` reproduced these scanner and quota checks.

| Check | Observed result |
| --- | --- |
| Nine synthetic positive cases at every split | 182 of 182 redacted as expected |
| Five negative controls at every split | 140 of 140 unchanged |
| 65,712 streamed text bytes | 48 redactions, 99,889 peak traced Python bytes |
| 2,102,784 streamed text bytes | 1,536 redactions, 92,178 peak traced Python bytes |
| SQLite boundary | Token 50,000 admitted, token 50,001 denied, full budget restored at 60 seconds |

The memory probe consumes events without collecting output and measures Python allocations with `tracemalloc`. The unit tests assert the 256-character scanner limit separately. The first safe output test holds the mock's second delta behind an event and checks that the first delta arrives before releasing it.

## Submission review

The submission check scanned the staged files for common credential signatures. The demonstration bearer values are synthetic. The ZIP is generated from the local submission commit on `main`. [Submission instructions](README.md#submission)
