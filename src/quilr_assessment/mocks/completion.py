"""Deterministic primary/secondary completion providers for Task 4.

Error bodies deliberately contain realistic upstream noise — paths, exception text
and a fake credential — so tests can assert the gateway never relays any of it.
"""

import asyncio
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..task4_model_router.tokens import count_tokens

COMPLETIONS = {
    "primary": "Primary model reply: your request was handled by the preferred provider.",
    "secondary": "Secondary model reply: the backup provider completed this request.",
}
UPSTREAM_LEAK = (
    'Traceback (most recent call last): File "/opt/models/serving/handler.py", line 84,'
    " in dispatch raise RuntimeError('pool exhausted at 10.4.2.7:8443')"
    " api_key=sk-mock-upstream-secret"
)
Scenario = Literal[
    "ok", "rate_limited", "slow", "server_error", "bad_request", "unauthorized", "malformed"
]


class MockCompletionRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", hide_input_in_errors=True)

    prompt: str = Field(min_length=1, max_length=8000)
    max_output_tokens: int = Field(ge=1, le=4096)
    scenario: Scenario = "ok"
    delay_ms: int = Field(default=0, ge=0, le=10_000)

    @field_validator("prompt")
    @classmethod
    def require_nonblank_prompt(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Prompt must not be blank")
        return value


def create_app() -> FastAPI:
    app = FastAPI(title="Task 4 mock model providers", docs_url=None, redoc_url=None)
    app.state.requests = {"primary": 0, "secondary": 0}
    app.state.completed = {"primary": 0, "secondary": 0}
    app.state.cancelled = {"primary": 0, "secondary": 0}

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, error: RequestValidationError) -> JSONResponse:
        return JSONResponse({"error": "Invalid completion request"}, status_code=422)

    async def handle(name: str, request: MockCompletionRequest) -> Response:
        app.state.requests[name] += 1
        try:
            if request.delay_ms or request.scenario == "slow":
                await asyncio.sleep((request.delay_ms or 5000) / 1000)
            if request.scenario == "rate_limited":
                return JSONResponse(
                    {"error": UPSTREAM_LEAK}, status_code=429, headers={"Retry-After": "30"}
                )
            if request.scenario == "server_error":
                return JSONResponse({"error": UPSTREAM_LEAK}, status_code=500)
            if request.scenario == "bad_request":
                return JSONResponse({"error": UPSTREAM_LEAK}, status_code=400)
            if request.scenario == "unauthorized":
                return JSONResponse({"error": UPSTREAM_LEAK}, status_code=401)
            if request.scenario == "malformed":
                return Response(
                    ("{not json " + UPSTREAM_LEAK).encode("utf-8"),
                    media_type="application/json",
                )
            text = COMPLETIONS[name]
            # The completion is fixed synthetic text; the prompt is never echoed.
            completion_tokens = min(count_tokens(text), request.max_output_tokens)
            app.state.completed[name] += 1
            return JSONResponse(
                {
                    "text": text,
                    "model": f"mock-{name}-001",
                    "usage": {
                        "prompt_tokens": count_tokens(request.prompt),
                        "completion_tokens": completion_tokens,
                    },
                }
            )
        except asyncio.CancelledError:
            app.state.cancelled[name] += 1
            raise

    @app.post("/primary/completions")
    async def primary(request: MockCompletionRequest) -> Response:
        return await handle("primary", request)

    @app.post("/secondary/completions")
    async def secondary(request: MockCompletionRequest) -> Response:
        return await handle("secondary", request)

    return app
