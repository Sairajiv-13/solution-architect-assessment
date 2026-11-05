"""Official SDK stdio server; small ingress and handler adaptations explained in README."""

import asyncio
import json
import logging
import sys
from collections.abc import AsyncIterator
from decimal import Decimal
from io import TextIOWrapper
from typing import Annotated, Any

import anyio
import mcp.types as mt
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import McpError
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictStr,
    ValidationError,
    WithJsonSchema,
    field_validator,
)

from assessment.protocol import RPCFault, parse_rpc, tool_params


class CustomerInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    customer_id: Annotated[StrictStr, Field(pattern=r"^CUST-[0-9]{5}$")]


class RefundInput(CustomerInput):
    amount: Annotated[Decimal, WithJsonSchema({"type": "number", "exclusiveMinimum": 0})] = Field(
        gt=0, allow_inf_nan=False
    )
    reason: StrictStr = Field(min_length=10)

    @field_validator("amount", mode="before")
    @classmethod
    def json_number(cls, value: Any) -> Any:
        if type(value) not in (int, float):
            raise ValueError("amount must be a finite JSON number")
        return Decimal(str(value))

    @field_validator("reason", mode="before")
    @classmethod
    def trim_reason(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value


SCHEMAS: dict[str, type[CustomerInput]] = {
    "get_customer_record": CustomerInput,
    "trigger_refund": RefundInput,
}
server: Server[Any] = Server("customer-assessment", version="0.1.0")


async def list_tools(request: mt.ListToolsRequest) -> mt.ServerResult:
    tools = [
        mt.Tool(
            name=name,
            description="Synthetic demonstration; no real side effects",
            inputSchema=model.model_json_schema(),
        )
        for name, model in SCHEMAS.items()
    ]
    return mt.ServerResult(mt.ListToolsResult(tools=tools))


async def call_tool(request: mt.CallToolRequest) -> mt.ServerResult:
    model = SCHEMAS.get(request.params.name)
    if model is None:
        raise McpError(mt.ErrorData(code=mt.INVALID_PARAMS, message="Unknown tool"))
    try:
        args = model.model_validate(request.params.arguments)
    except ValidationError:
        raise McpError(
            mt.ErrorData(code=mt.INVALID_PARAMS, message="Invalid tool arguments")
        ) from None
    result: dict[str, Any]
    if isinstance(args, RefundInput):
        # Decimal(str(...)) avoids carrying binary floating point artifacts into mock money.
        amount = args.amount
        result = {
            "customer_id": args.customer_id,
            "amount": str(amount),
            "status": "mock_refund",
            "reason": args.reason,
        }
    else:
        result = {"customer_id": args.customer_id, "name": "Synthetic Customer", "currency": "USD"}
    return mt.ServerResult(
        mt.CallToolResult(
            content=[mt.TextContent(type="text", text=json.dumps(result))], structuredContent=result
        )
    )


# Public low-level dispatch registry bypasses call_tool()'s broad exception-to-tool-error wrapper.
server.request_handlers[mt.CallToolRequest] = call_tool
server.request_handlers[mt.ListToolsRequest] = list_tools


async def main() -> None:
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(levelname)s %(message)s")
    # The pinned SDK logs malformed wire input instead of replying with a JSON-RPC parse error.
    # Its public stdin/stdout injection points let us validate lines and serialize error replies.
    source = anyio.wrap_file(sys.stdin.buffer)
    sink = anyio.wrap_file(TextIOWrapper(sys.stdout.buffer, encoding="utf-8"))
    lock = anyio.Lock()

    class Output:
        async def write(self, value: str) -> None:
            async with lock:
                await sink.write(value)
                await sink.flush()

        async def flush(self) -> None:
            pass

    output = Output()

    async def validated_lines() -> AsyncIterator[str]:
        async for raw in source:
            envelope = None
            try:
                line = raw.decode("utf-8")
                envelope, data = parse_rpc(line)
                if "id" not in data:
                    if envelope.method in ("notifications/initialized", "notifications/cancelled"):
                        yield line
                    continue
                if envelope.method in ("tools/list", "tools/call"):
                    tool_params(envelope)
                elif envelope.method not in ("initialize", "ping"):
                    raise RPCFault(-32601, "Method not found", envelope.id)
                elif "params" in data and not isinstance(envelope.params, dict):
                    raise RPCFault(-32602, "Invalid params", envelope.id)
                yield line
            except (RPCFault, UnicodeDecodeError) as exc:
                fault = exc if isinstance(exc, RPCFault) else RPCFault(-32700, "Parse error")
                # Notifications never receive a JSON-RPC response.
                if envelope is None or "id" in envelope.model_fields_set:
                    await output.write(json.dumps(fault.payload()) + "\n")

    # These are intentionally duck-typed adapters for the SDK's async file interface.
    async with stdio_server(
        stdin=validated_lines(),  # type: ignore[arg-type]
        stdout=output,  # type: ignore[arg-type]
    ) as (reader, writer):
        await server.run(reader, writer, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
