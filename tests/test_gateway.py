import asyncio
import json
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.requests import ClientDisconnect
from starlette.types import Message, Scope

from assessment.app import OwnedStreamResponse, create_app
from assessment.config import Settings, digest


def rpc(
    method: str = "tools/list", params: Any = None, request_id: Any = "request-42"
) -> dict[str, Any]:
    result: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        result["params"] = params
    return result


def make_client(tmp_path: Path, handler: Callable[[httpx.Request], httpx.Response]) -> TestClient:
    return TestClient(
        create_app(
            Settings(database=tmp_path / "quota.sqlite3"), transport=httpx.MockTransport(handler)
        )
    )


def test_transparent_list_and_authz_zero_downstream_calls(tmp_path: Path) -> None:
    calls: list[httpx.Request] = []

    def downstream(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        data = json.loads(request.content)
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": data["id"], "result": {"tools": []}}
        )

    viewer = {
        "Authorization": "Bearer dev-viewer-token",
        "Connection": "X-Secret",
        "X-Secret": "must-not-forward",
    }
    with make_client(tmp_path, downstream) as client:
        listed = client.post("/mcp", json=rpc(), headers=viewer)
        assert listed.json() == {"jsonrpc": "2.0", "id": "request-42", "result": {"tools": []}}
        assert json.loads(calls[0].content) == rpc()
        assert "authorization" not in calls[0].headers
        assert "x-secret" not in calls[0].headers
        assert calls[0].headers.get("connection") != "X-Secret"
        before = len(calls)
        denied = client.post(
            "/mcp", json=rpc("tools/call", {"name": "admin_reset_key"}), headers=viewer
        )
        assert denied.json() == {
            "jsonrpc": "2.0",
            "id": "request-42",
            "error": {"code": -32001, "message": "Unauthorized Tool Call"},
        }
        assert len(calls) == before
        for name, token in [("echo", "dev-viewer-token"), ("admin_reset_key", "dev-admin-token")]:
            assert (
                "result"
                in client.post(
                    "/mcp",
                    json=rpc("tools/call", {"name": name}),
                    headers={"Authorization": f"Bearer {token}"},
                ).json()
            )
        assert len(calls) == before + 2


def test_upstream_cookie_is_not_replayed_across_callers(tmp_path: Path) -> None:
    cookies: list[str | None] = []

    def downstream(request: httpx.Request) -> httpx.Response:
        cookies.append(request.headers.get("cookie"))
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": "request-42", "result": {}},
            headers={"Set-Cookie": "synthetic_session=admin; Path=/"},
        )

    with make_client(tmp_path, downstream) as client:
        for role in ("admin", "viewer"):
            response = client.post(
                "/mcp", json=rpc(), headers={"Authorization": f"Bearer dev-{role}-token"}
            )
            assert response.status_code == 200
    assert cookies == [None, None]


@pytest.mark.parametrize(
    "token",
    [
        None,
        "",
        "Bearer",
        "Basic dev-admin-token",
        "Bearer admin",
        "Bearer eyJyb2xlIjoiYWRtaW4ifQ",
        "Bearer a b",
    ],
)
def test_unverified_credentials_never_forward(tmp_path: Path, token: str | None) -> None:
    def downstream(request: httpx.Request) -> httpx.Response:
        pytest.fail("Unauthenticated request reached downstream")

    with make_client(tmp_path, downstream) as client:
        response = client.post(
            "/mcp", json=rpc(), headers={"Authorization": token} if token else {}
        )
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "unauthorized"
        assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ([], -32600),
        ([rpc()], -32600),
        ({}, -32600),
        (rpc(request_id=True), -32600),
        (rpc(request_id=None), -32600),
        ({**rpc(), "jsonrpc": "1.0"}, -32600),
        (rpc("tools/list", {"extra": 1}), -32602),
        ({**rpc(), "params": None}, -32602),
        (rpc("tools/call", {"name": 12}), -32602),
        (rpc("tools/call", {"name": "echo", "arguments": []}), -32602),
        (rpc("tools/call", []), -32602),
        (rpc("missing"), -32601),
    ],
)
def test_rejections_never_forward(tmp_path: Path, payload: Any, code: int) -> None:
    def downstream(request: httpx.Request) -> httpx.Response:
        pytest.fail("Rejected request reached downstream")

    with make_client(tmp_path, downstream) as client:
        result = client.post(
            "/mcp", json=payload, headers={"Authorization": "Bearer dev-admin-token"}
        )
        assert result.json()["error"]["code"] == code


