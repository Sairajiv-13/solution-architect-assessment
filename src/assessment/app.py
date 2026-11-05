import asyncio
import uuid
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from http.cookiejar import Cookie, CookieJar, DefaultCookiePolicy
from typing import Any

import anyio
import httpx
import mcp.types as mt
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import ValidationError
from starlette.requests import ClientDisconnect
from starlette.types import Receive, Scope, Send

from assessment.config import Settings, digest
from assessment.protocol import CallParams, RPCFault, decode_json, parse_rpc, tool_params
from assessment.provider import Provider, ProviderFailure, limited_body, open_upstream, sse
from assessment.quota import Quota
from assessment.router import GatewayError, Generation, Router, upstream_error


class RejectUpstreamCookies(DefaultCookiePolicy):
    def set_ok(self, cookie: Cookie, request: Any) -> bool:
        return False


@asynccontextmanager
async def cancel_on_disconnect(receive: Receive) -> AsyncIterator[None]:
    """Cancel upstream work even when no response send is in progress."""

    async def watch(scope: anyio.CancelScope) -> None:
        while (await receive())["type"] != "http.disconnect":
            pass
        scope.cancel()

    failure: Exception | None = None
    async with anyio.create_task_group() as watchers:
        with anyio.CancelScope() as operation:
            watchers.start_soon(watch, operation)
            try:
                yield
            except Exception as exc:
                # Preserve gateway errors as ordinary exceptions across task group exit.
                failure = exc
            finally:
                watchers.cancel_scope.cancel()
    if operation.cancel_called:
        raise ClientDisconnect()
    if failure is not None:
        raise failure


