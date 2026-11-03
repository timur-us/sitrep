"""A fake embedder. Scaffolding: the naive first draft."""

from __future__ import annotations

from collections.abc import Sequence

from app.config.embedding import DIM, MODEL_ID, REVISION
from app.embeddings.base import passage_input, query_input


def fake_vector(text: str) -> list[float]:
    return [(hash(text) % 997) / 997] * DIM


class FakeEmbedder:
    model_id = MODEL_ID
    revision = REVISION
    dim = DIM

    def embed_query(self, text: str) -> list[float]:
        return fake_vector(query_input(text))

    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        return [fake_vector(passage_input(text)) for text in texts]
