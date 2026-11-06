import asyncio
from collections.abc import AsyncGenerator, Callable
from contextlib import AsyncExitStack
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, StrictStr

from assessment.pii import PIIScanner
from assessment.provider import Provider, ProviderFailure, Usage, sse
from assessment.quota import Quota, QuotaExceeded
from assessment.token_count import byte_estimate


class Generation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    prompt: StrictStr = Field(min_length=1, max_length=8192)
    max_tokens: int = Field(default=256, ge=1, le=4096)
    scenario: Literal["ok", "primary429", "primary_timeout", "backup_failure", "broken_stream"] = (
        "ok"
    )

    @property
    def attempt_budget(self) -> int:
        # Conservative UTF-8 byte estimate, not an exact tokenizer count.
        return len(self.prompt.encode("utf-8")) + self.max_tokens


class GatewayError(Exception):
    def __init__(self, code: str, message: str, status: int):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)

    def payload(self, request_id: str) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": self.message, "request_id": request_id}}


def upstream_error() -> GatewayError:
    return GatewayError("upstream_unavailable", "Generation provider unavailable", 502)


def retryable(error: BaseException) -> bool:
    return isinstance(error, (TimeoutError, httpx.TimeoutException)) or (
        isinstance(error, ProviderFailure) and error.status == 429
    )


class Router:
    def __init__(
        self,
        quota: Quota,
        provider: Provider,
        primary_timeout: float = 3,
        secondary_timeout: float = 5,
        count_input: Callable[[str], int] = byte_estimate,
    ):
        self.quota, self.provider = quota, provider
        self.attempts = (("primary", primary_timeout), ("secondary", secondary_timeout))
        self.count_input = count_input

    def budget(self, request: Generation) -> int:
        return self.count_input(request.prompt) + request.max_tokens

    async def admit(self, tenant: str, request: Generation) -> str:
        try:
            return await self.quota.reserve(tenant, 2 * self.budget(request))
        except QuotaExceeded:
            raise GatewayError("quota_exceeded", "Tenant token budget exhausted", 429) from None

    @staticmethod
    def reconcile(usage: Usage | None, reserved: int) -> int:
        return reserved if usage is None else usage.total

    async def complete(self, tenant: str, request: Generation) -> dict[str, Any]:
        ticket = await self.admit(tenant, request)
        budget = self.budget(request)
        charges: list[int] = []
        try:
            for index, (model, timeout) in enumerate(self.attempts):
                charges.append(budget)
                try:
                    clock = asyncio.get_running_loop().time
                    deadline = clock() + timeout
                    async with asyncio.timeout_at(deadline):
                        result = await self.provider.complete(model, request.model_dump())
                    if clock() >= deadline:
                        raise TimeoutError()
                    charges[-1] = self.reconcile(result.usage, budget)
                    if charges[-1] > budget:
                        # Record the overrun as debt rather than discarding reported usage.
                        raise ProviderFailure()
                    scanner = PIIScanner()
                    return {
                        "model": model,
                        "text": scanner.feed(result.text) + scanner.finish(),
                        "usage": result.usage.model_dump() if result.usage else None,
                    }
                except (ProviderFailure, httpx.HTTPError, TimeoutError) as exc:
                    if index == 0 and retryable(exc):
                        continue
                    raise upstream_error() from None
            raise upstream_error()
        finally:
            await self.quota.finish(ticket, sum(charges))

    async def stream(
        self, ticket: str, request: Generation, request_id: str
    ) -> AsyncGenerator[bytes, None]:
        charges: list[int] = []
        budget = self.budget(request)
        visible = False
        settled = False
        try:
            for index, (model, timeout) in enumerate(self.attempts):
                charges.append(budget)
                scanner = PIIScanner()
                loop = asyncio.get_running_loop()
                deadline = loop.time() + timeout
                seen_usage = False
                reported_usage: Usage | None = None
                try:
                    async with AsyncExitStack() as stack:
                        async with asyncio.timeout_at(deadline):
                            deltas = await stack.enter_async_context(
                                self.provider.stream(model, request.model_dump())
                            )
                        while True:
                            # A buffered delta can return before the timeout callback runs.
                            if loop.time() >= deadline:
                                raise TimeoutError()
                            # Apply the deadline to upstream awaits, not downstream yields.
                            async with asyncio.timeout_at(deadline):
                                delta = await anext(deltas, None)
                            if loop.time() >= deadline:
                                raise TimeoutError()
                            if delta is None:
                                break
                            if seen_usage:
                                raise ProviderFailure()
                            if delta.usage is not None:
                                seen_usage = True
                                reported_usage = delta.usage
                                if reported_usage.total > budget:
                                    charges[-1] = reported_usage.total
                                    raise ProviderFailure()
                            safe = scanner.feed(delta.delta)
                            if safe:
                                visible = True
                                yield sse("delta", {"text": safe})
                        # A usage event becomes authoritative only after valid stream termination.
                        charges[-1] = self.reconcile(reported_usage, budget)
                        safe = scanner.finish()
                        if safe:
                            visible = True
                            yield sse("delta", {"text": safe})
                    await self.quota.finish(ticket, sum(charges))
                    settled = True
                    yield sse("done", {"model": model, "request_id": request_id})
                    return
                except (ProviderFailure, httpx.HTTPError, TimeoutError) as exc:
                    if not visible and index == 0 and retryable(exc):
                        continue
                    raise upstream_error() from None
            raise upstream_error()
        finally:
            if not settled:
                await self.quota.finish(ticket, sum(charges))
