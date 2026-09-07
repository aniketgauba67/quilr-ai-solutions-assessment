import asyncio
import json
from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from quilr_assessment.mocks import llm

PROMPT = "private-prompt-sentinel"


@pytest.fixture
def mock_app() -> FastAPI:
    return llm.create_app()


@pytest_asyncio.fixture
async def client(mock_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_app), base_url="http://mock-llm"
    ) as client:
        yield client


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["ordinary", "email", "ssn", "card", "mixed"])
@pytest.mark.parametrize("chunk_size", [1, 7, 256])
async def test_mock_emits_incremental_sse_deltas_with_finish_and_done(
    client: httpx.AsyncClient, mock_app: FastAPI, scenario: str, chunk_size: int
) -> None:
    async with asyncio.timeout(3):
        response = await client.post(
            "/stream", json={"prompt": PROMPT, "scenario": scenario, "chunk_size": chunk_size}
        )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-store"
    frames = response.text.rstrip("\n").split("\n\n")
    assert frames[-1] == "data: [DONE]"
    choices = [json.loads(frame.removeprefix("data: "))["choices"][0] for frame in frames[:-1]]
    assert choices[0] == {"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}
    assert choices[-1] == {"index": 0, "delta": {}, "finish_reason": "stop"}
    deltas = [choice["delta"]["content"] for choice in choices[1:-1]]
    assert all(1 <= len(delta) <= chunk_size for delta in deltas)
    assert all(choice["index"] == 0 and choice["finish_reason"] is None for choice in choices[1:-1])
    assert "".join(deltas) == llm.RESPONSES[scenario]
    assert PROMPT not in response.text
    assert mock_app.state.request_count == 1
    assert mock_app.state.completed_streams == 1
    assert mock_app.state.active_streams == 0


@pytest.mark.asyncio
async def test_mock_defaults_are_deterministic_and_do_not_echo_prompts(
    client: httpx.AsyncClient,
) -> None:
    first = await client.post("/stream", json={"prompt": PROMPT})
    second = await client.post("/stream", json={"prompt": "another private prompt"})
    assert first.content == second.content
    assert b"Caf\xc3\xa9" in first.content
    assert PROMPT not in first.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scenario", "delay_ms", "expected"), [("slow", 0, 0.1), ("mixed", 2, 0.002)]
)
async def test_mock_slow_scenario_and_configurable_delay(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
    delay_ms: int,
    expected: float,
) -> None:
    delays: list[float] = []

    async def record_delay(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(llm.asyncio, "sleep", record_delay)
    response = await client.post(
        "/stream",
        json={"prompt": PROMPT, "scenario": scenario, "delay_ms": delay_ms, "chunk_size": 7},
    )
    assert response.status_code == 200
    assert delays == [expected] * ((len(llm.RESPONSES[scenario]) + 6) // 7)


@pytest.mark.asyncio
async def test_mock_http_error_is_sanitized_without_starting_stream(
    client: httpx.AsyncClient, mock_app: FastAPI
) -> None:
    response = await client.post("/stream", json={"prompt": PROMPT, "scenario": "http_error"})
    assert response.status_code == 503
    assert response.json() == {"error": "Mock provider unavailable"}
    assert mock_app.state.request_count == 1
    assert mock_app.state.active_streams == mock_app.state.completed_streams == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["malformed", "disconnect"])
async def test_mock_bad_stream_scenarios_end_without_success_terminal(
    client: httpx.AsyncClient, mock_app: FastAPI, scenario: str
) -> None:
    response = await client.post("/stream", json={"prompt": PROMPT, "scenario": scenario})
    assert response.status_code == 200
    assert '"role": "assistant"' in response.text
    assert "[DONE]" not in response.text
    assert '"finish_reason": "stop"' not in response.text
    assert PROMPT not in response.text
    if scenario == "malformed":
        assert "data: {malformed-json}\n\n" in response.text
    else:
        assert "jordan.lee@exam" in response.text
    assert mock_app.state.aborted_streams == 1
    assert mock_app.state.active_streams == mock_app.state.completed_streams == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"prompt": ""},
        {"prompt": " \t\n "},
        {"prompt": 123},
        {"prompt": "x" * 4001},
        {"prompt": PROMPT, "scenario": "unknown"},
        {"prompt": PROMPT, "delay_ms": -1},
        {"prompt": PROMPT, "delay_ms": 1001},
        {"prompt": PROMPT, "delay_ms": True},
        {"prompt": PROMPT, "delay_ms": "1"},
        {"prompt": PROMPT, "chunk_size": 0},
        {"prompt": PROMPT, "chunk_size": 257},
        {"prompt": PROMPT, "chunk_size": False},
        {"prompt": PROMPT, "unexpected": "private-input-sentinel"},
    ],
)
async def test_mock_rejects_invalid_inputs_without_echoing_them(
    client: httpx.AsyncClient, mock_app: FastAPI, payload: object
) -> None:
    response = await client.post("/stream", json=payload)
    assert response.status_code == 422
    assert response.json() == {"error": "Invalid generation request"}
    assert mock_app.state.request_count == 0
    assert mock_app.state.active_streams == 0


@pytest.mark.asyncio
async def test_mock_malformed_json_is_sanitized(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/stream",
        content=b'{"prompt":"private-input-sentinel',
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert response.json() == {"error": "Invalid generation request"}


@pytest.mark.asyncio
async def test_mock_cancellation_releases_active_stream(
    client: httpx.AsyncClient, mock_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    reached_delay = asyncio.Event()
    release = asyncio.Event()

    async def wait_for_release(delay: float) -> None:
        reached_delay.set()
        await release.wait()

    monkeypatch.setattr(llm.asyncio, "sleep", wait_for_release)
    task = asyncio.create_task(client.post("/stream", json={"prompt": PROMPT, "scenario": "slow"}))
    try:
        async with asyncio.timeout(3):
            await reached_delay.wait()
            assert mock_app.state.active_streams == 1
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert mock_app.state.active_streams == 0
    assert mock_app.state.cancelled_streams == 1
    assert mock_app.state.completed_streams == 0
