import asyncio
import json
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

import httpx
import pytest
from pydantic import ValidationError

from quilr_assessment.mocks.llm import create_app as create_mock
from quilr_assessment.task3_stream_guardrail import app as gateway
from quilr_assessment.task3_stream_guardrail import provider
from quilr_assessment.task3_stream_guardrail.config import Settings, from_environment

SENTINEL = "private-sentinel /private/server.py https://internal.invalid API_KEY=private"
PII = ("jordan.lee@example.com", "123-45-6789", "4111 1111 1111 1111")


def event(text: str | None = None, **fields: object) -> bytes:
    delta = {} if text is None else {"content": text}
    value = {"choices": [{"index": 0, "delta": delta}], **fields}
    return ("data: " + json.dumps(value, ensure_ascii=False) + "\n\n").encode()


class ByteStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes], *, failure: Exception | None = None) -> None:
        self.chunks = chunks
        self.failure = failure
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            yield chunk
        if self.failure is not None:
            raise self.failure

    async def aclose(self) -> None:
        self.closed = True


@asynccontextmanager
async def client_for(
    handler: Callable | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    timeout: float = 1,
) -> AsyncIterator[tuple[httpx.AsyncClient, object]]:
    selected = transport if transport is not None else httpx.MockTransport(handler)
    app = gateway.create_app(Settings(timeout_seconds=timeout), transport=selected)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://gateway"
        ) as client:
            yield client, app
    assert app.state.provider.is_closed


def output_events(response: httpx.Response) -> list[dict]:
    return [
        json.loads(line[6:])
        for line in response.text.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]


def output_text(response: httpx.Response) -> str:
    return "".join(
        choice.get("delta", {}).get("content") or ""
        for value in output_events(response)
        for choice in value.get("choices", [])
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["ordinary", "email", "ssn", "card", "mixed"])
@pytest.mark.parametrize("chunk_size", [1, 7, 256])
async def test_mock_through_gateway_redacts_all_supported_pii(
    scenario: str, chunk_size: int, caplog: pytest.LogCaptureFixture
) -> None:
    mock = create_mock()
    with caplog.at_level(logging.INFO):
        async with client_for(transport=httpx.ASGITransport(mock)) as (client, _):
            response = await client.post(
                "/generate",
                json={"prompt": SENTINEL, "scenario": scenario, "chunk_size": chunk_size},
            )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-accel-buffering"] == "no"
    assert response.text.endswith("data: [DONE]\n\n")
    counts = {"ordinary": 0, "email": 1, "ssn": 1, "card": 1, "mixed": 3}
    assert output_text(response).count("[REDACTED]") == counts[scenario]
    for sensitive in (*PII, SENTINEL):
        assert sensitive not in response.text + caplog.text
    assert mock.state.request_count == mock.state.completed_streams == 1
    assert mock.state.active_streams == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("network_size", [1, 2, 7, 10_000])
async def test_arbitrary_network_splits_and_metadata_order(network_size: int) -> None:
    metadata = {"id": "mock-1", "model": "demo", "created": 42}
    wire = (
        event("Café! Contact john.", **metadata)
        + event(None, **metadata)
        + event("doe@", **metadata)
        + event("example.com", **metadata)
        + b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
        + b'data: {"choices":[],"usage":{"total_tokens":12}}\n\n'
        + b"data: [DONE]\n\n"
    )
    stream = ByteStream([wire[n : n + network_size] for n in range(0, len(wire), network_size)])

    def upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream, headers={"Content-Type": "text/event-stream"})

    async with client_for(upstream) as (client, _):
        response = await client.post("/generate", json={"prompt": "test"})
    assert output_text(response) == "Café! Contact [REDACTED]"
    events = output_events(response)
    assert {k: events[0][k] for k in metadata} == metadata
    assert events[-2]["choices"][0]["finish_reason"] == "stop"
    assert events[-1]["usage"] == {"total_tokens": 12}
    assert stream.closed


@pytest.mark.asyncio
async def test_done_flushes_final_text_without_finish_event_and_ignores_trailing_bytes() -> None:
    stream = ByteStream([event("Last word") + b"data: [DONE]\n\n" + event(PII[0])])

    def upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream, headers={"Content-Type": "text/event-stream"})

    async with client_for(upstream) as (client, _):
        response = await client.post("/generate", json={"prompt": "test"})
    assert output_text(response) == "Last word"
    assert PII[0] not in response.text
    assert response.text.count("data: [DONE]") == 1
    assert stream.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["status", "redirect", "media", "encoding", "connect", "timeout"])
