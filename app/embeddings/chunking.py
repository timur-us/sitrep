"""Scaffolding (M1 PR 4, the tests-first commit): the names the tests import, nothing more.

The chunker replaces this file in the next commit.
"""

from __future__ import annotations

__all__ = ("chunk",)


def chunk(text: str, tokenizer: object, *, limit: int = 0) -> list[str]:
    raise NotImplementedError("M1 PR 4")
