"""Real HTTP checks for incremental delivery, disconnects, and Uvicorn factories."""

import asyncio
import json
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, nullcontext
from typing import Any
from unittest.mock import patch

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.responses import StreamingResponse

from quilr_assessment.task3_stream_guardrail.app import create_app
from quilr_assessment.task3_stream_guardrail.config import Settings

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@asynccontextmanager
async def serve(app: FastAPI | str) -> AsyncIterator[str]:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host="127.0.0.1",
                port=port,
                factory=isinstance(app, str),
                ws="none",
                access_log=False,
                log_config=None,
                timeout_graceful_shutdown=1,
            )
        )
        # These servers share pytest's process; signal ownership stays with pytest.
        with patch.object(server, "capture_signals", nullcontext):
            task = asyncio.create_task(server.serve(sockets=[listener]))
            try:
                async with asyncio.timeout(5):
                    while not server.started:
                        if task.done():
                            await task
                            pytest.fail("Uvicorn exited before startup")
                        await asyncio.sleep(0.01)
                yield f"http://127.0.0.1:{port}"
            finally:
                server.should_exit = True
                await asyncio.wait_for(task, timeout=5)


def text_event(content: str, *, finish: bool = False) -> bytes:
    choice = {
        "index": 0,
        "delta": {"content": content},
        "finish_reason": "stop" if finish else None,
    }
    return ("data: " + json.dumps({"choices": [choice]}) + "\n\n").encode()


def gated_provider(
    release: asyncio.Event, waiting: asyncio.Event, closed: asyncio.Event, completed: asyncio.Event
) -> FastAPI:
    app = FastAPI()

    async def generate() -> AsyncIterator[bytes]:
        try:
            yield text_event("Welcome! ")
            waiting.set()
            await release.wait()
            for text in ("Contact jordan.", "lee@example.", "com for help.\n"):
                yield text_event(text)
            yield text_event("", finish=True)
            yield b"data: [DONE]\n\n"
            completed.set()
        finally:
            closed.set()

    @app.post("/stream")
    async def stream() -> StreamingResponse:
        return StreamingResponse(generate(), media_type="text/event-stream")

    return app


async def events(response: httpx.Response) -> AsyncIterator[dict[str, Any] | None]:
    async for line in response.aiter_lines():
        if line.startswith("data: "):
            data = line.removeprefix("data: ")
            if data == "[DONE]":
                yield None
            else:
                event = json.loads(data)
                assert "error" not in event
                yield event


def content(event: dict[str, Any] | None) -> str:
    if event is None or not event.get("choices"):
        return ""
    return event["choices"][0]["delta"].get("content") or ""


async def first_text(iterator: AsyncIterator[dict[str, Any] | None]) -> str:
    async with asyncio.timeout(3):
        async for event in iterator:
            if text := content(event):
                return text
    pytest.fail("Stream ended before returning any text")


async def test_real_http_emits_safe_text_while_provider_completion_is_gated() -> None:
    release, waiting, closed, completed = (asyncio.Event() for _ in range(4))
    provider = gated_provider(release, waiting, closed, completed)
    async with asyncio.timeout(20):
        async with serve(provider) as upstream:
            gateway = create_app(Settings(provider_url=f"{upstream}/stream"))
            try:
                async with serve(gateway) as base_url:
                    async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
                        async with client.stream(
                            "POST", f"{base_url}/generate", json={"prompt": "demo"}
                        ) as response:
                            assert response.status_code == 200
                            assert response.headers["content-type"].startswith("text/event-stream")
                            received = events(response)
                            text = await first_text(received)
                            assert "Welcome!" in text
                            assert waiting.is_set() and not release.is_set()
                            assert not completed.is_set()
                            # Completion is impossible until the client has observed safe text.
                            release.set()
                            done_count = 0
                            async for event in received:
                                text += content(event)
                                done_count += event is None
                            assert text == "Welcome! Contact [REDACTED] for help.\n"
                            assert done_count == 1
                        await asyncio.wait_for(closed.wait(), timeout=3)
                        assert completed.is_set()
            finally:
                release.set()


async def test_real_http_client_disconnect_closes_blocked_provider_generator() -> None:
    release, waiting, closed, completed = (asyncio.Event() for _ in range(4))
    provider = gated_provider(release, waiting, closed, completed)
    async with asyncio.timeout(20):
        async with serve(provider) as upstream:
            gateway = create_app(Settings(provider_url=f"{upstream}/stream"))
            try:
                async with serve(gateway) as base_url:
                    async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
                        async with client.stream(
                            "POST", f"{base_url}/generate", json={"prompt": "demo"}
                        ) as response:
                            assert response.status_code == 200
                            assert "Welcome!" in await first_text(events(response))
                            assert waiting.is_set() and not release.is_set()
                        # Closing the client stream must interrupt the still-blocked upstream.
                        await asyncio.wait_for(closed.wait(), timeout=3)
                        assert not release.is_set()
                        assert not completed.is_set()
            finally:
                release.set()


async def test_documented_factories_stream_ordinary_and_mixed_mock_scenarios(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with asyncio.timeout(20):
        async with serve("quilr_assessment.mocks.llm:create_app") as upstream:
            monkeypatch.setenv("QUILR_LLM_STREAM_URL", f"{upstream}/stream")
            async with serve("quilr_assessment.task3_stream_guardrail.app:create_app") as base_url:
                async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
                    for scenario, expected in (
                        ("ordinary", "Welcome! Café greetings — all systems are ready.\n"),
                        (
                            "mixed",
                            "Welcome! Café support: [REDACTED]; SSN: [REDACTED]; "
                            "card: [REDACTED]. Thank you.\n",
                        ),
                    ):
                        async with client.stream(
                            "POST",
                            f"{base_url}/generate",
                            json={
                                "prompt": "private-prompt-sentinel",
                                "scenario": scenario,
                                "chunk_size": 1,
                            },
                        ) as response:
                            assert response.status_code == 200
                            text = ""
                            done_count = 0
                            async for event in events(response):
                                text += content(event)
                                done_count += event is None
                            assert text == expected
                            assert done_count == 1
                            assert "private-prompt-sentinel" not in text
