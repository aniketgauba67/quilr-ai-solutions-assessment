import json
from collections.abc import Iterator

import pytest

from quilr_assessment.task3_stream_guardrail.stream import (
    MAX_EVENT_BYTES,
    SSEParser,
    StreamError,
    encode_event,
    parse_delta,
)


def delta(content: object = "Hello") -> dict:
    return {"choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}]}


@pytest.mark.parametrize("ending", [b"\n", b"\r\n", b"\r"])
def test_every_network_split_preserves_unicode_and_sse_framing(ending: bytes) -> None:
    wire = ending.join(
        [
            b"\xef\xbb\xbf: keepalive",
            b"event: message",
            "data: café 🔑".encode(),
            b"data: next",
            b"",
            b"",
        ]
    )
    for split in range(len(wire) + 1):
        parser = SSEParser()
        result = list(parser.feed(wire[:split])) + list(parser.feed(wire[split:]))
        assert result == ["café 🔑\nnext"]
        assert parser.buffered_bytes == 0
        parser.finish()


def test_one_byte_chunks_and_multiple_events() -> None:
    wire = b"data: one\r\n\r\ndata: two\r\rdata: three\n\ndata: [DONE]\n\n"
    parser = SSEParser()
    result = []
    for byte in wire:
        result.extend(parser.feed(bytes([byte])))
        assert parser.buffered_bytes <= MAX_EVENT_BYTES
    parser.finish()
    assert result == ["one", "two", "three", "[DONE]"]


def test_comments_ignored_fields_and_exact_data_field_semantics() -> None:
    parser = SSEParser()
    wire = b": comment\nid: 123\nretry: 50\nunknown: value\ndata : ignored\n\n"
    assert list(parser.feed(wire)) == []
    assert list(parser.feed(b"data:\n\ndata\n\ndata:  two spaces\ndata: trailing \n\n")) == [
        "",
        "",
        " two spaces\ntrailing ",
    ]
    parser.finish()


def test_initial_bom_only_and_empty_stream_finish_cleanly() -> None:
    for wire in (b"", b"\xef\xbb\xbf", b"\n\n", b": comment\n\n"):
        parser = SSEParser()
        assert list(parser.feed(wire)) == []
        parser.finish()
        assert parser.buffered_bytes == 0


def test_bom_is_removed_only_at_stream_start() -> None:
    parser = SSEParser()
    assert list(parser.feed(b"\n\xef\xbb\xbfdata: ignored\n\ndata: \xef\xbb\xbfkept\n\n")) == [
        "\ufeffkept"
    ]
    parser.finish()


@pytest.mark.parametrize("wire", [b"data: partial", b"data: partial\n", b": partial", b"\xc3"])
def test_incomplete_event_or_unicode_is_rejected_on_finish(wire: bytes) -> None:
    parser = SSEParser()
    assert list(parser.feed(wire)) == []
    with pytest.raises(StreamError, match="^Invalid upstream stream$"):
        parser.finish()
    assert parser.buffered_bytes == 0


@pytest.mark.parametrize("wire", [b"data: \xff\n\n", b": \xed\xa0\x80\n\n", b"data: \xc3\n\n"])
def test_invalid_utf8_has_only_a_sanitized_error(wire: bytes) -> None:
    parser = SSEParser()
    with pytest.raises(StreamError, match="^Invalid upstream stream$"):
        list(parser.feed(wire))
    assert parser.buffered_bytes == 0
    with pytest.raises(StreamError):
        list(parser.feed(b"data: recovery is not allowed\n\n"))


@pytest.mark.parametrize("name", [b"error", b"tool_call", b"private-sentinel"])
def test_unsupported_sse_event_type_is_rejected(name: bytes) -> None:
    with pytest.raises(StreamError, match="^Invalid upstream stream$"):
        list(SSEParser().feed(b"event: " + name + b"\ndata: {}\n\n"))


def test_event_byte_limit_is_inclusive_and_resets_between_events() -> None:
    wire = b"data:" + b"x" * (MAX_EVENT_BYTES - 7) + b"\n\n"
    parser = SSEParser()
    assert list(parser.feed(wire * 2)) == ["x" * (MAX_EVENT_BYTES - 7)] * 2
    assert parser.buffered_bytes == 0
    parser.finish()


@pytest.mark.parametrize("prefix", [b"data:", b":", b"ignored:"])
def test_long_unterminated_data_comments_and_ignored_fields_are_bounded(prefix: bytes) -> None:
    parser = SSEParser()
    assert list(parser.feed(prefix + b"x" * (MAX_EVENT_BYTES - len(prefix)))) == []
    assert parser.buffered_bytes == MAX_EVENT_BYTES
    with pytest.raises(StreamError):
        list(parser.feed(b"x"))
    assert parser.buffered_bytes == 0


def test_ignored_lines_cannot_reset_the_event_budget() -> None:
    parser = SSEParser()
    with pytest.raises(StreamError):
        list(parser.feed(b": ignored\n" * MAX_EVENT_BYTES))
    assert parser.buffered_bytes == 0


def test_feed_is_lazy_and_does_not_collect_all_events_from_large_chunks() -> None:
    parser = SSEParser()
    events = parser.feed(b"data: first\n\n" + b"x" * (MAX_EVENT_BYTES + 1))
    assert isinstance(events, Iterator)
    assert parser.buffered_bytes == 0
    assert next(events) == "first"
    assert parser.buffered_bytes == 0
    with pytest.raises(StreamError):
        next(events)


def test_no_event_is_emitted_before_the_blank_line() -> None:
    parser = SSEParser()
    assert list(parser.feed(b"data: first\n")) == []
    assert list(parser.feed(b"\n")) == ["first"]
    parser.finish()
    with pytest.raises(StreamError):
        list(parser.feed(b""))


@pytest.mark.parametrize("content", ["", "Hello", "café 🔑", None])
def test_text_or_null_delta_and_done(content: object) -> None:
    event = delta(content)
    assert parse_delta(json.dumps(event)) == event
    assert parse_delta("[DONE]") is None


def test_role_missing_content_and_usage_only_events_are_preserved() -> None:
    for event in (
        {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        {"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}},
        {"choices": [], "usage": None},
        {"choices": []},
    ):
        assert parse_delta(json.dumps(event)) == event


def test_conventional_metadata_and_numeric_usage_details_are_preserved() -> None:
    event = {
        **delta(),
        "id": "chatcmpl-demo",
        "object": "chat.completion.chunk",
        "created": 123,
        "model": "mock-model",
        "system_fingerprint": "fp-demo",
        "usage": {
            "prompt_tokens": 3,
            "completion_tokens": 2,
            "total_tokens": 5,
            "prompt_tokens_details": {"cached_tokens": 1, "audio_tokens": 0},
            "completion_tokens_details": {"reasoning_tokens": 1},
        },
    }
    assert parse_delta(json.dumps(event)) == event
    event["system_fingerprint"] = None
    assert parse_delta(json.dumps(event)) == event


def test_unknown_metadata_is_not_forwarded_as_an_alternate_text_channel() -> None:
    event = delta()
    event["private"] = "private-sentinel"
    event["choices"][0]["private"] = "private-sentinel"
    event["usage"] = {"private": "private-sentinel", "prompt_tokens": 1}
    parsed = parse_delta(json.dumps(event))
    assert "private-sentinel" not in json.dumps(parsed)
    assert parsed["usage"] == {"prompt_tokens": 1}


@pytest.mark.parametrize(
    "payload",
    [
        "",
        "[DONE] ",
        "{",
        "[]",
        "null",
        '{"choices":[],"choices":[]}',
        '{"choices":[],"unused":NaN}',
        '{"choices":[],"unused":1e400}',
        '{"choices":[],"unused":"\\ud800"}',
        '{"error":{"message":"private-sentinel"}}',
        '{"choices":[],"error":null}',
        " " * (MAX_EVENT_BYTES + 1),
    ],
)
def test_invalid_json_and_error_events_are_sanitized(payload: str) -> None:
    with pytest.raises(StreamError, match="^Invalid upstream stream$"):
        parse_delta(payload)


@pytest.mark.parametrize(
    "choices",
    [
        None,
        {},
        [None],
        [{"index": 0, "delta": {}}, {"index": 1, "delta": {}}],
        [{"delta": {}}],
        [{"index": True, "delta": {}}],
        [{"index": 1, "delta": {}}],
        [{"index": 0, "delta": None}],
        [{"index": 0, "delta": {"content": 123}}],
        [{"index": 0, "delta": {"content": ["private-sentinel"]}}],
        [{"index": 0, "delta": {"role": "user"}}],
        [{"index": 0, "delta": {"tool_calls": []}}],
        [{"index": 0, "delta": {"function_call": {}}}],
        [{"index": 0, "delta": {"refusal": "private-sentinel"}}],
        [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
        [{"index": 0, "delta": {}, "finish_reason": []}],
        [{"index": 0, "delta": {}, "logprobs": {"content": "private-sentinel"}}],
    ],
)
def test_unsupported_delta_shapes_are_rejected(choices: object) -> None:
    with pytest.raises(StreamError, match="^Invalid upstream stream$"):
        parse_delta(json.dumps({"choices": choices}))


@pytest.mark.parametrize(
    "key,value",
    [
        ("id", ""),
        ("model", "x" * 257),
        ("object", 1),
        ("system_fingerprint", []),
        ("created", True),
        ("created", -1),
        ("created", 2**63),
        ("usage", []),
        ("usage", {"total_tokens": True}),
        ("usage", {"prompt_tokens": -1}),
        ("usage", {"completion_tokens": "2"}),
        ("usage", {"prompt_tokens_details": []}),
        ("usage", {"completion_tokens_details": {"reasoning_tokens": -1}}),
    ],
)
def test_metadata_has_explicit_types_and_bounds(key: str, value: object) -> None:
    with pytest.raises(StreamError, match="^Invalid upstream stream$"):
        parse_delta(json.dumps({**delta(), key: value}))


@pytest.mark.parametrize("reason", [None, "stop", "length", "content_filter"])
def test_supported_finish_reasons(reason: str | None) -> None:
    event = delta(None)
    event["choices"][0]["finish_reason"] = reason
    assert parse_delta(json.dumps(event)) == event


def test_encoded_events_are_single_safe_sse_records_and_round_trip() -> None:
    event = delta("Unicode café\n\ndata: not a separate event")
    parser = SSEParser()
    payloads = list(parser.feed(encode_event(event) + encode_event(None)))
    parser.finish()
    assert [parse_delta(payload) for payload in payloads] == [event, None]
    assert encode_event(None) == b"data: [DONE]\n\n"


@pytest.mark.parametrize("value", [float("nan"), "\ud800", object()])
def test_encoding_errors_are_sanitized(value: object) -> None:
    with pytest.raises(StreamError, match="^Invalid upstream stream$"):
        encode_event({"error": value})
