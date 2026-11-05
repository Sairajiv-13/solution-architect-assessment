import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_quota import rows

from assessment.provider import Completion, Delta, Provider, ProviderFailure, Usage
from assessment.quota import Quota
from assessment.router import GatewayError, Generation, Router


class FakeProvider(Provider):
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.closed: list[str] = []
        self.outcomes: dict[str, Completion | Exception] = {}
        self.parts: dict[str, list[Delta | Exception]] = {}
        self.block: set[str] = set()
        self.entered = asyncio.Event()
        self.pulled = 0

    async def complete(self, model: str, payload: dict[str, Any]) -> Completion:
        self.calls.append(model)
        try:
            if model in self.block:
                self.entered.set()
                await asyncio.Event().wait()
            result = self.outcomes.get(
                model, Completion(text="Hello!", usage=Usage(input_tokens=1, output_tokens=2))
            )
            if isinstance(result, Exception):
                raise result
            return result
        finally:
            self.closed.append(model)

    @asynccontextmanager
    async def stream(
        self, model: str, payload: dict[str, Any]
    ) -> AsyncIterator[AsyncIterator[Delta]]:
        self.calls.append(model)

        async def parts() -> AsyncIterator[Delta]:
            if model in self.block:
                self.entered.set()
                await asyncio.Event().wait()
            for part in self.parts.get(model, [Delta(delta="Hello! ")]):
                self.pulled += 1
                if isinstance(part, Exception):
                    raise part
                yield part

        try:
            outcome = self.outcomes.get(model)
            if isinstance(outcome, Exception):
                raise outcome
            yield parts()
        finally:
            self.closed.append(model)


def setup(tmp_path: Path, timeout: float = 3, limit: int = 50000) -> tuple[Router, FakeProvider]:
    quota = Quota(tmp_path / "quota.sqlite3", limit=limit)
    quota.initialize()
    provider = FakeProvider()
    return Router(quota, provider, primary_timeout=timeout), provider


async def test_primary_success_and_release_unused_backup_reservation(tmp_path: Path) -> None:
    router, provider = setup(tmp_path)
    result = await router.complete("tenant", Generation(prompt="hello"))
    assert result["model"] == "primary"
    assert provider.calls == provider.closed == ["primary"]
    assert rows(router.quota.path)[0][0] == 3
    assert rows(router.quota.path)[0][1] is not None


@pytest.mark.parametrize("error", [ProviderFailure(429), httpx.ReadTimeout("secret timeout")])
async def test_fallback_failure_charged_conservatively(tmp_path: Path, error: Exception) -> None:
    router, provider = setup(tmp_path)
    provider.outcomes["primary"] = error
    request = Generation(prompt="hello")
    assert (await router.complete("tenant", request))["model"] == "secondary"
    assert provider.calls == provider.closed == ["primary", "secondary"]
    assert rows(router.quota.path)[0][0] == request.attempt_budget + 3


async def test_real_deadline_cancels_and_joins_primary(tmp_path: Path) -> None:
    router, provider = setup(tmp_path, timeout=0)
    provider.block.add("primary")
    assert (await router.complete("tenant", Generation(prompt="hi")))["model"] == "secondary"
    assert provider.closed == ["primary", "secondary"]


