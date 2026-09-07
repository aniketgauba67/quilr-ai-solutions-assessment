"""Bounded SSE framing and the supported text-only chat-completion event shape."""

import json
from collections.abc import Iterator
from typing import Any, NoReturn

MAX_EVENT_BYTES = 64 * 1024
_MAX_INTEGER = 2**63 - 1


class StreamError(Exception):
    """A fixed diagnostic that never includes upstream text or exception details."""

    def __init__(self) -> None:
        super().__init__("Invalid upstream stream")


class SSEParser:
    """Retain one event; callers must exhaust each feed iterator before feeding again.

    UTF-8 bytes are decoded only after a complete line, so network boundaries may
    divide code points. The budget includes ignored fields and normalized line
    endings (CRLF counts as one). No complete network chunk is copied or decoded.
    """

    def __init__(self) -> None:
        self._line = bytearray()
        self._data: list[str] = []
        self._event_bytes = 0
        self._after_cr = False
        self._first_line = True
        self._finished = False

    @property
    def buffered_bytes(self) -> int:
        return self._event_bytes

    def _fail(self) -> NoReturn:
        self._line.clear()
        self._data.clear()
        self._event_bytes = 0
        self._finished = True
        raise StreamError() from None

    def _complete_line(self) -> str | None:
        try:
            line = self._line.decode("utf-8")
        except UnicodeError:
            self._fail()
        self._line.clear()
        if self._first_line:
            line = line.removeprefix("\ufeff")
            self._first_line = False
        if not line:
            data = "\n".join(self._data) if self._data else None
            self._data.clear()
            self._event_bytes = 0
            return data
        field, separator, value = line.partition(":")
        value = value.removeprefix(" ") if separator else ""
        if field == "data":
            self._data.append(value)
        elif field == "event" and value not in ("", "message"):
            self._fail()
        return None

    def feed(self, chunk: bytes) -> Iterator[str]:
        if self._finished:
            raise StreamError()
        for byte in chunk:
            if self._after_cr:
                self._after_cr = False
                if byte == 10:
                    continue
            if self._event_bytes == MAX_EVENT_BYTES:
                self._fail()
            self._event_bytes += 1
            if byte in (10, 13):
                self._after_cr = byte == 13
                data = self._complete_line()
                if data is not None:
                    yield data
            else:
                self._line.append(byte)

    def finish(self) -> None:
        if self._finished:
            return
        # A BOM by itself carries no event data.
        if self._first_line and self._line == b"\xef\xbb\xbf":
            self._line.clear()
            self._event_bytes = 0
        if self._event_bytes:
            self._fail()
        self._finished = True


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise StreamError()
        result[key] = value
    return result


def _integer(value: Any) -> int:
    if type(value) is not int or not 0 <= value <= _MAX_INTEGER:
        raise StreamError()
    return value


def _usage(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise StreamError()
    result: dict[str, Any] = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        if key in value:
            result[key] = _integer(value[key])
    for key, fields in (
        ("prompt_tokens_details", ("cached_tokens", "audio_tokens")),
        (
            "completion_tokens_details",
            (
                "reasoning_tokens",
                "audio_tokens",
                "accepted_prediction_tokens",
                "rejected_prediction_tokens",
            ),
        ),
    ):
        if key not in value:
            continue
        details = value[key]
        if details is None:
            result[key] = None
        elif isinstance(details, dict):
            result[key] = {field: _integer(details[field]) for field in fields if field in details}
        else:
            raise StreamError()
    return result


def parse_delta(data: str) -> dict[str, Any] | None:
    """Validate one text choice; preserve only conventional, bounded metadata."""
    if data == "[DONE]":
        return None
    try:
        if len(data.encode("utf-8")) > MAX_EVENT_BYTES:
            raise StreamError()
        event = json.loads(data, object_pairs_hook=_unique_object)
        json.dumps(event, allow_nan=False, ensure_ascii=False).encode("utf-8")
    except (ValueError, UnicodeError, RecursionError):
        raise StreamError() from None
    if not isinstance(event, dict) or "error" in event:
        raise StreamError()
    choices = event.get("choices")
    if not isinstance(choices, list) or len(choices) > 1:
        raise StreamError()
    result: dict[str, Any] = {}
    for key in ("id", "object", "model", "system_fingerprint"):
        if key not in event:
            continue
        value = event[key]
        if key == "system_fingerprint" and value is None:
            result[key] = None
        elif isinstance(value, str) and 1 <= len(value) <= 256:
            result[key] = value
        else:
            raise StreamError()
    if "created" in event:
        result["created"] = _integer(event["created"])
    if "usage" in event:
        result["usage"] = _usage(event["usage"])
    result["choices"] = []
    if not choices:
        return result
    choice = choices[0]
    if not isinstance(choice, dict) or type(choice.get("index")) is not int or choice["index"] != 0:
        raise StreamError()
    delta = choice.get("delta")
    if not isinstance(delta, dict) or set(delta) - {"role", "content"}:
        raise StreamError()
    if "role" in delta and delta["role"] != "assistant":
        raise StreamError()
    if (
        "content" in delta
        and delta["content"] is not None
        and not isinstance(delta["content"], str)
    ):
        raise StreamError()
    if choice.get("logprobs") is not None:
        raise StreamError()
    clean_choice: dict[str, Any] = {"index": 0, "delta": dict(delta)}
    if "finish_reason" in choice:
        if choice["finish_reason"] not in (None, "stop", "length", "content_filter"):
            raise StreamError()
        clean_choice["finish_reason"] = choice["finish_reason"]
    result["choices"].append(clean_choice)
    return result


def encode_event(event: dict[str, Any] | None) -> bytes:
    if event is None:
        return b"data: [DONE]\n\n"
    try:
        data = json.dumps(event, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        return b"data: " + data.encode("utf-8") + b"\n\n"
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise StreamError() from None
