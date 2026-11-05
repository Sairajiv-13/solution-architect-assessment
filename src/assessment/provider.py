import codecs
import json
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

import anyio
import httpx
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, ValidationError


class ProviderFailure(Exception):
    def __init__(self, status: int = 502):
        self.status = status
        super().__init__("Provider request failed")


class Usage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    input_tokens: StrictInt = Field(ge=0)
    output_tokens: StrictInt = Field(ge=0)

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


class Delta(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    delta: StrictStr = ""
    usage: Usage | None = None


class Completion(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    text: StrictStr
    usage: Usage | None = None


async def limited_body(response: httpx.Response, limit: int = 262_144) -> bytes:
    body = bytearray()
    async for chunk in response.aiter_bytes(chunk_size=1024):
        if len(body) + len(chunk) > limit:
            raise ProviderFailure()
        body.extend(chunk)
    return bytes(body)


async def sse_deltas(chunks: AsyncIterator[bytes], max_event: int = 16_384) -> AsyncIterator[Delta]:
    """UTF-8, LF/CRLF SSE with bounded lines/events, multiline data, explicit [DONE]."""
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    line = ""
    data: list[str] = []
    size = 0
    try:
        async for chunk in chunks:
            for char in decoder.decode(chunk):
                size += 1
                if size > max_event:
                    raise ProviderFailure()
                if char != "\n":
                    line += char
                    continue
                value, line = line.removesuffix("\r"), ""
                if value:
                    if value.startswith("data:"):
                        data.append(value[5:].removeprefix(" "))
                    continue
                payload = "\n".join(data)
                data, size = [], 0
                if not payload:
                    continue
                if payload == "[DONE]":
                    return
                yield Delta.model_validate_json(payload)
        decoder.decode(b"", final=True)
        raise ProviderFailure()
    except (UnicodeError, ValidationError, ValueError):
        raise ProviderFailure() from None


def sse(event: str, data: dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=True)}\n\n".encode()


class Provider:
    def __init__(self, client: httpx.AsyncClient, base_url: str, secondary_url: str | None = None):
        self.client = client
        self.urls = {
            "primary": base_url.rstrip("/"),
            "secondary": (secondary_url or base_url).rstrip("/"),
        }

    @asynccontextmanager
    async def response(self, url: str, payload: dict[str, Any]) -> AsyncIterator[httpx.Response]:
        async with AsyncExitStack() as stack:
            response = await stack.enter_async_context(
                self.client.stream("POST", url, json=payload, timeout=None)
            )
            try:
                yield response
            finally:
                # Disconnect cancellation remains active at every cleanup await.
                with anyio.CancelScope(shield=True):
                    await stack.aclose()

    async def complete(self, model: str, payload: dict[str, Any]) -> Completion:
        async with self.response(f"{self.urls[model]}/provider/{model}", payload) as response:
            if response.status_code != 200:
                raise ProviderFailure(response.status_code)
            try:
                return Completion.model_validate_json(await limited_body(response))
            except ValidationError:
                raise ProviderFailure() from None

    @asynccontextmanager
    async def stream(
        self, model: str, payload: dict[str, Any]
    ) -> AsyncIterator[AsyncIterator[Delta]]:
        async with self.response(
            f"{self.urls[model]}/provider/{model}/stream", payload
        ) as response:
            if response.status_code != 200:
                raise ProviderFailure(response.status_code)
            if not response.headers.get("content-type", "").startswith("text/event-stream"):
                raise ProviderFailure()
            yield sse_deltas(response.aiter_bytes())
