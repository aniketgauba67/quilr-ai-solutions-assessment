"""Stateless HTTP/JSON-RPC fixture for exercising the Task 2 gateway."""

import json
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

MAX_BODY_BYTES = 65_536
TOOL_DESCRIPTIONS = {
    "get_status": "Return deterministic service status.",
    "admin_reset_key": "Simulate a protected operation without changing any key.",
    "demo_error": "Return a deterministic, sanitized tool execution failure.",
}


def _invalid_constant(value: str) -> None:
    raise ValueError("Non-JSON numeric constant")


def _error(request_id: object, code: int, message: str) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def _dispatch(payload: dict[str, Any]) -> dict[str, object]:
    request_id = payload.get("id")
    method = payload["method"]
    if method == "tools/list":
        result: dict[str, object] = {
            "tools": [
                {
                    "name": name,
                    "description": description,
                    "inputSchema": {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                }
                for name, description in TOOL_DESCRIPTIONS.items()
            ]
        }
    elif method == "tools/call":
        params = payload.get("params")
        if not isinstance(params, dict) or not isinstance(params.get("name"), str):
            return _error(request_id, -32602, "Invalid params")
        if params["name"] not in TOOL_DESCRIPTIONS or params.get("arguments", {}) != {}:
            return _error(request_id, -32602, "Invalid params")
        if params["name"] == "demo_error":
            result = {
                "content": [{"type": "text", "text": "Mock tool execution failed"}],
                "isError": True,
            }
        else:
            value = (
                {"status": "ok", "service": "mock-mcp"}
                if params["name"] == "get_status"
                else {"status": "simulated", "operation": "reset_key"}
            )
            result = {
                "content": [{"type": "text", "text": json.dumps(value, sort_keys=True)}],
                "structuredContent": value,
                "isError": False,
            }
    else:
        return _error(request_id, -32601, "Method not found")
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def create_app() -> FastAPI:
    app = FastAPI(title="Task 2 mock downstream", docs_url=None, redoc_url=None)
    app.state.request_count = 0

    @app.post("/mcp")
    async def mcp(request: Request) -> Response:
        app.state.request_count += 1
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > MAX_BODY_BYTES:
                return JSONResponse({"error": "Request too large"}, status_code=413)
            body.extend(chunk)
        try:
            payload = json.loads(body.decode("utf-8"), parse_constant=_invalid_constant)
            json.dumps(payload, allow_nan=False, ensure_ascii=False).encode("utf-8")
        except (ValueError, RecursionError):
            return JSONResponse(_error(None, -32700, "Parse error"))
        if (
            not isinstance(payload, dict)
            or payload.get("jsonrpc") != "2.0"
            or not isinstance(payload.get("method"), str)
            or not payload["method"]
            or ("params" in payload and not isinstance(payload["params"], (dict, list)))
            or ("id" in payload and type(payload["id"]) not in (str, int, float, type(None)))
        ):
            return JSONResponse(_error(None, -32600, "Invalid Request"))
        response = _dispatch(payload)
        # Explicit null IDs are requests. Only an absent ID denotes a notification.
        if "id" not in payload:
            return Response(status_code=204)
        return JSONResponse(response)

    return app