def test_parse_errors_notifications_and_size_limit(tmp_path: Path) -> None:
    def downstream(request: httpx.Request) -> httpx.Response:
        pytest.fail("Rejected request or notification reached downstream")

    with make_client(tmp_path, downstream) as client:
        headers = {"Authorization": "Bearer dev-admin-token"}
        assert client.post("/mcp", content=b"{", headers=headers).json()["error"]["code"] == -32700
        for method in ("tools/list", "tools/call", "unknown"):
            notification = {"jsonrpc": "2.0", "method": method}
            response = client.post("/mcp", json=notification, headers=headers)
            assert response.status_code == 204
            assert not response.content
        assert client.post("/mcp", content=b"a" * 65537, headers=headers).status_code == 413


@pytest.mark.parametrize(
    "failure", ["status", "network", "bad_json", "duplicate_result", "wrong_id", "oversize"]
)
def test_downstream_failures_are_sanitized(tmp_path: Path, failure: str) -> None:
    def downstream(request: httpx.Request) -> httpx.Response:
        if failure == "network":
            raise httpx.ConnectError("private-host secret-token traceback")
        if failure == "bad_json":
            return httpx.Response(200, text="private-host secret-token traceback")
        if failure == "duplicate_result":
            return httpx.Response(
                200, content=b'{"jsonrpc":"2.0","id":"request-42","result":{},"result":{}}'
            )
        if failure == "wrong_id":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 42, "result": {}})
        if failure == "oversize":
            return httpx.Response(200, content=b"x" * 262145)
        return httpx.Response(503, text="private-host secret-token traceback")

    with make_client(tmp_path, downstream) as client:
        response = client.post(
            "/mcp", json=rpc(), headers={"Authorization": "Bearer dev-viewer-token"}
        )
        assert response.status_code == 502
        assert response.json()["id"] == "request-42"
        assert response.json()["error"]["code"] == -32002
        assert "private-host" not in response.text


def test_http_generation_errors_redaction_and_fallback(tmp_path: Path) -> None:
    calls: list[str] = []

    def downstream(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        assert all(value is None for value in request.extensions["timeout"].values())
        if request.url.path.endswith("primary"):
            return httpx.Response(429, text="private provider failure")
        return httpx.Response(
            200,
            json={
                "text": "Email demo@example.com!",
                "usage": {"input_tokens": 1, "output_tokens": 10},
            },
        )

    with make_client(tmp_path, downstream) as client:
        assert client.post("/v1/completions", json={"prompt": "hi"}).status_code == 401
        headers = {"Authorization": "Bearer dev-tenant-key"}
        invalid = client.post(
            "/v1/completions", json={"prompt": "hi", "max_tokens": True}, headers=headers
        )
        assert invalid.status_code == 400
        result = client.post("/v1/completions", json={"prompt": "hi"}, headers=headers)
        assert result.status_code == 200
        assert result.json()["model"] == "secondary"
        assert result.json()["text"] == "Email [REDACTED]!"
        assert result.headers["x-request-id"] == result.json()["request_id"]
        assert calls == ["/provider/primary", "/provider/secondary"]


def test_stream_error_after_output_has_valid_sse(tmp_path: Path) -> None:
    calls: list[str] = []

    def downstream(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=b'data: {"delta":"Hello! "}\n\ndata: private malformed data\n\n',
        )

    with make_client(tmp_path, downstream) as client:
        result = client.post(
            "/v1/generate",
            json={"prompt": "hi"},
            headers={"Authorization": "Bearer dev-tenant-key"},
        )
        assert result.status_code == 200
        events = [part for part in result.text.split("\n\n") if part]
        assert events[0].startswith("event: delta\n")
        assert events[1].startswith("event: error\n")
        assert (
            json.loads(events[1].split("data: ", 1)[1])["error"]["code"] == "upstream_unavailable"
        )
        assert "private malformed" not in result.text
        assert calls == ["/provider/primary/stream"]


def test_stream_preflight_failure_uses_http_error(tmp_path: Path) -> None:
    def downstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="private failure")

    with make_client(tmp_path, downstream) as client:
        response = client.post(
            "/v1/generate",
            json={"prompt": "hi"},
            headers={"Authorization": "Bearer dev-tenant-key"},
        )
        assert response.status_code == 502
        assert response.json()["error"]["code"] == "upstream_unavailable"


def test_duplicate_json_keys_never_reach_downstream(tmp_path: Path) -> None:
    def downstream(request: httpx.Request) -> httpx.Response:
        pytest.fail("Ambiguous JSON reached downstream")

    with make_client(tmp_path, downstream) as client:
        response = client.post(
            "/mcp",
            headers={"Authorization": "Bearer dev-viewer-token"},
            content=(
                '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":'
                '{"name":"admin_reset_key","name":"echo"}}'
            ),
        )
        assert response.json()["error"]["code"] == -32700


def test_downstream_jsonrpc_error_is_validated_and_sanitized(tmp_path: Path) -> None:
    def downstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": "request-42",
                "error": {"code": -32602, "message": "private stack trace", "data": "secret-token"},
            },
        )

    with make_client(tmp_path, downstream) as client:
        result = client.post(
            "/mcp", json=rpc(), headers={"Authorization": "Bearer dev-viewer-token"}
        )
        assert result.json()["error"] == {"code": -32602, "message": "Downstream request failed"}


