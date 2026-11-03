"""The Embedder interface. Scaffolding: no prefixes."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable


@runtime_checkable
class Embedder(Protocol):
    model_id: str
    revision: str
    dim: int

    def embed_query(self, text: str) -> list[float]: ...

    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]: ...


def query_input(text: str) -> str:
    return text


def passage_input(text: str) -> str:
    return text
