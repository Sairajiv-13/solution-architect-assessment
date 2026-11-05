import random
from collections.abc import AsyncIterator

import pytest

from assessment.pii import PIIScanner
from assessment.provider import ProviderFailure, sse_deltas


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1 123@example.com!", "1 [REDACTED]!"),
        ("4111 123-45-6789!", "[REDACTED]!"),
        ("foo@bar.com.baz@example.net!", "[REDACTED]!"),
        ("a" * 255 + "4111 1111 1111 1111!", "[REDACTED]!"),
    ],
)
def test_redacts_across_numeric_candidate_transitions(value: str, expected: str) -> None:
    for index in range(len(value) + 1):
        scanner = PIIScanner()
        assert (
            scanner.feed(value[:index]) + scanner.feed(value[index:]) + scanner.finish() == expected
        )


def redact(parts: list[str], limit: int = 256) -> str:
    scanner = PIIScanner(limit)
    output = []
    for part in parts:
        output.append(scanner.feed(part))
        assert len(scanner.pending) <= limit
    return "".join(output) + scanner.finish()


@pytest.mark.parametrize(
    "secret",
    [
        "demo@example.com",
        "first.last+tag@sub.example.org",
        "123-45-6789",
        "4111111111111111",
        "4111 1111 1111 1111",
        "4111-1111-1111-1111",
    ],
)
def test_every_two_part_split(secret: str) -> None:
    value = f"Hello: ({secret}), done!"
    for split in range(len(value) + 1):
        assert redact([value[:split], value[split:]]) == "Hello: ([REDACTED]), done!"
    assert redact(list(value)) == "Hello: ([REDACTED]), done!"
    randomizer = random.Random(2026)
    for _ in range(30):
        cuts = sorted({0, len(value), *(randomizer.randrange(len(value)) for _ in range(12))})
        assert redact([value[a:b] for a, b in zip(cuts, cuts[1:], strict=False)]) == (
            "Hello: ([REDACTED]), done!"
        )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Ordinary text. Price 12.50; count 42.", "Ordinary text. Price 12.50; count 42."),
        ("a@b.com,123-45-6789;4111111111111111!", "[REDACTED],[REDACTED];[REDACTED]!"),
        ("unfinished@host", "unfinished@host"),
        ("123-45-", "123-45-"),
        ("4111 1111", "4111 1111"),
        ("a@b.com.", "[REDACTED]."),
        ("12345678901234567890123456!", "[REDACTED]!"),
        ("4111 1111 1111 1111 next", "[REDACTED] next"),
        ("card4111 1111 1111 1111!", "card[REDACTED]!"),
        ("x123-45-6789 4111111111111111!", "x[REDACTED]!"),
    ],
)
def test_eof_punctuation_and_multiple(value: str, expected: str) -> None:
    assert redact(list(value)) == expected


@pytest.mark.parametrize(
    "value", ["a" * 10000 + "@example.com", "1" * 10000 + "a@example.com", "4111 " * 2000]
)
def test_oversized_candidate_is_suppressed_through_boundary(value: str) -> None:
    scanner = PIIScanner()
    result = []
    for char in value:
        result.append(scanner.feed(char))
        assert len(scanner.pending) <= 256
    assert "".join(result) + scanner.feed("!") + scanner.finish() == "[REDACTED]!"


def test_first_safe_output_and_backpressure() -> None:
    scanner = PIIScanner()
    assert scanner.feed("Hello ") == "Hello "
    assert scanner.feed("demo@") == ""
    assert scanner.feed("example.com") == ""
    assert scanner.feed("!") == "[REDACTED]!"


async def chunks(parts: list[bytes]) -> AsyncIterator[bytes]:
    for part in parts:
        yield part


async def test_sse_every_split_utf8_crlf_multiline() -> None:
    wire = 'data: {"delta":\r\ndata: "héllo"}\r\n\r\ndata: [DONE]\r\n\r\n'.encode()
    for split in range(len(wire) + 1):
        result = [delta.delta async for delta in sse_deltas(chunks([wire[:split], wire[split:]]))]
        assert result == ["héllo"]
    assert [d.delta async for d in sse_deltas(chunks([bytes([b]) for b in wire]))] == ["héllo"]


@pytest.mark.parametrize(
    "wire",
    [
        b"data: nope\n\n",
        b'data: {"delta":1}\n\n',
        b'data: {"delta":"x"}\n\n',
        b"data: \xff\n\n",
        b"data: " + b"a" * 20000,
        b"data: [DONE]",
        b"data: {}\n\ndata: [DONE]\n\n",
        b'data: {"usage":null}\n\ndata: [DONE]\n\n',
        b'data: {"delta":"a","delta":"b"}\n\ndata: [DONE]\n\n',
    ],
)
async def test_malformed_provider_stream(wire: bytes) -> None:
    with pytest.raises(ProviderFailure):
        _ = [delta async for delta in sse_deltas(chunks([wire]))]
