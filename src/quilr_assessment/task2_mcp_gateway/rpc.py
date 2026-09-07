"""Inspect single JSON-RPC messages without rewriting accepted request bodies."""

import json
from dataclasses import dataclass
from typing import Any

RequestId = str | int | float | None


@dataclass
class RPCError(Exception):
    code: int
    message: str
    request_id: RequestId = None
    notification: bool = False


@dataclass(frozen=True)
class RPCRequest:
    method: str
    request_id: RequestId
    notification: bool
    tool_name: str | None = None


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON member")
        result[key] = value
    return result


def load_json(body: bytes) -> Any:
    value = json.loads(body.decode("utf-8"), object_pairs_hook=_unique_object)
    # Reject nonfinite numbers and unpaired Unicode surrogates, including nested values.
    json.dumps(value, allow_nan=False, ensure_ascii=False).encode("utf-8")
    return value


def valid_id(value: Any) -> bool:
    return value is None or type(value) in (str, int, float)


def inspect_request(body: bytes) -> RPCRequest:
    try:
        payload = load_json(body)
    except (ValueError, UnicodeError, RecursionError):
        raise RPCError(-32700, "Parse error") from None
    if not isinstance(payload, dict):
        raise RPCError(-32600, "Invalid Request")
    request_id = payload.get("id")
    if not valid_id(request_id):
        raise RPCError(-32600, "Invalid Request")
    if (
        payload.get("jsonrpc") != "2.0"
        or not isinstance(payload.get("method"), str)
        or not payload["method"]
        or "result" in payload
        or "error" in payload
    ):
        raise RPCError(-32600, "Invalid Request", request_id)
    notification = "id" not in payload
    params = payload.get("params")
    if "params" in payload and not isinstance(params, (dict, list)):
        raise RPCError(-32602, "Invalid params", request_id, notification)
    tool_name = None
    if payload["method"] == "tools/call":
        if (
            not isinstance(params, dict)
            or not isinstance(params.get("name"), str)
            or not params["name"]
        ):
            raise RPCError(-32602, "Invalid params", request_id, notification)
        if "arguments" in params and not isinstance(params["arguments"], dict):
            raise RPCError(-32602, "Invalid params", request_id, notification)
        tool_name = params["name"]
    return RPCRequest(payload["method"], request_id, notification, tool_name)


def error_payload(request_id: RequestId, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def inspect_response(body: bytes, request_id: RequestId) -> dict[str, Any]:
    try:
        payload = load_json(body)
        if (
            not isinstance(payload, dict)
            or payload.get("jsonrpc") != "2.0"
            or "id" not in payload
            or not valid_id(payload["id"])
            or payload["id"] != request_id
            or ("result" in payload) == ("error" in payload)
        ):
            raise ValueError("Invalid response")
        if "error" in payload:
            error = payload["error"]
            if (
                not isinstance(error, dict)
                or type(error.get("code")) is not int
                or not isinstance(error.get("message"), str)
            ):
                raise ValueError("Invalid error")
        return payload
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError("Invalid downstream response") from None