async def test_initial_provider_failures_are_sanitized(
    case: str, caplog: pytest.LogCaptureFixture
) -> None:
    stream = ByteStream([SENTINEL.encode()])

    def upstream(request: httpx.Request) -> httpx.Response:
        if case == "connect":
            raise httpx.ConnectError(SENTINEL)
        if case == "timeout":
            raise httpx.ReadTimeout(SENTINEL)
        status = {"status": 503, "redirect": 307}.get(case, 200)
        headers = {"Content-Type": "text/event-stream", "Location": "http://internal.invalid"}
        if case == "media":
            headers["Content-Type"] = "text/html"
        if case == "encoding":
            headers["Content-Encoding"] = "gzip"
        return httpx.Response(status, stream=stream, headers=headers)

    with caplog.at_level(logging.INFO):
        async with client_for(upstream) as (client, _):
            response = await client.post("/generate", json={"prompt": "test"})
    assert response.status_code == (504 if case == "timeout" else 502)
    assert response.json()["error"]["message"] == "Provider unavailable"
    assert SENTINEL not in response.text + caplog.text
    assert "Traceback" not in caplog.text
    if case not in ("connect", "timeout"):
        assert stream.closed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_tail",
    [
        b"data: {private-sentinel}\n\n",
        b"data: \xff\n\n",
        b"data: " + b"x" * 65_536,
        b'data: {"error":{"message":"private-sentinel"}}\n\n',
        b'data: {"choices":[{"index":0,"delta":{"tool_calls":[]}}]}\n\n',
        b"event: error\ndata: [DONE]\n\n",
        b"data: {unfinished",
        b"",
    ],
)
async def test_malformed_or_truncated_stream_discards_pending_pii_and_sanitizes_error(
    bad_tail: bytes, caplog: pytest.LogCaptureFixture
) -> None:
    stream = ByteStream([event("Contact john.doe@exam"), bad_tail])

    def upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream, headers={"Content-Type": "text/event-stream"})

    with caplog.at_level(logging.INFO):
        async with client_for(upstream) as (client, _):
            response = await client.post("/generate", json={"prompt": SENTINEL})
    assert response.status_code == 200
    assert "event: error\n" in response.text
    assert output_events(response)[-1] == {
        "error": {"code": "upstream_stream_error", "message": "Provider stream unavailable"}
    }
    assert output_text(response) == "Contact "
    assert "[DONE]" not in response.text
    for private in ("john.doe", "private-sentinel", "Traceback", "internal.invalid"):
        assert private not in response.text + caplog.text
    assert stream.closed


@pytest.mark.asyncio
async def test_transport_failure_after_output_closes_upstream() -> None:
    stream = ByteStream([event("Hello! john@")], failure=httpx.ReadError(SENTINEL))

    def upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream, headers={"Content-Type": "text/event-stream"})

    async with client_for(upstream) as (client, _):
        response = await client.post("/generate", json={"prompt": "test"})
    assert output_text(response) == "Hello! "
    assert "upstream_stream_error" in response.text
    assert SENTINEL not in response.text
    assert stream.closed


@pytest.mark.asyncio
async def test_idle_deadline_cancels_provider_read_and_closes_stream() -> None:
    cancelled = asyncio.Event()

    class StalledStream(ByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield event("Hello! john@")
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    stream = StalledStream([])

    def upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream, headers={"Content-Type": "text/event-stream"})

    async with asyncio.timeout(2), client_for(upstream, timeout=0.01) as (client, _):
        response = await client.post("/generate", json={"prompt": "test"})
    assert "upstream_timeout" in response.text
    assert output_text(response) == "Hello! "
    assert cancelled.is_set() and stream.closed


@pytest.mark.asyncio
async def test_caller_cancellation_closes_upstream_without_error_output() -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    class StalledStream(ByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
            yield b""

    stream = StalledStream([])

    def upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream, headers={"Content-Type": "text/event-stream"})

    async with asyncio.timeout(2), client_for(upstream) as (client, _):
        task = asyncio.create_task(client.post("/generate", json={"prompt": "test"}))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert cancelled.is_set() and stream.closed


@pytest.mark.asyncio
async def test_no_caller_headers_or_persistent_cookies_cross_provider_boundary() -> None:
    seen = []

    def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            stream=ByteStream([event("Hello!") + b"data: [DONE]\n\n"]),
            headers={"Content-Type": "text/event-stream", "Set-Cookie": "session=secret; Path=/"},
        )

    async with client_for(upstream) as (client, app):
        for _ in range(2):
            response = await client.post(
                "/generate",
                json={"prompt": "test"},
                headers={
                    "Authorization": "Bearer private",
                    "Cookie": "secret=1",
                    "X-Key": "private",
                },
            )
            assert "set-cookie" not in response.headers
        assert not list(app.state.provider.cookies.jar)
    assert len(seen) == 2
    for request in seen:
        assert all(name not in request.headers for name in ("authorization", "cookie", "x-key"))
        assert request.headers["accept"] == "text/event-stream"
        assert request.headers["accept-encoding"] == "identity"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {},
        {"prompt": " "},
        {"prompt": 42},
        {"prompt": "x" * 4001},
        {"prompt": "test", "scenario": "unknown"},
        {"prompt": "test", "delay_ms": True},
        {"prompt": "test", "chunk_size": 0},
        {"prompt": "test", "private-sentinel": "extra"},
    ],
)
async def test_invalid_requests_are_rejected_before_provider(body: dict) -> None:
    def upstream(request: httpx.Request) -> httpx.Response:
        pytest.fail("Invalid request reached provider")

    async with client_for(upstream) as (client, _):
        response = await client.post("/generate", json=body)
    assert response.status_code == 422
    assert response.json()["error"]["message"] == "Invalid generation request"
    assert "private-sentinel" not in response.text


