import hashlib
import os
from collections.abc import Callable
from pathlib import Path

import tiktoken

ENCODING = "cl100k_base"
VOCAB_URL = "https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken"
VOCAB_SHA256 = "223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7"


def byte_estimate(text: str) -> int:
    return len(text.encode("utf-8"))


def input_counter() -> tuple[str, Callable[[str], int]]:
    # tiktoken keys its cache by the vocabulary URL. Only use an explicit local cache.
    directory = os.getenv("TIKTOKEN_CACHE_DIR")
    key = hashlib.sha1(VOCAB_URL.encode(), usedforsecurity=False).hexdigest()
    if directory:
        try:
            cached = (Path(directory) / key).read_bytes()
            if hashlib.sha256(cached).hexdigest() == VOCAB_SHA256:
                encoder = tiktoken.get_encoding(ENCODING)
                return ENCODING, lambda text: len(encoder.encode_ordinary(text))
        except (OSError, ValueError):
            pass
    return "utf8_bytes", byte_estimate
