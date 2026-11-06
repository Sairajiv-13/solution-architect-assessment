# 0005. Keep the demo single host

The HTTP gateway handles authenticated tool listing and calls. A local adapter owns a separate MCP stdio session to the clinical server. The demo substitutes a synthetic registry response while exercising the actual clinical server process.

The scope excludes a distributed limiter, multi-region failover, full Streamable HTTP session handling, and a paid model provider. The local demo and hosted container smoke test exercise the same package. A public deployment needs real service credentials and network policy.
