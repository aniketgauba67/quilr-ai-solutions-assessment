"""Provider boundary: deadlines, failure classification and payload sanitization."""

import asyncio
import gzip
import json
import logging

import httpx
import pytest

from quilr_assessment.task4_model_router.providers import (
    FALLBACK_FAILURES,
    MAX_RESPONSE_BYTES,
    Completion,
    ProviderError,
    complete,
)

pytestmark = pytest.mark.asyncio

SENTINEL = "sk-upstream-secret /opt/models/handler.py Traceback"
PAYLOAD = {"prompt": "hello", "max_output_tokens": 16, "scenario": "ok", "delay_ms": 0}
GOOD_BODY = {"text": "reply", "usage": {"prompt_tokens": 3, "completion_tokens": 5}}


def client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def responder(*, status: int = 200, body: object = None, headers: dict[str, str] | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status,
            json=GOOD_BODY if body is None else body,
            headers=headers or {},
        )

    return handler


async def test_successful_call_returns_normalized_usage() -> None:
    async with client(responder()) as http:
        result = await complete(
            http, name="primary", url="http://provider/x", payload=PAYLOAD, timeout=5
        )
    assert result == Completion("primary", "reply", 3, 5)
    assert result.total_tokens == 8


async def test_request_carries_the_payload_and_no_caller_credentials() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=GOOD_BODY)

    async with client(handler) as http:
        await complete(http, name="primary", url="http://provider/x", payload=PAYLOAD, timeout=5)
    assert json.loads(seen[0].content) == PAYLOAD
    assert seen[0].headers["accept-encoding"] == "identity"
    assert "authorization" not in seen[0].headers
    assert "cookie" not in seen[0].headers


@pytest.mark.parametrize(
    "status,failure",
    [
        (429, "rate_limited"),
        (400, "http_error"),
        (401, "http_error"),
        (403, "http_error"),
        (404, "http_error"),
        (500, "http_error"),
        (503, "http_error"),
        (301, "http_error"),
    ],
)
async def test_status_codes_map_to_documented_failures(status: int, failure: str) -> None:
    async with client(responder(status=status, body={"error": SENTINEL})) as http:
        with pytest.raises(ProviderError) as raised:
            await complete(
                http, name="primary", url="http://provider/x", payload=PAYLOAD, timeout=5
            )
    assert raised.value.failure == failure
    assert SENTINEL not in str(raised.value)
    assert raised.value.name == "primary"


async def test_only_rate_limiting_and_timeouts_are_fallback_triggers() -> None:
    assert FALLBACK_FAILURES == {"rate_limited", "timeout"}


async def test_deadline_covers_the_whole_attempt_and_closes_the_connection() -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        started.set()
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return httpx.Response(200, json=GOOD_BODY)

    async with client(handler) as http:
        with pytest.raises(ProviderError) as raised:
            await complete(
                http, name="primary", url="http://provider/x", payload=PAYLOAD, timeout=0.05
            )
    assert raised.value.failure == "timeout"
    assert started.is_set()
    assert cancelled.is_set()


async def test_slow_body_also_hits_the_total_deadline() -> None:
    async def stream():
        yield b'{"text": "partial"'
        await asyncio.sleep(10)
        yield b"}"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"Content-Type": "application/json"}, content=stream())

    async with client(handler) as http:
        with pytest.raises(ProviderError) as raised:
            await complete(
                http, name="primary", url="http://provider/x", payload=PAYLOAD, timeout=0.05
            )
    assert raised.value.failure == "timeout"


