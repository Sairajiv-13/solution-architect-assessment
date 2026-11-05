"""Strict JSON-RPC validation shared by the HTTP proxy and stdio ingress."""

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, ValidationError


class RPCFault(Exception):
    def __init__(self, code: int, message: str, request_id: str | int | None = None):
        self.code, self.message, self.request_id = code, message, request_id
        super().__init__(message)

    def payload(self) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": self.request_id,
            "error": {"code": self.code, "message": self.message},
        }


class Envelope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    jsonrpc: StrictStr
    method: StrictStr
    id: StrictStr | StrictInt | None = None
    params: dict[str, Any] | list[Any] | None = None


class ListParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    cursor: StrictStr | None = None
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class CallParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: StrictStr = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


def decode_json(raw: str | bytes) -> Any:
    def reject_constant(value: str) -> None:
        raise ValueError(value)

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON member")
            result[key] = value
        return result

    text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
    data = json.loads(text, parse_constant=reject_constant, object_pairs_hook=unique_object)
    # json.loads accepts unpaired surrogate escapes that the SDK's UTF-8 parser rejects.
    json.dumps(data, ensure_ascii=False).encode("utf-8")
    return data


def parse_rpc(raw: str | bytes) -> tuple[Envelope, dict[str, Any]]:
    try:
        data = decode_json(raw)
    except (ValueError, RecursionError):
        raise RPCFault(-32700, "Parse error") from None
    if not isinstance(data, dict):
        raise RPCFault(-32600, "Invalid Request; batches are unsupported")
    request_id = data.get("id")
    safe_id = request_id if type(request_id) in (str, int) else None
    try:
        envelope = Envelope.model_validate(data)
        if envelope.jsonrpc != "2.0" or ("id" in data and safe_id is None):
            raise ValueError("invalid envelope")
    except (ValidationError, ValueError):
        raise RPCFault(-32600, "Invalid Request", safe_id) from None
    return envelope, data


def tool_params(envelope: Envelope) -> CallParams | ListParams:
    if envelope.params is None and "params" in envelope.model_fields_set:
        raise RPCFault(-32602, "Invalid params", envelope.id)
    try:
        if envelope.method == "tools/list":
            return ListParams.model_validate({} if envelope.params is None else envelope.params)
        if envelope.method == "tools/call":
            return CallParams.model_validate(envelope.params)
    except ValidationError:
        raise RPCFault(-32602, "Invalid params", envelope.id) from None
    raise RPCFault(-32601, "Method not found", envelope.id)