@pytest.mark.parametrize("streaming", [False, True])
async def test_buffered_primary_result_cannot_pass_expired_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, streaming: bool
) -> None:
    loop = asyncio.get_running_loop()
    clock = loop.time
    elapsed = 0.0
    monkeypatch.setattr(loop, "time", lambda: clock() + elapsed)

    class LateProvider(FakeProvider):
        async def complete(self, model: str, payload: dict[str, Any]) -> Completion:
            nonlocal elapsed
            result = await super().complete(model, payload)
            if model == "primary":
                elapsed = 4
            return result

        @asynccontextmanager
        async def stream(
            self, model: str, payload: dict[str, Any]
        ) -> AsyncIterator[AsyncIterator[Delta]]:
            async with super().stream(model, payload) as source:

                async def buffered() -> AsyncIterator[Delta]:
                    nonlocal elapsed
                    async for delta in source:
                        if model == "primary":
                            elapsed = 4
                        yield delta

                yield buffered()

    router, _ = setup(tmp_path)
    provider = LateProvider()
    provider.parts["primary"] = [Delta(delta="Late primary!")]
    router.provider = provider
    request = Generation(prompt="hi")
    if streaming:
        ticket = await router.admit("tenant", request)
        output = b"".join([event async for event in router.stream(ticket, request, "rid")])
        assert b"Late primary" not in output
        assert b'"model": "secondary"' in output
    else:
        assert (await router.complete("tenant", request))["model"] == "secondary"
    assert provider.calls == provider.closed == ["primary", "secondary"]


async def test_backup_failure_and_non_retryable_primary(tmp_path: Path) -> None:
    router, provider = setup(tmp_path)
    provider.outcomes = {"primary": ProviderFailure(429), "secondary": ProviderFailure(503)}
    with pytest.raises(GatewayError) as caught:
        await router.complete("tenant", Generation(prompt="hi"))
    assert caught.value.status == 502
    assert caught.value.payload("id")["error"] == {
        "code": "upstream_unavailable",
        "message": "Generation provider unavailable",
        "request_id": "id",
    }
    assert provider.calls == ["primary", "secondary"]
    provider.calls.clear()
    provider.outcomes["primary"] = ProviderFailure(500)
    with pytest.raises(GatewayError):
        await router.complete("tenant", Generation(prompt="hi"))
    assert provider.calls == ["primary"]


async def test_separate_backup_timeout_is_bounded(tmp_path: Path) -> None:
    router, provider = setup(tmp_path)
    router.attempts = (("primary", 3), ("secondary", 0))
    provider.outcomes["primary"] = ProviderFailure(429)
    provider.block.add("secondary")
    with pytest.raises(GatewayError):
        await router.complete("tenant", Generation(prompt="hi"))
    assert provider.closed == ["primary", "secondary"]


async def test_cancellation_propagates_without_fallback_and_settles(tmp_path: Path) -> None:
    router, provider = setup(tmp_path)
    provider.block.add("primary")
    request = Generation(prompt="hi")
    task = asyncio.create_task(router.complete("tenant", request))
    await provider.entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert provider.calls == provider.closed == ["primary"]
    assert rows(router.quota.path)[0][0] == request.attempt_budget
    assert rows(router.quota.path)[0][1] is not None


async def test_missing_usage_and_usage_overrun(tmp_path: Path) -> None:
    router, provider = setup(tmp_path)
    request = Generation(prompt="hi")
    provider.outcomes["primary"] = Completion(text="Hello")
    await router.complete("tenant", request)
    assert rows(router.quota.path)[0][0] == request.attempt_budget
    provider.outcomes["primary"] = Completion(
        text="Hello", usage=Usage(input_tokens=60000, output_tokens=1)
    )
    with pytest.raises(GatewayError):
        await router.complete("tenant", request)
    assert rows(router.quota.path)[1][0] == 60001
    with pytest.raises(GatewayError) as caught:
        await router.complete("tenant", request)
    assert caught.value.status == 429
    assert len(provider.calls) == 2


async def test_quota_denial_never_contacts_provider(tmp_path: Path) -> None:
    router, provider = setup(tmp_path, limit=100)
    with pytest.raises(GatewayError) as caught:
        await router.complete("tenant", Generation(prompt="hi"))
    assert caught.value.code == "quota_exceeded"
    assert provider.calls == []


