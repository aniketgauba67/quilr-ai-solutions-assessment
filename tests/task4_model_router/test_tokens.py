"""The local token approximation must be explicit, deterministic and monotonic."""

import pytest

from quilr_assessment.task4_model_router.tokens import CHARS_PER_TOKEN, count_tokens


@pytest.mark.parametrize(
    "text,expected",
    [
        ("", 0),
        ("   \n\t ", 0),
        ("a", 1),
        ("abcd", 1),
        ("abcde", 2),
        ("abcdefgh", 2),
        ("hello world", 4),
        ("!", 1),
        ("a, b", 3),
        ("Café", 2),  # Non-ASCII letters count as single symbols.
        ("123456789", 3),
    ],
)
def test_documented_counts(text: str, expected: int) -> None:
    assert count_tokens(text) == expected


def test_counting_is_deterministic_and_whitespace_insensitive() -> None:
    text = "The quick brown fox jumps over the lazy dog."
    assert count_tokens(text) == count_tokens(text)
    assert count_tokens(text) == count_tokens(text.replace(" ", "\n"))


@pytest.mark.parametrize("size", [1, 4, 5, 400, 4000])
def test_long_runs_use_the_documented_character_ratio(size: int) -> None:
    assert count_tokens("a" * size) == (size + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN


def test_appending_text_never_lowers_the_count() -> None:
    running = 0
    text = ""
    for piece in ("alpha", " beta", " gamma-delta", "! epsilon"):
        text += piece
        current = count_tokens(text)
        assert current >= running
        running = current