@pytest.mark.parametrize("url", ["file:///private/file", "http://key@host", "http://host?q=secret"])
def test_configuration_rejects_credential_or_non_http_urls(url: str) -> None:
    with pytest.raises(ValidationError):
        Settings(provider_url=url)


def test_environment_configuration_and_sanitized_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QUILR_LLM_STREAM_URL", "http://127.0.0.1:9876/stream")
    monkeypatch.setenv("QUILR_LLM_TIMEOUT_SECONDS", "2")
    assert str(from_environment().provider_url) == "http://127.0.0.1:9876/stream"
    assert from_environment().timeout_seconds == 2
    monkeypatch.setenv("QUILR_LLM_TIMEOUT_SECONDS", SENTINEL)
    with pytest.raises(ValueError) as caught:
        from_environment()
    assert SENTINEL not in str(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 503])
async def test_provider_close_failure_does_not_escape_or_leak(
    status: int, caplog: pytest.LogCaptureFixture
) -> None:
    class BrokenClose(ByteStream):
        async def aclose(self) -> None:
            self.closed = True
            raise RuntimeError(SENTINEL)

    stream = BrokenClose([event("Hello!") + b"data: [DONE]\n\n"])

    def upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, stream=stream, headers={"Content-Type": "text/event-stream"})

    with caplog.at_level(logging.WARNING):
        async with client_for(upstream) as (client, _):
            response = await client.post("/generate", json={"prompt": "test"})
    assert response.status_code == (200 if status == 200 else 502)
    assert stream.closed
    assert "Guardrail provider cleanup failed" in caplog.text
    assert SENTINEL not in response.text + caplog.text
    assert "Traceback" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body,headers,status",
    [
        (b"{private-sentinel", {"Content-Type": "application/json"}, 422),
        (b'{"prompt":"\\ud800"}', {"Content-Type": "application/json"}, 422),
        (b"x" * (gateway.MAX_REQUEST_BYTES + 1), {"Content-Type": "application/json"}, 413),
        (b"{}", {"Content-Type": "text/plain"}, 415),
        (b"{}", {"Content-Type": "application/json", "Content-Encoding": "gzip"}, 415),
    ],
)
async def test_http_body_validation_has_safe_errors_and_zero_provider_calls(
    body: bytes, headers: dict[str, str], status: int
) -> None:
    def upstream(request: httpx.Request) -> httpx.Response:
        pytest.fail("Invalid HTTP body reached provider")

    async with client_for(upstream) as (client, _):
        response = await client.post("/generate", content=body, headers=headers)
    assert response.status_code == status
    assert "private-sentinel" not in response.text


@pytest.mark.asyncio
async def test_initial_provider_deadline_cancels_pending_request() -> None:
    cancelled = asyncio.Event()

    async def upstream(request: httpx.Request) -> httpx.Response:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async with asyncio.timeout(2), client_for(upstream, timeout=0.01) as (client, _):
        response = await client.post("/generate", json={"prompt": "test"})
    assert response.status_code == 504
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_slow_request_body_is_cancelled_before_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gateway, "BODY_TIMEOUT_SECONDS", 0.01)
    cancelled = asyncio.Event()

    async def body() -> AsyncIterator[bytes]:
        try:
            await asyncio.Event().wait()
            yield b"{}"
        finally:
            cancelled.set()

    def upstream(request: httpx.Request) -> httpx.Response:
        pytest.fail("Timed-out body reached provider")

    async with asyncio.timeout(2), client_for(upstream) as (client, _):
        response = await client.post(
            "/generate", content=body(), headers={"Content-Type": "application/json"}
        )
    assert response.status_code == 408
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_provider_cleanup_timeout_cancels_stalled_close(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(provider, "CLOSE_TIMEOUT_SECONDS", 0.01)
    cancelled = asyncio.Event()

    class StalledClose(ByteStream):
        async def aclose(self) -> None:
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    def upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, stream=StalledClose([]))

    with caplog.at_level(logging.WARNING):
        async with asyncio.timeout(2), client_for(upstream) as (client, _):
            response = await client.post("/generate", json={"prompt": "test"})
    assert response.status_code == 502
    assert cancelled.is_set()
    assert "Guardrail provider cleanup failed" in caplog.text


@pytest.mark.asyncio
async def test_cleanup_preserves_caller_cancellation() -> None:
    class CancelledClose(ByteStream):
        async def aclose(self) -> None:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await provider.close_provider(httpx.Response(200, stream=CancelledClose([])))
