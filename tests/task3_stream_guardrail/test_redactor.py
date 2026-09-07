import pytest

from quilr_assessment.task3_stream_guardrail.redactor import (
    MAX_PENDING_CHARS,
    StreamingRedactor,
)

CASES = [
    ("john.doe@example.com", "[REDACTED]"),
    ("Email me at john@example.com.", "Email me at [REDACTED]."),
    ("(john@example.com)", "([REDACTED])"),
    ("first.last+tag@sub.example.co.uk!", "[REDACTED]!"),
    ("123-45-6789", "[REDACTED]"),
    ("SSN: 123-45-6789; done.", "SSN: [REDACTED]; done."),
    ("4111111111111111", "[REDACTED]"),
    ("4111 1111 1111 1111", "[REDACTED]"),
    ("4111-1111-1111-1111", "[REDACTED]"),
    ("4111 1111-1111 1111.", "[REDACTED]."),
    ("4111111111111112", "[REDACTED]"),  # Deliberately no Luhn requirement.
    ("1234567890123", "[REDACTED]"),
    ("1234567890123456789", "[REDACTED]"),
    ("12345678901234567890", "12345678901234567890"),
    ("123456789012", "123456789012"),
    ("42 17 8. Price: 19.99", "42 17 8. Price: 19.99"),
    ("At @ noon; user@host; x@y.c", "At @ noon; user@host; x@y.c"),
    ("Hello, world! Café ☕\n", "Hello, world! Café ☕\n"),
    ("Contact john.do", "Contact john.do"),
    ("123-45-", "123-45-"),
    ("4111 1111 ", "4111 1111 "),
    ("", ""),
    (
        "john@example.com,123-45-6789;4111 1111 1111 1111!",
        "[REDACTED],[REDACTED];[REDACTED]!",
    ),
    ("123-45-6789@example.com", "[REDACTED]"),
    ("4111111111111111@example.com", "[REDACTED]"),
    (".4111 1111 1111 1111", ".[REDACTED]"),
    ("Card4111 1111 1111 1111", "Card[REDACTED]"),
    ("123-45-6789 4111 1111 1111 1111", "[REDACTED]"),
    ("4111 1111 1111 1111 123-45-6789", "[REDACTED]"),
    ("4111 1111 1111 1111 4111 1111 1111 1111", "[REDACTED]"),
    (".john@example.com", ".[REDACTED]"),
    ("...john@example.com", "...[REDACTED]"),
    ("1234 1234567890@example.com", "[REDACTED]"),
    ("123-45-6789 12345@example.com", "[REDACTED]"),
]


def redact(chunks: list[str]) -> str:
    engine = StreamingRedactor()
    result = "".join(engine.feed(chunk) for chunk in chunks) + engine.finish()
    assert engine.pending_chars == 0
    return result


@pytest.mark.parametrize("text,expected", CASES)
def test_all_two_way_splits_and_fixed_sizes_are_invariant(text: str, expected: str) -> None:
    assert redact([text]) == expected
    for split in range(len(text) + 1):
        assert redact([text[:split], "", text[split:]]) == expected
    for size in (1, 2, 3, 5, 7, 11):
        assert (
            redact([text[index : index + size] for index in range(0, len(text), size)]) == expected
        )


@pytest.mark.parametrize(
    "chunks",
    [
        ["john.", "doe@", "example.", "com"],
        ["123-", "45-", "6789"],
        ["4111 1111", " ", "1111 1111"],
    ],
)
def test_no_candidate_prefix_is_emitted_before_classification(chunks: list[str]) -> None:
    engine = StreamingRedactor()
    for chunk in chunks:
        assert engine.feed(chunk) == ""
    assert engine.finish() == "[REDACTED]"
    assert sum(engine.redaction_counts.values()) == 1


def test_safe_text_is_released_before_finish_and_pii_stays_pending() -> None:
    engine = StreamingRedactor()
    assert engine.feed("Hello! Contact john.do") == "Hello! Contact "
    assert engine.feed("e@example.com") == ""
    assert engine.feed(" for help.") == "[REDACTED] for "
    assert engine.finish() == "help."


def test_long_stream_retains_bounded_state_without_historical_output() -> None:
    engine = StreamingRedactor()
    for _ in range(20_000):
        assert engine.feed("Ordinary text. ") == "Ordinary text. "
        assert engine.pending_chars <= MAX_PENDING_CHARS
    assert engine.finish() == ""


@pytest.mark.parametrize("unit", ["a", "9", "1 "])
def test_overlong_candidates_are_discarded_once_with_bounded_state(unit: str) -> None:
    engine = StreamingRedactor()
    emitted = 0
    for _ in range(2000):
        output = engine.feed(unit)
        assert output in ("", "[REDACTED]")
        emitted += output.count("[REDACTED]")
        assert engine.pending_chars <= MAX_PENDING_CHARS
    assert emitted == 1
    assert engine.finish() == (" " if unit.endswith(" ") else "")


@pytest.mark.parametrize("size", [1, 7, 511, 512, 513, 2000])
def test_overflow_is_chunk_invariant_and_recovers_at_boundary(size: int) -> None:
    text = "a" * 800 + "@example.com! john@example.com."
    chunks = [text[index : index + size] for index in range(0, len(text), size)]
    assert redact(chunks) == "[REDACTED]! [REDACTED]."


def test_finish_is_idempotent_and_feed_after_finish_is_rejected() -> None:
    engine = StreamingRedactor()
    assert engine.feed("word") == ""
    assert engine.finish() == "word"
    assert engine.finish() == ""
    with pytest.raises(ValueError, match="already finished"):
        engine.feed("more")