async def test_caller_cancellation_is_not_converted_into_a_provider_failure() -> None:
    entered = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        entered.set()
        await asyncio.sleep(10)
        return httpx.Response(200, json=GOOD_BODY)

    async with client(handler) as http:
        task = asyncio.create_task(
            complete(http, name="primary", url="http://provider/x", payload=PAYLOAD, timeout=30)
        )
        await asyncio.wait_for(entered.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_transport_failures_are_classified_without_leaking_details() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"connection refused to {SENTINEL}")

    async with client(handler) as http:
        with pytest.raises(ProviderError) as raised:
            await complete(
                http, name="secondary", url="http://provider/x", payload=PAYLOAD, timeout=5
            )
    assert raised.value.failure == "transport"
    assert SENTINEL not in str(raised.value)


@pytest.mark.parametrize(
    "body,headers",
    [
        (b"{not json}", {"Content-Type": "application/json"}),
        (b'{"text": "hi"}', {"Content-Type": "application/json"}),
        (
            b'{"usage": {"prompt_tokens": 1, "completion_tokens": 1}}',
            {"Content-Type": "application/json"},
        ),
        (
            b'{"text": 5, "usage": {"prompt_tokens": 1, "completion_tokens": 1}}',
            {"Content-Type": "application/json"},
        ),
        (
            b'{"text": "hi", "usage": {"prompt_tokens": -1, "completion_tokens": 1}}',
            {"Content-Type": "application/json"},
        ),
        (
            b'{"text": "hi", "usage": {"prompt_tokens": 1, "completion_tokens": 1}}',
            {"Content-Type": "text/plain"},
        ),
        (b"[]", {"Content-Type": "application/json"}),
        (b"", {"Content-Type": "application/json"}),
    ],
)
async def test_unsupported_payloads_are_rejected(body: bytes, headers: dict[str, str]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, headers=headers)

    async with client(handler) as http:
        with pytest.raises(ProviderError) as raised:
            await complete(
                http, name="primary", url="http://provider/x", payload=PAYLOAD, timeout=5
            )
    assert raised.value.failure == "invalid_response"


async def test_compressed_responses_are_rejected_even_when_decodable() -> None:
    body = gzip.compress(json.dumps(GOOD_BODY).encode())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=body,
            headers={"Content-Type": "application/json", "Content-Encoding": "gzip"},
        )

    async with client(handler) as http:
        with pytest.raises(ProviderError) as raised:
            await complete(
                http, name="primary", url="http://provider/x", payload=PAYLOAD, timeout=5
            )
    assert raised.value.failure == "invalid_response"


async def test_oversized_responses_are_rejected_without_buffering_them_all() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        text = "a" * (MAX_RESPONSE_BYTES + 1024)
        return httpx.Response(
            200,
            content=json.dumps({"text": text, "usage": GOOD_BODY["usage"]}).encode(),
            headers={"Content-Type": "application/json"},
        )

    async with client(handler) as http:
        with pytest.raises(ProviderError) as raised:
            await complete(
                http, name="primary", url="http://provider/x", payload=PAYLOAD, timeout=5
            )
    assert raised.value.failure == "invalid_response"


async def test_unknown_provider_metadata_is_dropped_rather_than_forwarded() -> None:
    body = {
        "text": "reply",
        "usage": {"prompt_tokens": 1, "completion_tokens": 2, "internal_cost": 9},
        "debug": {"host": SENTINEL},
    }
    async with client(responder(body=body)) as http:
        result = await complete(
            http, name="primary", url="http://provider/x", payload=PAYLOAD, timeout=5
        )
    assert result == Completion("primary", "reply", 1, 2)
    assert SENTINEL not in repr(result)


async def test_failure_logs_stay_fixed_and_carry_no_upstream_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async with client(responder(status=500, body={"error": SENTINEL})) as http:
        with caplog.at_level(logging.DEBUG):
            with pytest.raises(ProviderError):
                await complete(
                    http, name="primary", url="http://provider/x", payload=PAYLOAD, timeout=5
                )
    ours = "\n".join(
        record.getMessage()
        for record in caplog.records
        if record.name.startswith("quilr_assessment")
    )
    assert ours
    assert SENTINEL not in ours
    assert "http://provider/x" not in ours
    assert "Traceback" not in ours