@pytest.mark.parametrize("use_timeout", [False, True])
async def test_stream_fallback_only_before_visible_output(
    tmp_path: Path, use_timeout: bool
) -> None:
    router, provider = setup(tmp_path, timeout=0 if use_timeout else 3)
    if use_timeout:
        provider.block.add("primary")
    else:
        provider.outcomes["primary"] = ProviderFailure(429)
    request = Generation(prompt="hi")
    ticket = await router.admit("tenant", request)
    output = b"".join([event async for event in router.stream(ticket, request, "rid")])
    assert b'"model": "secondary"' in output
    assert provider.calls == provider.closed == ["primary", "secondary"]


async def test_stream_does_not_retry_after_output(tmp_path: Path) -> None:
    router, provider = setup(tmp_path)
    provider.parts["primary"] = [Delta(delta="Visible! "), httpx.ReadTimeout("secret")]
    request = Generation(prompt="hi")
    ticket = await router.admit("tenant", request)
    stream = router.stream(ticket, request, "rid")
    assert b"Visible!" in await anext(stream)
    with pytest.raises(GatewayError):
        await anext(stream)
    assert provider.calls == provider.closed == ["primary"]


async def test_stream_deadline_rejects_buffered_delta_after_backpressure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    router, provider = setup(tmp_path)
    provider.parts["primary"] = [Delta(delta="First!"), Delta(delta="Second!")]
    request = Generation(prompt="hi")
    ticket = await router.admit("tenant", request)
    stream = router.stream(ticket, request, "rid")
    assert b"First!" in await anext(stream)
    loop = asyncio.get_running_loop()
    now = loop.time
    monkeypatch.setattr(loop, "time", lambda: now() + 4)
    with pytest.raises(GatewayError):
        await anext(stream)
    assert provider.pulled == 1
    assert provider.calls == provider.closed == ["primary"]


async def test_stream_pull_backpressure_close_and_redaction(tmp_path: Path) -> None:
    router, provider = setup(tmp_path)
    provider.parts["primary"] = [
        Delta(delta="Hello! "),
        Delta(delta="demo@"),
        Delta(delta="example.com"),
        Delta(delta="! "),
    ]
    request = Generation(prompt="hi")
    ticket = await router.admit("tenant", request)
    stream = router.stream(ticket, request, "rid")
    assert b"Hello!" in await anext(stream)
    assert provider.pulled == 1  # No eager reader task or unbounded queue.
    assert b"[REDACTED]" in await anext(stream)
    await stream.aclose()
    assert provider.closed == ["primary"]
    assert rows(router.quota.path)[0][1] is not None


async def test_stream_usage_is_terminal_and_cannot_be_repeated(tmp_path: Path) -> None:
    router, provider = setup(tmp_path)
    provider.parts["primary"] = [
        Delta(usage=Usage(input_tokens=1, output_tokens=1)),
        Delta(delta="bad"),
    ]
    request = Generation(prompt="hi")
    ticket = await router.admit("tenant", request)
    with pytest.raises(GatewayError):
        _ = [event async for event in router.stream(ticket, request, "rid")]
    assert rows(router.quota.path)[0][0] == request.attempt_budget


async def test_stream_reconciles_after_successful_termination(tmp_path: Path) -> None:
    router, provider = setup(tmp_path)
    provider.parts["primary"] = [
        Delta(delta="Hello! "),
        Delta(usage=Usage(input_tokens=1, output_tokens=2)),
    ]
    request = Generation(prompt="hi")
    ticket = await router.admit("tenant", request)
    _ = [event async for event in router.stream(ticket, request, "rid")]
    assert rows(router.quota.path)[0][0] == 3


async def test_stream_task_cancellation_closes_provider_and_settles(tmp_path: Path) -> None:
    router, provider = setup(tmp_path)
    provider.block.add("primary")
    request = Generation(prompt="hi")
    ticket = await router.admit("tenant", request)
    stream = router.stream(ticket, request, "rid")
    task = asyncio.create_task(anext(stream))
    await provider.entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert provider.calls == provider.closed == ["primary"]
    assert rows(router.quota.path)[0][1] is not None
