# 0004. Use FastAPI and HTTPX

The stdio server and existing clinical tooling are Python. FastAPI keeps the gateway in that stack and provides an ASGI receive channel for disconnects. Pydantic validates wire payloads. HTTPX supplies the connection pool and incremental body reads.

Node could implement the same contract. A separate runtime would add deployment and test setup without changing the proxy or quota design.
