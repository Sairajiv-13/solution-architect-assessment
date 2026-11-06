"""Loopback-only demonstration downstream. Never deploy this unauthenticated mock publicly."""

import asyncio
import json
import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.exceptions import McpError

from assessment.protocol import CallParams, RPCFault, parse_rpc, tool_params
from assessment.router import Generation
from assessment.token_count import byte_estimate, input_counter


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.tokenizer, app.state.count_input = await asyncio.to_thread(input_counter)
    app.state.clinical = None
    python = os.getenv("CLINICAL_MCP_PYTHON")
    if not python:
        yield
        return
    env = {
        key: value
        for key in ("CT_MCP_BASE_URL", "CT_MCP_MAX_RETRIES", "CT_MCP_TIMEOUT_SECONDS")
        if (value := os.getenv(key)) is not None
    }
    params = StdioServerParameters(
        command=python,
        args=["-m", "clinical_trials_mcp.server", "--transport", "stdio"],
        env=env,
    )
    async with stdio_client(params) as (reader, writer), ClientSession(reader, writer) as clinical:
        await clinical.initialize()
        app.state.clinical = clinical
        yield


app = FastAPI(title="Synthetic downstream", lifespan=lifespan)
TEXT = "Hello! Contact demo@example.com; SSN 123-45-6789; card 4111 1111 1111 1111. Done."


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/mcp")
async def mock_mcp(request: Request) -> Response:
    envelope = None
    try:
        envelope, _ = parse_rpc(await request.body())
        params = tool_params(envelope)
        clinical: ClientSession | None = getattr(request.app.state, "clinical", None)
        if isinstance(params, CallParams):
            if params.name in ("echo", "admin_reset_key"):
                text = json.dumps(params.arguments) if params.name == "echo" else "Mock key reset"
                result: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
            elif clinical is not None:
                call = await clinical.call_tool(params.name, params.arguments)
                result = call.model_dump(by_alias=True, mode="json", exclude_none=True)
            else:
                raise RPCFault(-32602, "Unknown tool", envelope.id)
        else:
            result = {
                "tools": [
                    {"name": name, "inputSchema": {"type": "object"}}
                    for name in ("echo", "admin_reset_key")
                ]
            }
            if clinical is not None:
                listed = await clinical.list_tools(cursor=params.cursor)
                result["tools"].extend(
                    tool.model_dump(by_alias=True, mode="json", exclude_none=True)
                    for tool in listed.tools
                )
                if listed.nextCursor is not None:
                    result["nextCursor"] = listed.nextCursor
        return JSONResponse({"jsonrpc": "2.0", "id": envelope.id, "result": result})
    except RPCFault as exc:
        return JSONResponse(exc.payload())
    except McpError as exc:
        fault = RPCFault(
            exc.error.code, "Clinical MCP request failed", envelope.id if envelope else None
        )
        return JSONResponse(fault.payload())


@app.get("/registry/studies/{nct_id}")
async def registry_fixture(nct_id: str) -> dict[str, Any]:
    """A synthetic registry response for the optional clinical subprocess demo."""
    return {
        "protocolSection": {
            "identificationModule": {
                "nctId": nct_id,
                "briefTitle": "Synthetic study for the gateway demo",
            },
            "statusModule": {"overallStatus": "RECRUITING"},
        }
    }


async def scenario(model: str, request: Generation) -> Response | None:
    if model == "primary" and request.scenario in ("primary429", "backup_failure"):
        return Response(status_code=429)
    if model == "primary" and request.scenario == "primary_timeout":
        await asyncio.sleep(3.2)  # Explicit demo only; automated timeout tests use Events.
    if model == "secondary" and request.scenario == "backup_failure":
        return Response("private upstream failure details", status_code=503)
    return None


def synthetic_output(
    request: Generation, count_input: Callable[[str], int]
) -> tuple[str, dict[str, int]]:
    # The mock treats each output character as one synthetic token and honors the cap.
    text = TEXT[: request.max_tokens]
    return text, {"input_tokens": count_input(request.prompt), "output_tokens": len(text)}


@app.post("/provider/{model}")
async def mock_complete(model: str, request: Generation, http: Request) -> Response:
    failure = await scenario(model, request)
    if failure is not None:
        return failure
    text, usage = synthetic_output(request, getattr(http.app.state, "count_input", byte_estimate))
    return JSONResponse({"text": text, "usage": usage})


@app.post("/provider/{model}/stream")
async def mock_stream(model: str, request: Generation, http: Request) -> Response:
    failure = await scenario(model, request)
    if failure is not None:
        return failure
    text, usage = synthetic_output(request, getattr(http.app.state, "count_input", byte_estimate))

    async def chunks() -> AsyncIterator[bytes]:
        for offset in range(0, len(text), 3):
            frame = f"data: {json.dumps({'delta': text[offset : offset + 3]})}\n\n".encode()
            # Split even the SSE JSON framing, independently of the text delta.
            yield frame[:7]
            await asyncio.sleep(0)
            yield frame[7:]
        if request.scenario == "broken_stream":
            yield b"data: {malformed\n\n"
            return
        yield f"data: {json.dumps({'usage': usage})}\n\ndata: [DONE]\n\n".encode()

    return StreamingResponse(chunks(), media_type="text/event-stream")