class OwnedStreamResponse(StreamingResponse):
    def __init__(self, events: AsyncIterator[bytes], owner: AsyncGenerator[bytes, None]):
        self.owner = owner
        super().__init__(
            events,
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            async with cancel_on_disconnect(receive):
                await self.stream_response(send)
        except ClientDisconnect:
            pass
        except OSError:
            raise ClientDisconnect() from None
        finally:
            # Covers disconnect/send failure even before the events iterator starts.
            with anyio.CancelScope(shield=True):
                await self.owner.aclose()


async def body(request: Request, limit: int = 65_536) -> bytes:
    result = bytearray()
    async for chunk in request.stream():
        if len(result) + len(chunk) > limit:
            raise GatewayError("request_too_large", "Request body exceeds limit", 413)
        result.extend(chunk)
    return bytes(result)


def identity(request: Request, keys: dict[str, str]) -> str:
    header = request.headers.get("authorization", "").split()
    if len(header) != 2 or header[0].lower() != "bearer":
        raise GatewayError("unauthorized", "A valid bearer credential is required", 401)
    result = keys.get(digest(header[1]))
    if result is None:
        raise GatewayError("unauthorized", "A valid bearer credential is required", 401)
    return result


def create_app(
    settings: Settings | None = None, transport: httpx.AsyncBaseTransport | None = None
) -> FastAPI:
    config = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        quota = Quota(config.database, config.limit, config.window)
        await asyncio.to_thread(quota.initialize)
        async with httpx.AsyncClient(
            transport=transport,
            timeout=5,
            follow_redirects=False,
            trust_env=False,
            cookies=CookieJar(policy=RejectUpstreamCookies()),
        ) as client:
            app.state.client = client
            app.state.router = Router(
                quota,
                Provider(client, config.provider, config.secondary_provider),
                config.primary_timeout,
                config.secondary_timeout,
            )
            yield

    app = FastAPI(title="Assessment gateways", lifespan=lifespan)

    @app.middleware("http")
    async def errors(request: Request, call_next: Any) -> Response:
        request.state.request_id = uuid.uuid4().hex
        try:
            response: Response = await call_next(request)
        except GatewayError as exc:
            response = JSONResponse(exc.payload(request.state.request_id), status_code=exc.status)
            if exc.status == 401:
                response.headers["WWW-Authenticate"] = "Bearer"
        except ClientDisconnect:
            response = Response(status_code=499)
        except Exception:
            error = GatewayError("internal_error", "Gateway request failed", 500)
            response = JSONResponse(error.payload(request.state.request_id), status_code=500)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/mcp")
    async def mcp_proxy(request: Request) -> Response:
        role = identity(request, config.roles)
        envelope = None
        data: dict[str, Any] = {}
        try:
            raw = await body(request)
            envelope, data = parse_rpc(raw)
            params = tool_params(envelope)
            if "id" not in data:
                # One-way tool notifications are ignored; they never execute a tool.
                return Response(status_code=204)
            if (
                isinstance(params, CallParams)
                and params.name.startswith("admin_")
                and role != "admin"
            ):
                raise RPCFault(-32001, "Unauthorized Tool Call", envelope.id)
            client: httpx.AsyncClient = request.app.state.client
            upstream_request = client.build_request(
                "POST",
                config.downstream,
                content=raw,
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                timeout=None,
            )
            loop = asyncio.get_running_loop()
            deadline = loop.time() + config.mcp_timeout
            async with cancel_on_disconnect(request.receive), asyncio.timeout_at(deadline):
                async with open_upstream(client, upstream_request) as upstream:
                    if upstream.status_code != 200:
                        raise ProviderFailure()
                    wire = await limited_body(upstream)
                if loop.time() >= deadline:
                    raise TimeoutError()
            # Validate the response envelope; do not expose arbitrary HTML or provider error bodies.
            payload = mt.JSONRPCMessage.model_validate(decode_json(wire), strict=True).root
            if (
                not isinstance(payload, (mt.JSONRPCResponse, mt.JSONRPCError))
                or bool(payload.model_extra)
                or type(payload.id) is not type(envelope.id)
                or payload.id != envelope.id
            ):
                raise ProviderFailure()
            if isinstance(payload, mt.JSONRPCError):
                fault = RPCFault(payload.error.code, "Downstream request failed", envelope.id)
                return JSONResponse(fault.payload())
            return Response(wire, media_type="application/json")
        except RPCFault as exc:
            if envelope is not None and "id" not in data:
                return Response(status_code=204)
            return JSONResponse(
                exc.payload(), status_code=400 if exc.code in (-32700, -32600) else 200
            )
        except (TimeoutError, httpx.TimeoutException):
            fault = RPCFault(
                -32002, "Downstream request timed out", envelope.id if envelope else None
            )
            return JSONResponse(fault.payload(), status_code=504)
        except (httpx.HTTPError, ProviderFailure, ValueError, RecursionError):
            fault = RPCFault(-32002, "Downstream unavailable", envelope.id if envelope else None)
            return JSONResponse(fault.payload(), status_code=502)

    async def generation(request: Request) -> tuple[str, Generation]:
        tenant = identity(request, config.tenants)
        try:
            parsed = Generation.model_validate_json(await body(request))
        except ValidationError:
            raise GatewayError("invalid_request", "Invalid generation parameters", 400) from None
        return tenant, parsed

    @app.post("/v1/completions")
    async def complete(request: Request) -> dict[str, Any]:
        tenant, parsed = await generation(request)
        router: Router = request.app.state.router
        async with cancel_on_disconnect(request.receive):
            result = await router.complete(tenant, parsed)
        return {**result, "request_id": request.state.request_id}

    @app.post("/v1/generate")
    async def generate(request: Request) -> Response:
        tenant, parsed = await generation(request)
        router: Router = request.app.state.router
        stream = None
        # Preflight first safe output so opening failures can still carry an HTTP error status.
        try:
            async with cancel_on_disconnect(request.receive):
                ticket = await router.admit(tenant, parsed)
                stream = router.stream(ticket, parsed, request.state.request_id)
                first = await anext(stream)
        except BaseException:
            if stream is not None:
                with anyio.CancelScope(shield=True):
                    await stream.aclose()
            raise
        assert stream is not None

        async def events() -> AsyncIterator[bytes]:
            try:
                yield first
                async for event in stream:
                    yield event
            except asyncio.CancelledError:
                raise
            except Exception:
                yield sse("error", upstream_error().payload(request.state.request_id))
            finally:
                with anyio.CancelScope(shield=True):
                    await stream.aclose()

        return OwnedStreamResponse(events(), stream)

    return app
