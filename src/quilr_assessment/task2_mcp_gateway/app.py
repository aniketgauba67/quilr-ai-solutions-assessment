"""HTTP credentials -> RPC inspection -> tool policy -> downstream HTTP."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from http.cookiejar import CookieJar, DefaultCookiePolicy

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.requests import ClientDisconnect

from .auth import AuthenticationError, authenticate
from .config import Settings, from_environment
from .policy import may_call_tool
from .proxy import forward
from .rpc import RPCError, RPCRequest, error_payload, inspect_request

MAX_REQUEST_BYTES = 64 * 1024
BODY_TIMEOUT_SECONDS = 5.0
logger = logging.getLogger(__name__)


def http_error(status: int, code: str, message: str) -> JSONResponse:
    headers = {"WWW-Authenticate": "Bearer"} if status == 401 else None
    return JSONResponse(
        {"error": {"code": code, "message": message}}, status_code=status, headers=headers
    )


async def read_body(request: Request) -> bytes:
    content = bytearray()
    async with asyncio.timeout(BODY_TIMEOUT_SECONDS):
        async for chunk in request.stream():
            if len(content) + len(chunk) > MAX_REQUEST_BYTES:
                raise ValueError("Request too large")
            content.extend(chunk)
    return bytes(content)


def create_app(
    settings: Settings | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    settings = settings if settings is not None else from_environment()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # A shared connection pool must not become a cross-caller cookie session.
        cookies = CookieJar(policy=DefaultCookiePolicy(allowed_domains=()))
        async with httpx.AsyncClient(
            transport=transport,
            timeout=settings.timeout_seconds,
            trust_env=False,
            follow_redirects=False,
            cookies=cookies,
        ) as client:
            app.state.downstream = client
            yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.post("/mcp")
    async def gateway(request: Request) -> Response:
        try:
            role = authenticate(
                request.headers.getlist("authorization"),
                admin_token=settings.admin_token.get_secret_value(),
                viewer_token=settings.viewer_token.get_secret_value(),
            )
        except AuthenticationError:
            logger.warning("Gateway authentication rejected")
            return http_error(401, "unauthenticated", "Authentication required")

        if (
            request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            != "application/json"
        ):
            return http_error(415, "unsupported_media_type", "Use application/json")
        if request.headers.get("content-encoding", "identity").lower() != "identity":
            return http_error(415, "unsupported_encoding", "Use an uncompressed JSON body")
        try:
            body = await read_body(request)
        except ValueError:
            return http_error(413, "request_too_large", "Request too large")
        except TimeoutError:
            return http_error(408, "request_timeout", "Request body timeout")
        except ClientDisconnect:
            return Response(status_code=400)

        rpc: RPCRequest | None = None
        try:
            rpc = inspect_request(body)
            if rpc.tool_name is not None and not may_call_tool(role, rpc.tool_name):
                logger.info("Protected tool call denied")
                if rpc.notification:
                    return Response(status_code=204)
                return JSONResponse(error_payload(rpc.request_id, -32001, "Unauthorized Tool Call"))
            return await forward(
                app.state.downstream,
                str(settings.downstream_url),
                body,
                rpc,
                settings.timeout_seconds,
            )
        except RPCError as exc:
            if exc.notification:
                return Response(status_code=204)
            return JSONResponse(
                error_payload(exc.request_id, exc.code, exc.message), status_code=400
            )
        except Exception:
            logger.error("Gateway request failed")
            if rpc is not None and rpc.notification:
                return Response(status_code=500)
            return JSONResponse(
                error_payload(rpc.request_id if rpc else None, -32603, "Internal error"),
                status_code=500,
            )

    return app
