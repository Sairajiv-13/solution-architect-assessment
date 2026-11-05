"""Bounded candidate scanner. Provider SSE framing is deliberately a separate layer."""

import re
import string

REDACTED = "[REDACTED]"
TOKEN_CHARS = frozenset(string.ascii_letters + string.digits + "._%+-@")
EMAIL = re.compile(
    r"(?=([A-Za-z0-9._%+\-]{1,64}@"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?\.)+"
    r"[A-Za-z]{2,63}))"
)
SSN = re.compile(r"[0-9]{3}-[0-9]{2}-[0-9]{4}")
CARD = re.compile(r"[0-9](?:[ -]?[0-9]){12,}")
NUMERIC_SUFFIX = re.compile(r"[0-9][0-9-]*$")


class PIIScanner:
    def __init__(self, max_candidate: int = 256):
        if max_candidate < 1:
            raise ValueError("max_candidate must be positive")
        self.limit = max_candidate
        self.pending = ""
        self.suppressing = False
        self.numeric = False
        self.has_candidate = False
        self.numeric_tail = False

    def _append(self, char: str) -> str:
        if char in string.digits:
            self.numeric_tail = True
        elif char not in " -":
            self.numeric_tail = False
        if self.suppressing:
            return ""
        if len(self.pending) == self.limit:
            self.pending = ""
            self.suppressing = True
            return REDACTED
        self.pending += char
        return ""

    def _flush(self) -> str:
        value = "" if self.suppressing else self.pending
        # Scan the original text so masking an SSN cannot hide an overlapping card.
        # Email lookahead also finds addresses whose local parts overlap a previous domain.
        spans = [match.span(1) for match in EMAIL.finditer(value)]
        spans.extend(match.span() for pattern in (SSN, CARD) for match in pattern.finditer(value))
        output: list[str] = []
        end = 0
        for start, stop in sorted(spans):
            if start >= end:
                output.extend((value[end:start], REDACTED))
            end = max(end, stop)
        output.append(value[end:])
        self.pending = ""
        self.suppressing = self.numeric = self.has_candidate = False
        self.numeric_tail = False
        return "".join(output)

    def feed(self, text: str) -> str:
        output: list[str] = []
        for char in text:
            # Numeric candidates may span ASCII spaces; other email-like tokens may not.
            if self.has_candidate and char == " ":
                if self.suppressing and self.numeric_tail:
                    # An oversized token can end with the first digits of a spaced card.
                    self.numeric = True
                elif not self.numeric and not self.suppressing:
                    suffix = NUMERIC_SUFFIX.search(self.pending)
                    if suffix:
                        tail = suffix.group()
                        self.pending = self.pending[: suffix.start()]
                        output.append(self._flush())
                        self.pending = tail
                        self.has_candidate = self.numeric = True
                if self.numeric:
                    output.append(self._append(char))
                else:
                    output.append(self._flush())
                    output.append(char)
            elif char in TOKEN_CHARS:
                # Digits after a space can become the local part of an email.
                if not self.has_candidate:
                    self.has_candidate = True
                    self.numeric = char in string.digits
                self.numeric = self.numeric and char in "0123456789-"
                output.append(self._append(char))
            else:
                output.append(self._flush())
                output.append(char)
        return "".join(output)

    def finish(self) -> str:
        return self._flush()