def test_successful_stream_fallback_redacts_without_leaking_chunks(tmp_path: Path) -> None:
    calls: list[str] = []

    def downstream(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if "primary" in request.url.path:
            return httpx.Response(429)
        deltas = ["Hello: ", "demo@", "example.com", ";123-45-", "6789;4111 ", "1111 1111 1111!"]
        wire = "".join(f"data: {json.dumps({'delta': part})}\n\n" for part in deltas)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=(wire + "data: [DONE]\n\n").encode(),
        )

    with make_client(tmp_path, downstream) as client:
        result = client.post(
            "/v1/generate",
            json={"prompt": "hi"},
            headers={"Authorization": "Bearer dev-tenant-key"},
        )
        assert result.status_code == 200
        data = [
            json.loads(line[6:]) for line in result.text.splitlines() if line.startswith("data: ")
        ]
        assert "".join(part.get("text", "") for part in data) == (
            "Hello: [REDACTED];[REDACTED];[REDACTED]!"
        )
        assert data[-1]["model"] == "secondary"
        assert calls == ["/provider/primary/stream", "/provider/secondary/stream"]


async def test_disconnect_before_events_start_closes_prefetched_owner() -> None:
    closed, events_started = False, False

    async def owner() -> AsyncGenerator[bytes, None]:
        nonlocal closed
        try:
            yield b"first"
            yield b"next"
        finally:
            closed = True

    async def events() -> AsyncIterator[bytes]:
        nonlocal events_started
        events_started = True
        yield b"first"

    async def receive() -> Message:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def send(message: Message) -> None:
        raise OSError("disconnected before headers")

    stream = owner()
    await anext(stream)
    response = OwnedStreamResponse(events(), stream)
    with pytest.raises(ClientDisconnect):
        await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
    assert closed and not events_started


@pytest.mark.parametrize(
    ("path", "after_output"),
    [("/v1/generate", False), ("/v1/completions", False), ("/v1/generate", True)],
)
async def test_client_disconnect_cancels_provider_and_settles(
    path: str, after_output: bool, tmp_path: Path
) -> None:
    entered, cancelled = asyncio.Event(), asyncio.Event()
    visible, closed = asyncio.Event(), asyncio.Event()

    async def block() -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    class BlockingStream(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b'data: {"delta":"First!"}\n\n'
            await block()

        async def aclose(self) -> None:
            await asyncio.sleep(0)
            closed.set()

    async def downstream(request: httpx.Request) -> httpx.Response:
        assert "/primary" in request.url.path
        if after_output:
            return httpx.Response(
                200, headers={"Content-Type": "text/event-stream"}, stream=BlockingStream()
            )
        await block()
        raise AssertionError("Provider wait unexpectedly returned")

    app = create_app(Settings(database=tmp_path / "quota.db"), httpx.MockTransport(downstream))
    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "method": "POST",
        "path": path,
        "headers": [(b"authorization", b"Bearer dev-tenant-key")],
        "query_string": b"",
    }
    sent_request = False

    async def receive() -> Message:
        nonlocal sent_request
        if not sent_request:
            sent_request = True
            return {"type": "http.request", "body": b'{"prompt":"x"}', "more_body": False}
        await entered.wait()
        if after_output:
            await visible.wait()
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        if message["type"] == "http.response.body" and b"First!" in message.get("body", b""):
            visible.set()

    async with app.router.lifespan_context(app), asyncio.timeout(2):
        await app(scope, receive, send)
        assert cancelled.is_set()
        if after_output:
            assert closed.is_set()
        quota = app.state.router.quota
        # The started primary attempt is charged and the unstarted backup is released.
        await quota.reserve("tenant-demo", 50000 - 257)


def test_verified_keys_share_a_tenant_budget_without_persisting_keys(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    def downstream(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"text": "x"})

    keys = {"demo-key-a": "tenant-a", "demo-key-b": "tenant-a", "demo-key-c": "tenant-c"}
    settings = Settings(
        database=tmp_path / "quota.sqlite3",
        limit=6,
        tenants={digest(key): tenant for key, tenant in keys.items()},
    )
    with TestClient(create_app(settings, transport=httpx.MockTransport(downstream))) as client:
        for key, status in [("demo-key-a", 200), ("demo-key-b", 429), ("demo-key-c", 200)]:
            response = client.post(
                "/v1/completions",
                json={"prompt": "hi", "max_tokens": 1},
                headers={"Authorization": f"Bearer {key}"},
            )
            assert response.status_code == status
        assert len(requests) == 2
        assert all("authorization" not in request.headers for request in requests)
    stored = settings.database.read_bytes()
    assert all(key.encode() not in stored for key in keys)
