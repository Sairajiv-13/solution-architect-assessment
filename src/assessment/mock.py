"""Loopback-only demonstration downstream. Never deploy this unauthenticated mock publicly."""

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from assessment.protocol import CallParams, RPCFault, parse_rpc, tool_params
from assessment.router import Generation

app = FastAPI(title="Synthetic downstream")
TEXT = "Hello! Contact demo@example.com; SSN 123-45-6789; card 4111 1111 1111 1111. Done."


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/mcp")
async def mock_mcp(request: Request) -> Response:
    try:
        envelope, _ = parse_rpc(await request.body())
        params = tool_params(envelope)
        if isinstance(params, CallParams):
            if params.name not in ("echo", "admin_reset_key"):
                raise RPCFault(-32602, "Unknown tool", envelope.id)
            text = json.dumps(params.arguments) if params.name == "echo" else "Mock key reset"
            result: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
        else:
            result = {
                "tools": [
                    {"name": name, "inputSchema": {"type": "object"}}
                    for name in ("echo", "admin_reset_key")
                ]
            }
        return JSONResponse({"jsonrpc": "2.0", "id": envelope.id, "result": result})
    except RPCFault as exc:
        return JSONResponse(exc.payload())


async def scenario(model: str, request: Generation) -> Response | None:
    if model == "primary" and request.scenario in ("primary429", "backup_failure"):
        return Response(status_code=429)
    if model == "primary" and request.scenario == "primary_timeout":
        await asyncio.sleep(3.2)  # Explicit demo only; automated timeout tests use Events.
    if model == "secondary" and request.scenario == "backup_failure":
        return Response("private upstream failure details", status_code=503)
    return None


def synthetic_output(request: Generation) -> tuple[str, dict[str, int]]:
    # The mock treats each output character as one synthetic token and honors the cap.
    text = TEXT[: request.max_tokens]
    return text, {"input_tokens": len(request.prompt.encode()), "output_tokens": len(text)}


@app.post("/provider/{model}")
async def mock_complete(model: str, request: Generation) -> Response:
    failure = await scenario(model, request)
    if failure is not None:
        return failure
    text, usage = synthetic_output(request)
    return JSONResponse({"text": text, "usage": usage})


@app.post("/provider/{model}/stream")
async def mock_stream(model: str, request: Generation) -> Response:
    failure = await scenario(model, request)
    if failure is not None:
        return failure
    text, usage = synthetic_output(request)

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
