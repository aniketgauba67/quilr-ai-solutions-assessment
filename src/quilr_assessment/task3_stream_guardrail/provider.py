"""HTTP provider boundary and incremental transformation of validated SSE deltas."""

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx

from .redactor import StreamingRedactor
from .stream import SSEParser, StreamError, encode_event, parse_delta

logger = logging.getLogger(__name__)
CLOSE_TIMEOUT_SECONDS = 5.0


class ProviderError(Exception):
    def __init__(self, *, timeout: bool = False) -> None:
        self.timeout = timeout
        super().__init__("Provider unavailable")


async def close_provider(response: httpx.Response) -> None:
    try:
        async with asyncio.timeout(CLOSE_TIMEOUT_SECONDS):
            await response.aclose()
    except Exception:
        logger.warning("Guardrail provider cleanup failed")


async def open_provider(
    client: httpx.AsyncClient, url: str, payload: dict[str, Any], timeout: float
) -> httpx.Response:
    response: httpx.Response | None = None
    try:
        async with asyncio.timeout(timeout):
            request = client.build_request(
                "POST",
                url,
                json=payload,
                headers={"Accept": "text/event-stream", "Accept-Encoding": "identity"},
            )
            response = await client.send(request, stream=True)
        if (
            response.status_code != 200
            or response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            != "text/event-stream"
            or response.headers.get("content-encoding", "identity").lower() != "identity"
        ):
            raise ProviderError()
        return response
    except BaseException as exc:
        if response is not None:
            await close_provider(response)
        if not isinstance(exc, Exception):
            raise
        raise ProviderError(
            timeout=isinstance(exc, (TimeoutError, httpx.TimeoutException))
        ) from None


def content_event(text: str, metadata: dict[str, Any]) -> dict[str, Any]:
    return {**metadata, "choices": [{"index": 0, "delta": {"content": text}}]}


async def sanitized_events(upstream: httpx.Response, timeout: float) -> AsyncIterator[bytes]:
    """The response owner closes upstream on success, failure or client disconnect."""
    parser = SSEParser()
    redactor = StreamingRedactor()
    metadata: dict[str, Any] = {}
    text_finished = False
    chunks = upstream.aiter_bytes().__aiter__()
    logger.info("Guardrail stream started")
    try:
        while True:
            try:
                async with asyncio.timeout(timeout):
                    chunk = await anext(chunks)
            except StopAsyncIteration:
                parser.finish()
                raise StreamError() from None  # A normal stream requires [DONE].
            for data in parser.feed(chunk):
                event = parse_delta(data)
                if event is None:
                    tail = redactor.finish()
                    if tail:
                        yield encode_event(content_event(tail, metadata))
                    yield encode_event(None)
                    logger.info("Guardrail stream completed")
                    return
                if event["choices"]:
                    if text_finished:
                        raise StreamError()
                    choice = event["choices"][0]
                    delta = choice["delta"]
                    metadata = {k: v for k, v in event.items() if k not in ("choices", "usage")}
                    text = delta.get("content")
                    safe = redactor.feed(text) if text is not None else ""
                    if choice.get("finish_reason") is not None:
                        safe += redactor.finish()
                        text_finished = True
                    if text is not None or safe:
                        delta["content"] = safe
                yield encode_event(event)
    except Exception as exc:
        # On an incomplete stream, never flush unresolved text as if it were final.
        timed_out = isinstance(exc, (TimeoutError, httpx.TimeoutException))
        logger.warning("Guardrail provider stream failed")
        code = "upstream_timeout" if timed_out else "upstream_stream_error"
        yield b"event: error\n" + encode_event(
            {"error": {"code": code, "message": "Provider stream unavailable"}}
        )
