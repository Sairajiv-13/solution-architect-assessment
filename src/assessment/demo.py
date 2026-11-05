"""Run a real loopback demonstration and measure time to first safe client output."""

import asyncio
import json
import os
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.exceptions import McpError

from assessment.config import digest


async def stdio_demo() -> None:
    params = StdioServerParameters(command=sys.executable, args=["-m", "assessment.mcp_server"])
    with (
        tempfile.TemporaryDirectory(prefix="assessment-stdio-") as directory,
        (Path(directory) / "stderr.log").open("w+", encoding="utf-8") as errors,
    ):
        async with stdio_client(params, errlog=errors) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                assert len((await session.list_tools()).tools) == 2
                assert not (
                    await session.call_tool("get_customer_record", {"customer_id": "CUST-12345"})
                ).isError
                assert not (
                    await session.call_tool(
                        "trigger_refund",
                        {"customer_id": "CUST-12345", "amount": 12.5, "reason": "Duplicate charge"},
                    )
                ).isError
                try:
                    await session.call_tool(
                        "trigger_refund",
                        {"customer_id": "CUST-12345", "amount": True, "reason": "Duplicate charge"},
                    )
                except McpError as exc:
                    assert exc.error.code == -32602
                else:
                    raise AssertionError("Invalid input was not a JSON-RPC error")
    print("stdio: initialized, listed, called both tools; invalid arguments returned -32602")


def unused_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def ready(client: httpx.AsyncClient, url: str) -> None:
    deadline = time.perf_counter() + 15
    while time.perf_counter() < deadline:
        try:
            if (await client.get(url + "/health")).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        await asyncio.sleep(0.05)
    raise RuntimeError("Local demonstration server failed to start")


async def http_demo() -> None:
    downstream_port, gateway_port = unused_port(), unused_port()
    while gateway_port == downstream_port:
        gateway_port = unused_port()
    provider_url = f"http://127.0.0.1:{downstream_port}"
    gateway_url = f"http://127.0.0.1:{gateway_port}"
    with tempfile.TemporaryDirectory(prefix="assessment-demo-") as directory:
        env = {
            **os.environ,
            "DEV_ROLE_TOKENS": json.dumps(
                {"dev-admin-token": "admin", "dev-viewer-token": "viewer"}
            ),
            "TENANT_KEY_DIGESTS": json.dumps({digest("dev-tenant-key"): "tenant-demo"}),
            "PROVIDER_URL": provider_url,
            "SECONDARY_PROVIDER_URL": provider_url,
            "DOWNSTREAM_URL": provider_url + "/mcp",
            "QUOTA_DB": str(Path(directory) / "quota.sqlite3"),
            "TOKEN_LIMIT": "50000",
            "WINDOW_SECONDS": "60",
            "PRIMARY_TIMEOUT": "3.0",
            "SECONDARY_TIMEOUT": "5.0",
            # Use the base Windows interpreter to avoid an extra venv launcher process.
            "PYTHONPATH": os.pathsep.join(sys.path),
        }
        executable = (
            str(Path(sys.base_prefix) / "python.exe") if os.name == "nt" else sys.executable
        )
        processes: list[asyncio.subprocess.Process] = []
        with (Path(directory) / "servers.log").open("wb") as logs:
            try:
                for target, port, factory in [
                    ("assessment.mock:app", downstream_port, []),
                    ("assessment.app:create_app", gateway_port, ["--factory"]),
                ]:
                    processes.append(
                        await asyncio.create_subprocess_exec(
                            executable,
                            "-m",
                            "uvicorn",
                            target,
                            *factory,
                            "--host",
                            "127.0.0.1",
                            "--port",
                            str(port),
                            "--no-access-log",
                            "--no-proxy-headers",
                            env=env,
                            stdout=logs,
                            stderr=logs,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                        )
                    )
                async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
                    await ready(client, provider_url)
                    await ready(client, gateway_url)
                    viewer = {"Authorization": "Bearer dev-viewer-token"}
                    listed = await client.post(
                        gateway_url + "/mcp",
                        headers=viewer,
                        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                    )
                    assert len(listed.json()["result"]["tools"]) == 2
                    denied = await client.post(
                        gateway_url + "/mcp",
                        headers=viewer,
                        json={
                            "jsonrpc": "2.0",
                            "id": 2,
                            "method": "tools/call",
                            "params": {"name": "admin_reset_key"},
                        },
                    )
                    assert denied.json()["error"]["code"] == -32001
                    print("HTTP MCP: transparent list; viewer admin call denied with -32001")
                    headers = {"Authorization": "Bearer dev-tenant-key"}
                    for scenario, expected in [
                        ("ok", "primary"),
                        ("primary429", "secondary"),
                        ("primary_timeout", "secondary"),
                    ]:
                        response = await client.post(
                            gateway_url + "/v1/completions",
                            headers=headers,
                            json={"prompt": "Synthetic demo", "scenario": scenario},
                        )
                        assert response.status_code == 200, response.text
                        assert response.json()["model"] == expected
                        print(f"completion {scenario}: {expected}")
                    failed = await client.post(
                        gateway_url + "/v1/completions",
                        headers=headers,
                        json={"prompt": "Synthetic demo", "scenario": "backup_failure"},
                    )
                    assert failed.status_code == 502
                    assert failed.json()["error"]["code"] == "upstream_unavailable"
                    print("backup failure: sanitized HTTP 502")
                    samples: list[float] = []
                    for _ in range(7):
                        started = time.perf_counter()
                        first = True
                        output: list[str] = []
                        done = False
                        async with client.stream(
                            "POST",
                            gateway_url + "/v1/generate",
                            headers=headers,
                            json={"prompt": "Synthetic demo"},
                        ) as response:
                            assert response.status_code == 200
                            async for line in response.aiter_lines():
                                if line.startswith("data: "):
                                    event = json.loads(line[6:])
                                    if "text" in event:
                                        if first:
                                            samples.append((time.perf_counter() - started) * 1000)
                                            first = False
                                        output.append(event["text"])
                                    if "model" in event:
                                        done = True
                        text = "".join(output)
                        assert done and text.count("[REDACTED]") == 3
                        assert "demo@example.com" not in text and "123-45-6789" not in text
                    print("stream: email, SSN and card redacted; valid terminal event")
                    print(
                        json.dumps(
                            {
                                "metric": "request_to_first_safe_client_text_ms",
                                "trials": len(samples),
                                "min": round(min(samples), 3),
                                "median": round(statistics.median(samples), 3),
                                "max": round(max(samples), 3),
                                "samples": [round(value, 3) for value in samples],
                            }
                        )
                    )
            finally:
                for process in processes:
                    if process.returncode is not None:
                        continue
                    process.terminate()
                for process in processes:
                    try:
                        async with asyncio.timeout(5):
                            await process.wait()
                    except TimeoutError:
                        process.kill()
                        await process.wait()


async def main() -> None:
    await stdio_demo()
    await http_demo()


if __name__ == "__main__":
    asyncio.run(main())
