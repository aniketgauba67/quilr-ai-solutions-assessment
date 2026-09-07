"""Bounded request -> provider SSE -> text guardrail -> client SSE."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from http.cookiejar import CookieJar, DefaultCookiePolicy

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import ValidationError
from starlette.requests import ClientDisconnect
from starlette.types import Receive, Scope, Send

from .config import GenerationRequest, Settings, from_environment
from .provider import ProviderError, close_provider, open_provider, sanitized_events

MAX_REQUEST_BYTES = 32 * 1024
BODY_TIMEOUT_SECONDS = 5.0
logger = logging.getLogger(__name__)


class ProviderStreamingResponse(StreamingResponse):
    """Own upstream even when the client disconnects while ASGI is sending output."""

    def __init__(self, upstream: httpx.Response, timeout: float) -> None:
        self.upstream = upstream
        self.events = sanitized_events(upstream, timeout)
        super().__init__(
            self.events,
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            try:
                await self.events.aclose()
            finally:
                await close_provider(self.upstream)


def error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


def create_app(
    settings: Settings | None = None, *, transport: httpx.AsyncBaseTransport | None = None
) -> FastAPI:
    settings = settings if settings is not None else from_environment()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(
                settings.timeout_seconds, connect=min(5, settings.timeout_seconds)
            ),
            trust_env=False,
            follow_redirects=False,
            cookies=CookieJar(policy=DefaultCookiePolicy(allowed_domains=())),
        ) as client:
            app.state.provider = client
            yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.post("/generate")
    async def generate(request: Request) -> Response:
        if (
            request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            != "application/json"
            or request.headers.get("content-encoding", "identity").lower() != "identity"
        ):
            return error(415, "unsupported_media_type", "Use uncompressed application/json")
        body = bytearray()
        try:
            async with asyncio.timeout(BODY_TIMEOUT_SECONDS):
                async for chunk in request.stream():
                    if len(body) + len(chunk) > MAX_REQUEST_BYTES:
                        return error(413, "request_too_large", "Request too large")
                    body.extend(chunk)
            generation = GenerationRequest.model_validate_json(body)
        except (ValidationError, ValueError):
            return error(422, "invalid_request", "Invalid generation request")
        except TimeoutError:
            return error(408, "request_timeout", "Request body timeout")
        except ClientDisconnect:
            return Response(status_code=400)
        try:
            upstream = await open_provider(
                app.state.provider,
                str(settings.provider_url),
                generation.model_dump(),
                settings.timeout_seconds,
            )
        except ProviderError as exc:
            logger.warning("Guardrail provider request failed")
            return error(
                504 if exc.timeout else 502,
                "upstream_timeout" if exc.timeout else "upstream_unavailable",
                "Provider unavailable",
            )
        return ProviderStreamingResponse(upstream, settings.timeout_seconds)

    return app
