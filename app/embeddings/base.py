"""The Embedder interface (ADR 0005).

Callers pass the plain text; the embedder adds the model's prefix itself
(`query: ` for a search, `passage: ` for a stored chunk), through query_input() and
passage_input(), so no caller can forget it or add it twice. e5 was trained with the
prefixes and degrades without them.

Every embedder names the model and revision its vectors come from; both are stored with
every vector (`chunks.embedding_model`, `chunks.embedding_revision`), so vectors of
different models are never compared. The real embedder is app/embeddings/onnx.py;
app/embeddings/fake.py serves the tests that need no model.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from app.config.embedding import PASSAGE_PREFIX, QUERY_PREFIX


class TextTooLong(ValueError):
    """An input over the model's limit, prefix and special tokens included. The message
    carries counts, never the text. One class for every embedder with a limit, so a
    caller catches it without importing the model runtime (M1 PR 8: the search handler
    refuses such a query, app/harness/handlers.py)."""


@runtime_checkable
class Embedder(Protocol):
    model_id: str
    revision: str
    dim: int

    def embed_query(self, text: str) -> list[float]:
        """One unit-length vector of dim floats for a search, the prefix added inside;
        TextTooLong if the text is over the model's limit."""
        ...

    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        """One vector per text, in order, the prefix added inside; a str is refused."""
        ...


def _text(text: object) -> str:
    if type(text) is not str:
        raise TypeError(f"an embedder takes str, not {type(text).__name__}")
    return text


def query_input(text: str) -> str:
    return QUERY_PREFIX + _text(text)


def passage_input(text: str) -> str:
    return PASSAGE_PREFIX + _text(text)


def passage_list(texts: Sequence[str]) -> list[str]:
    """texts as a list of passage inputs; a single str is refused, not split into letters."""
    if isinstance(texts, str | bytes):
        raise TypeError("embed_passages takes a sequence of str, not one str")
    return [passage_input(text) for text in texts]
