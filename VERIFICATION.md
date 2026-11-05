# Verification record

Executed November 5, 2026 on Windows x64 with CPython 3.12.14 and `uv` 0.13.7. The local host used the bundled Python interpreter (`UV_PYTHON`) and kept the virtual environment, temporary files, and tool caches in the workspace's `work` directory. Those host overrides are not required on a normal Python installation.

| Command | Observed outcome |
| --- | --- |
| `uv lock` | Resolved 45 packages and wrote the lockfile. |
| `uv sync --locked` | Created a fresh environment and installed 45 packages from the lock. |
| `uv lock --check --offline` | Resolved the same 45 packages from the existing lock and cache. |
| `ruff check src tests` | All checks passed in the locked environment. |
| `ruff format --check src tests` | All 17 Python files already formatted. |
| `mypy` | No issues in 17 source and test files. |
| `pytest -q -p no:cacheprovider` | **117 passed in 6.22 s** after fixes. The review baseline was 103 passing tests. One dependency warning came from Starlette's use of the deprecated AnyIO `BlockingPortal` alias. |
| `python -u -m assessment.demo` | Exited 0. Exercised the stdio handshake and both tools, JSON-RPC `-32602` and `-32001` errors, transparent tool listing, primary success, 429 fallback, 3-second timeout fallback, sanitized backup failure, and all three redactions. |
| `git diff --check` | Passed on the review changes. |

The offline suite uses real stdio subprocesses and an on-disk SQLite database, including independent processes contending for the same quota. HTTP failures use deterministic in-process transports. The demo exercises actual HTTP connections on loopback. Hosted GitHub Actions verification remains pending.

## Submission preflight

Extracted the candidate ZIP into a separate directory and created a fresh Windows environment from the existing uv cache. The preflight ran `uv sync --locked --offline`, then ran the following commands with `uv run --locked --offline`.

| Command | Observed outcome |
| --- | --- |
| `pytest -q` | **117 passed in 6.50 s**, with the same Starlette dependency warning. |
| `ruff check src tests` | Passed. |
| `ruff format --check src tests` | All 17 Python files formatted. |
| `mypy` | No issues in 17 source and test files. |
| `python -u -m assessment.demo` | Passed all four tasks over real stdio and loopback HTTP. Median time to first safe streamed text was 19.005 ms across seven local requests. |

The hosted workflow pins checkout and uv setup to Node 24 releases and runs the same project checks on Linux and Windows. Hosted execution starts when the commit is pushed. [GitHub Actions runner runtime](https://github.blog/changelog/2026-09-23-node-20-is-no-longer-available-in-github-actions/)

## Review regressions

| Reproduce with | Evidence |
| --- | --- |
| `pytest -q tests/test_mcp_stdio.py tests/test_gateway.py` | Explicit null parameters return `-32602`. An unpaired surrogate returns `-32700` through the real stdio process. A duplicate upstream result returns HTTP 502. An admin response setting a synthetic cookie leaves the following viewer request without that cookie. |
| `pytest -q tests/test_pii.py` | Every split of `1 123@example.com!` now produces `1 [REDACTED]!`. The same check covers overlapping email and numeric matches and a spaced card continuation after the buffer overflows. |
| `pytest -q tests/test_quota.py` | Twelve simultaneous initializers open a fresh database successfully. Independent processes admit exactly ten reservations of ten tokens under a 100-token cap. Repeated cancellation settles a committed admission. Two out-of-range usage charges settle and deny work until the test clock advances past the window. |
| `pytest -q tests/test_gateway.py tests/test_router.py` | ASGI disconnects cancel stalled work before headers on both generation routes and after the first streamed delta. The tests check upstream closure and the settled quota. An expired attempt refuses a buffered delta and does not contact the backup once output has begun. |

## Local streaming measurement

The demo timed seven requests from immediately before sending the HTTP request to parsing the first SSE text delta at the client. These are end-to-end **time to first safe output** samples in milliseconds:

```text
67.298  34.395  47.398  28.156  25.256  29.638  26.742
```

Minimum **25.256 ms**, median **29.638 ms**, maximum **67.298 ms**. The measured interval includes loopback HTTP, quota admission, provider framing, and scanner delay. It starts after server startup and measures client receipt of the first redacted delta. Run `uv run python -m assessment.demo` to repeat the procedure.

## Submission review

The submission check scanned the staged files for common credential signatures. The demonstration bearer values are synthetic. The ZIP is generated from the local submission commit on `main`. [Submission instructions](README.md#submission)
