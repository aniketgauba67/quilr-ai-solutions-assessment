"""Bounded request -> tenant fingerprint -> quota admission -> primary/secondary routing."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from http.cookiejar import CookieJar, DefaultCookiePolicy

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError
from starlette.requests import ClientDisconnect

from .config import Settings, from_environment
from .errors import (
    INTERNAL_ERROR,
    INVALID_REQUEST,
    STATUS,
    UNAUTHORIZED,
    GatewayError,
    error_payload,
)
from .limiter import TokenWindowLimiter
from .router import ModelRouter
from .schemas import CompletionRequest
from .tenants import TenantError, fingerprint, resolve_tenant

MAX_REQUEST_BYTES = 32 * 1024
BODY_TIMEOUT_SECONDS = 5.0
logger = logging.getLogger(__name__)


def error(code: str, status: int | None = None) -> JSONResponse:
    return JSONResponse(error_payload(code), status_code=status or STATUS[code])


def create_app(
    settings: Settings | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    limiter: TokenWindowLimiter | None = None,
) -> FastAPI:
    settings = settings if settings is not None else from_environment()
    quota = (
        limiter
        if limiter is not None
        else TokenWindowLimiter(
            settings.database_path,
            token_budget=settings.token_budget,
            window_seconds=settings.window_seconds,
            timeout_seconds=settings.database_timeout_seconds,
        )
    )
    allowlist = (
        tuple(
            fingerprint(key.get_secret_value(), pepper=settings.pepper)
            for key in settings.tenant_api_keys
        )
        or None
    )

    # asyncio.timeout owns each attempt's deadline; the client must never preempt
    # the longer of the two, or a slow secondary would fail early.
    client_timeout = max(settings.primary_timeout_seconds, settings.secondary_timeout_seconds)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await quota.initialize()
        async with httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(client_timeout, connect=min(5.0, client_timeout)),
            trust_env=False,
            follow_redirects=False,
            cookies=CookieJar(policy=DefaultCookiePolicy(allowed_domains=())),
        ) as client:
            app.state.router = ModelRouter(client, settings, quota)
            yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.limiter = quota

    @app.exception_handler(Exception)
    async def unexpected_failure(request: Request, exc: Exception) -> Response:
        # Replaces Starlette's default 500 so no handler defect can shape the body.
        # Like the other tasks this logs a fixed diagnostic, never a traceback whose
        # frames could carry request data.
        logger.error("Unhandled gateway failure")
        return error(INTERNAL_ERROR)

    @app.post("/v1/completions")
    async def completions(request: Request) -> Response:
        if (
            request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            != "application/json"
            or request.headers.get("content-encoding", "identity").lower() != "identity"
        ):
            return error(INVALID_REQUEST, 415)
        try:
            tenant = resolve_tenant(
                request.headers.getlist("authorization"),
                pepper=settings.pepper,
                allowlist=allowlist,
            )
        except TenantError:
            return JSONResponse(
                error_payload(UNAUTHORIZED),
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        body = bytearray()
        try:
            async with asyncio.timeout(BODY_TIMEOUT_SECONDS):
                async for chunk in request.stream():
                    if len(body) + len(chunk) > MAX_REQUEST_BYTES:
                        return error(INVALID_REQUEST, 413)
                    body.extend(chunk)
            completion = CompletionRequest.model_validate_json(bytes(body))
        except (ValidationError, ValueError):
            return error(INVALID_REQUEST)
        except TimeoutError:
            return error(INVALID_REQUEST, 408)
        except ClientDisconnect:
            return Response(status_code=400)
        try:
            result = await app.state.router.complete(tenant, completion)
        except GatewayError as exc:
            return JSONResponse(exc.payload(), status_code=exc.status)
        return JSONResponse(result.model_dump())

    return app
