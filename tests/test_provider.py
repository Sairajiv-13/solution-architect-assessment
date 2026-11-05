import asyncio
from collections.abc import AsyncIterator

import httpx
import pytest

from assessment.provider import Provider, ProviderFailure


class TrackedStream(httpx.AsyncByteStream):
    def __init__(self, malformed: bool):
        self.closed = False
        self.malformed = malformed
        self.waiting = asyncio.Event()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b'data: {"delta":"Hello! "}\n\n'
        if self.malformed:
            yield b"data: malformed\n\n"
        else:
            self.waiting.set()
            await asyncio.Event().wait()

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.parametrize("malformed", [False, True])
async def test_httpx_upstream_is_closed_on_cancel_or_parse_failure(malformed: bool) -> None:
    wire = TrackedStream(malformed)

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=wire)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        provider = Provider(client, "http://synthetic")
        async with provider.stream("primary", {}) as deltas:
            assert (await anext(deltas)).delta == "Hello! "
            if malformed:
                with pytest.raises(ProviderFailure):
                    await anext(deltas)
            else:
                task = asyncio.ensure_future(anext(deltas))
                await wire.waiting.wait()
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
    assert wire.closed
