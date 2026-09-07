"""Deterministic SSE provider for Task 3; prompts are never echoed or logged."""

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Literal

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

RESPONSES = {
    "ordinary": "Welcome! Café greetings — all systems are ready.\n",
    "email": "Contact jordan.lee@example.com for the demo.\n",
    "ssn": "The synthetic SSN is 123-45-6789.\n",
    "card": "The synthetic card is 4111 1111 1111 1111.\n",
    "mixed": (
        "Welcome! Café support: jordan.lee@example.com; SSN: 123-45-6789; "
        "card: 4111 1111 1111 1111. Thank you.\n"
    ),
    "slow": "Welcome! The next words arrive gradually.\n",
}


class MockGenerationRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", hide_input_in_errors=True)

    prompt: str = Field(min_length=1, max_length=4000)
    scenario: Literal[
        "ordinary", "email", "ssn", "card", "mixed", "slow", "http_error", "malformed", "disconnect"
    ] = "mixed"
    delay_ms: int = Field(default=0, ge=0, le=1000)
    chunk_size: int = Field(default=7, ge=1, le=256)

    @field_validator("prompt")
    @classmethod
    def require_nonblank_prompt(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Prompt must not be blank")
        return value


def _event(delta: dict[str, str], finish_reason: str | None = None) -> bytes:
    payload = {"choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}]}
    return ("data: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode("utf-8")


def create_app() -> FastAPI:
    app = FastAPI(title="Task 3 mock LLM", docs_url=None, redoc_url=None)
    app.state.request_count = 0
    app.state.active_streams = 0
    app.state.completed_streams = 0
    app.state.cancelled_streams = 0
    app.state.aborted_streams = 0

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, error: RequestValidationError) -> JSONResponse:
        return JSONResponse({"error": "Invalid generation request"}, status_code=422)

    async def generate(request: MockGenerationRequest) -> AsyncIterator[bytes]:
        app.state.active_streams += 1
        try:
            yield _event({"role": "assistant"})
            if request.scenario == "malformed":
                yield b"data: {malformed-json}\n\n"
                app.state.aborted_streams += 1
                return
            if request.scenario == "disconnect":
                yield _event({"content": "Partial contact: jordan.lee@exam"})
                app.state.aborted_streams += 1
                return
            text = RESPONSES[request.scenario]
            delay_ms = request.delay_ms or (100 if request.scenario == "slow" else 0)
            for start in range(0, len(text), request.chunk_size):
                if delay_ms:
                    await asyncio.sleep(delay_ms / 1000)
                yield _event({"content": text[start : start + request.chunk_size]})
            yield _event({}, "stop")
            yield b"data: [DONE]\n\n"
            app.state.completed_streams += 1
        except (asyncio.CancelledError, GeneratorExit):
            app.state.cancelled_streams += 1
            raise
        finally:
            app.state.active_streams -= 1

    @app.post("/stream")
    async def stream(request: MockGenerationRequest) -> Response:
        # Count accepted requests; FastAPI rejects malformed inputs before this route.
        app.state.request_count += 1
        if request.scenario == "http_error":
            return JSONResponse({"error": "Mock provider unavailable"}, status_code=503)
        return StreamingResponse(
            generate(request),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    return app
