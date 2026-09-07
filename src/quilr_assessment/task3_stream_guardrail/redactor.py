"""Bounded lexical candidates; classification never depends on delta boundaries."""

import re
import string

REPLACEMENT = "[REDACTED]"
MAX_PENDING_CHARS = 512
TOKEN_CHARS = frozenset(string.ascii_letters + string.digits + "._%+@-")
DIGITS = frozenset(string.digits)
LOCAL = r"[A-Za-z0-9_%+-]+(?:\.[A-Za-z0-9_%+-]+)*"
LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
EMAIL = re.compile(
    rf"(?<![A-Za-z0-9_%+@-])({LOCAL})@({LABEL}(?:\.{LABEL})*\.[A-Za-z]{{2,63}})"
    r"(?![A-Za-z0-9_%+@-])"
)
SSN = re.compile(r"(?<![0-9])[0-9]{3}-[0-9]{2}-[0-9]{4}(?![0-9])")
NUMBER = re.compile(r"[0-9](?:[ -]?[0-9])*")


class StreamingRedactor:
    def __init__(self) -> None:
        self._pending: list[str] = []
        self._discarding = False
        self._last = ""
        self._finished = False
        self.redaction_counts = {"email": 0, "ssn": 0, "card": 0, "overflow": 0}

    @property
    def pending_chars(self) -> int:
        """Retained candidate text, excluding output already returned to the caller."""
        return len(self._pending)

    def _classify(self, text: str) -> str:
        matches: list[tuple[int, int, str]] = []
        for match in EMAIL.finditer(text):
            if len(match[1]) <= 64 and len(match[2]) <= 253:
                matches.append((match.start(), match.end(), "email"))
        matches.extend((m.start(), m.end(), "ssn") for m in SSN.finditer(text))
        for match in NUMBER.finditer(text):
            digits = sum(char in DIGITS for char in match[0])
            if 13 <= digits <= 19 or (digits > 19 and any(c in " -" for c in match[0])):
                # Adjacent cards/SSNs can be one ambiguous separated numeric run.
                # Redact that run rather than releasing a valid card inside it.
                matches.append((match.start(), match.end(), "card"))
        # Earliest/longest first; merge overlaps so no other detected suffix leaks.
        matches.sort(key=lambda item: (item[0], -item[1], item[2]))
        merged: list[tuple[int, int, str]] = []
        for start, stop, category in matches:
            if merged and start < merged[-1][1]:
                previous_start, previous_stop, previous_category = merged[-1]
                merged[-1] = (previous_start, max(stop, previous_stop), previous_category)
            else:
                merged.append((start, stop, category))
        output = []
        end = 0
        for start, stop, category in merged:
            output.extend((text[end:start], REPLACEMENT))
            self.redaction_counts[category] += 1
            end = stop
        output.append(text[end:])
        return "".join(output)

    def _release(self) -> str:
        if self._discarding:
            result = " " if self._last == " " else ""
        else:
            result = self._classify("".join(self._pending))
        self._pending.clear()
        self._discarding = False
        self._last = ""
        return result

    def _hold(self, char: str) -> str:
        self._last = char
        if self._discarding:
            return ""
        if len(self._pending) == MAX_PENDING_CHARS:
            # Never release a potentially sensitive prefix to enforce a size limit.
            self._pending.clear()
            self._discarding = True
            self.redaction_counts["overflow"] += 1
            return REPLACEMENT
        self._pending.append(char)
        return ""

    def feed(self, text: str) -> str:
        if self._finished:
            raise ValueError("Redactor has already finished")
        output = []
        for char in text:
            if char in TOKEN_CHARS:
                if self._last == " " and char not in DIGITS:
                    output.append(self._release())
                output.append(self._hold(char))
            elif char == " " and self._last in DIGITS:
                # One space may join the next group of a card candidate.
                output.append(self._hold(char))
            else:
                output.extend((self._release(), char))
        return "".join(output)

    def finish(self) -> str:
        if self._finished:
            return ""
        self._finished = True
        return self._release()
