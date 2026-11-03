"""The fake embedder: the real model's shape, and a name that cannot pass for it.

FakeEmbedder stands in for the model of app/config/embedding.py until M1. Its vectors
have DIM dimensions and unit length, are deterministic (SHAKE-256 of the prefixed text,
not Python's hash(), which changes per process), and carry no meaning: similar texts get
unrelated vectors. Its model id is "fake/" plus the real one and its revision is "fake";
both are stored with every vector, so a fake vector is never taken for a real one, and
retrieval quality is never measured with it.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

from app.config.embedding import DIM, MODEL_ID
from app.embeddings.base import passage_list, query_input

_BYTES = 4  # per dimension


def fake_vector(text: str) -> list[float]:
    """A unit vector of DIM floats seeded by the text's SHAKE-256."""
    digest = hashlib.shake_256(text.encode("utf-8")).digest(_BYTES * DIM)
    raw = [
        int.from_bytes(digest[i : i + _BYTES], "big") / 2 ** (8 * _BYTES - 1) - 1.0
        for i in range(0, len(digest), _BYTES)
    ]
    norm = sum(value * value for value in raw) ** 0.5
    return [value / norm for value in raw]


class FakeEmbedder:
    model_id = "fake/" + MODEL_ID
    revision = "fake"
    dim = DIM

    def embed_query(self, text: str) -> list[float]:
        return fake_vector(query_input(text))

    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        return [fake_vector(text) for text in passage_list(texts)]
