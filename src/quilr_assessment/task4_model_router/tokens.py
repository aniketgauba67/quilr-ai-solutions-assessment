"""Deterministic local token approximation shared by the router and its mocks."""

import re

CHARS_PER_TOKEN = 4
_PIECE = re.compile(r"[A-Za-z0-9]+|[^\sA-Za-z0-9]")


def count_tokens(text: str) -> int:
    """Approximate tokens as ~4 characters per alphanumeric run, one per symbol.

    This is an explicit local heuristic, not a model tokenizer. It exists so the
    gateway can size a reservation without trusting client-supplied counts, and
    so the local mock providers report usage the gateway can verify.
    """
    return sum(
        (len(piece) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN for piece in _PIECE.findall(text)
    )
