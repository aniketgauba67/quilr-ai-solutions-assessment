"""Bounded asynchronous HTTP forwarding to a configured downstream endpoint."""

import asyncio
import logging

import httpx
from fastapi.responses import JSONResponse, Response

from .rpc import RPCRequest, error_payload, inspect_response

MAX_RESPONSE_BYTES = 256 * 1024
logger = logging.getLogger(__name__)


async def forward(
    client: httpx.AsyncClient,
    url: str,
    body: bytes,
    rpc: RPCRequest,
    timeout: float,
) -> Response:
    try:
        async with asyncio.timeout(timeout):
            async with client.stream(
                "POST",
                url,
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Accept-Encoding": "identity",
                },
            ) as upstream:
                if not 200 <= upstream.status_code < 300:
                    raise ValueError("Downstream HTTP failure")
                if rpc.notification:
                    return Response(status_code=204)
                if (
                    upstream.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                    != "application/json"
                ):
                    raise ValueError("Expected JSON")
                if upstream.headers.get("content-encoding", "identity").lower() != "identity":
                    raise ValueError("Unsupported content encoding")
                content = bytearray()
                async for chunk in upstream.aiter_bytes():
                    if len(content) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise ValueError("Response too large")
                    content.extend(chunk)
                raw_body = bytes(content)
                payload = inspect_response(raw_body, rpc.request_id)
                if "error" in payload:
                    return JSONResponse(
                        error_payload(
                            rpc.request_id, payload["error"]["code"], "Downstream RPC error"
                        )
                    )
                result = payload["result"]
                if (
                    rpc.method == "tools/call"
                    and isinstance(result, dict)
                    and result.get("isError") is True
                ):
                    return JSONResponse(
                        {
                            "jsonrpc": "2.0",
                            "id": rpc.request_id,
                            "result": {
                                "content": [
                                    {"type": "text", "text": "Downstream tool execution failed"}
                                ],
                                "isError": True,
                            },
                        }
                    )
                return Response(
                    raw_body, status_code=upstream.status_code, media_type="application/json"
                )
    except (TimeoutError, httpx.TimeoutException):
        logger.warning("Downstream request timed out")
        return failure(rpc, 504, -32003, "Downstream timeout")
    except (httpx.HTTPError, ValueError):
        logger.warning("Downstream request failed")
        return failure(rpc, 502, -32002, "Downstream unavailable")


def failure(rpc: RPCRequest, status: int, code: int, message: str) -> Response:
    if rpc.notification:
        return Response(status_code=status)
    return JSONResponse(error_payload(rpc.request_id, code, message), status_code=status)
