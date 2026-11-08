"""Passages for the embedder: CHUNK_TOKENS tokens each at most, the prefix included.

chunk(text, tokenizer) cuts a document into passages that the model sees whole. Each
passage, with PASSAGE_PREFIX and the special tokens, counts at most CHUNK_TOKENS tokens
of the model's own tokenizer (ADR 0005), so the embedder never refuses one for its
length (MAX_TOKENS) and no text is cut off silently.

- **Verbatim.** Every passage is a slice of the text without its surrounding whitespace.
  The slices run end to end, so only whitespace at a cut is lost, and every passage can
  be found in its document.
- **Cuts** fall on token boundaries. Within the second half of a window the cut prefers
  a blank line, then a line break, then the end of a sentence, then a space; without
  any of them, it cuts between two tokens.
- **No overlap.** Each character lands in one passage.
- **Checked, not estimated.** A passage's count comes from tokenizing it with its prefix,
  so a cut that changes how the text around it tokenizes is caught and moved.

A text that fits is one passage; an empty or whitespace-only text gives none.
"""

from __future__ import annotations

from typing import Protocol

from app.config.embedding import CHUNK_TOKENS, MAX_TOKENS
from app.embeddings.base import passage_input

__all__ = ("TokenCounter", "chunk")

_SENTENCE_END = frozenset(".!?…:;")
# Boundary ranks, best first.
_PARAGRAPH, _LINE, _SENTENCE, _SPACE, _NONE = 4, 3, 2, 1, 0


class TokenCounter(Protocol):
    """The tokenizer the chunker cuts with (onnx.ModelTokenizer, or a test's own)."""

    def count(self, text: str) -> int:
        """Tokens the model sees for text as given, special tokens included."""
        ...

    def offsets(self, text: str) -> list[tuple[int, int]]:
        """(start, end) in characters of text for each token, special tokens left out."""
        ...


def _rank(text: str, spans: list[tuple[int, int]], k: int) -> int:
    """How good a cut before token k is: the whitespace between token k-1 and token k."""
    end, (start, stop) = spans[k - 1][1], spans[k]
    token = text[start:stop]
    between = text[end:start] + token[: len(token) - len(token.lstrip())]
    if not between or not between.isspace():
        return _NONE
    if between.count("\n") >= 2:
        return _PARAGRAPH
    if "\n" in between:
        return _LINE
    if end > 0 and text[end - 1] in _SENTENCE_END:
        return _SENTENCE
    return _SPACE


def _cut(text: str, spans: list[tuple[int, int]], first: int, last: int) -> int:
    """The token to start the next passage at: the best boundary in the second half of
    first+1..last, the latest of equals, or last itself."""
    best, best_rank = last, _NONE
    for k in range(last, first + max(1, (last - first) // 2) - 1, -1):
        rank = _rank(text, spans, k)
        if rank > best_rank:
            best, best_rank = k, rank
    return best


def chunk(text: str, tokenizer: TokenCounter, *, limit: int = CHUNK_TOKENS) -> list[str]:
    """The passages of text, in order, each counting at most limit tokens with its prefix."""
    if type(text) is not str:
        raise TypeError(f"chunk takes str, not {type(text).__name__}")
    if type(limit) is not int or not 0 < limit <= MAX_TOKENS:
        raise ValueError(f"limit must be an int in 1..{MAX_TOKENS}")
    overhead = tokenizer.count(passage_input(""))
    if overhead >= limit:
        raise ValueError(f"limit {limit} leaves no room after the prefix ({overhead} tokens)")

    def fits(piece: str) -> bool:
        return tokenizer.count(passage_input(piece)) <= limit

    if not text.strip():
        return []
    if fits(text.strip()):
        return [text.strip()]
    spans = tokenizer.offsets(text)
    budget = limit - overhead
    passages: list[str] = []
    start, first = 0, 0  # the character and the token the current passage starts at
    while True:
        # The rest is copied only once it may fit: a copy per passage would make a long
        # text cost its length squared (review finding sec-4).
        if len(spans) - first <= budget and fits(rest := text[start:].strip()):
            if rest:
                passages.append(rest)
            return passages
        last = min(len(spans) - 1, first + budget)
        if last <= first:
            # A single token that does not fit: impossible while limit > overhead + 1.
            raise ValueError("a single token does not fit the limit")
        k = _cut(text, spans, first, last)
        while k > first + 1 and (
            spans[k][0] <= start or not fits(text[start : spans[k][0]].strip())
        ):
            k -= 1
        end = spans[k][0]
        piece = text[start:end].strip()
        # Never loop in place, never keep a passage that does not fit: both would take a
        # tokenizer whose offsets break its contract.
        if end <= start or (piece and not fits(piece)):
            raise ValueError("the tokenizer's offsets give no passage that fits")
        if piece:
            passages.append(piece)
        start, first = end, k
