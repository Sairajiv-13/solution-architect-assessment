# Verification record

Executed November 5, 2026 on Windows x64 with CPython 3.12.14 and `uv` 0.13.7. The local host used the bundled Python interpreter (`UV_PYTHON`) and kept the virtual environment, temporary files, and tool caches in the workspace's `work` directory. Those host overrides are not required on a normal Python installation.

| Command | Observed outcome |
| --- | --- |
| `uv lock` | Resolved 45 packages and wrote the lockfile. |
| `uv sync --locked` | Created a fresh environment and installed 45 packages from the lock. |
| `uv lock --check --offline` | Resolved the same 45 packages from the existing lock and cache. |
| `ruff check src tests` | All checks passed in the locked environment. |
| `ruff format --check src tests` | All 17 Python files already formatted. |
| `mypy` and `mypy --platform linux` | No issues in 17 source and test files. |
| `pytest -q -p no:cacheprovider` | **133 passed in 5.75 s** after the final review. Starlette reported one dependency deprecation warning for its AnyIO `BlockingPortal` alias. |
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

A loopback MCP upstream sent four bytes every 45 ms. In the review build, a 200 ms per-read timeout allowed HTTP 200 after 578 ms. With a 200 ms overall deadline, the same probe returned HTTP 504 and JSON-RPC `-32002` after 219 ms.

## Local streaming measurement

The demo timed seven requests from immediately before sending the HTTP request to parsing the first SSE text delta at the client. These are end-to-end **time to first safe output** samples in milliseconds:

```text
30.827  15.437  16.892  15.593  15.623  14.321  14.621
```

Minimum **14.321 ms**, median **15.593 ms**, maximum **30.827 ms**. This interval includes loopback HTTP, quota admission, provider framing, and scanning. Upstream time to first token measures a separate interval at the provider. Run `uv run python -m assessment.demo` to repeat the procedure.

## Submission review

The submission check scanned the staged files for common credential signatures. The demonstration bearer values are synthetic. The ZIP is generated from the local submission commit on `main`. [Submission instructions](README.md#submission)
