"""Scaffolding (M1 PR 16, commit 1): recorded runs, built in the next commit."""

from __future__ import annotations

from typing import Any

FORMAT = 1
STALE_KEYS = ("prompt_version", "prompt_sha256", "tools_sha256", "corpus_sha256", "model")


class TranscriptError(ValueError):
    """A recorded tool result that wrap() did not write."""


def _todo(*args: Any, **kwargs: Any) -> Any:
    raise NotImplementedError("M1 PR 16")


canonical_sha256 = tools_sha256 = current = stale = reply_text = tool_calls = _todo
unwrap = sources = tool_texts = check = sample_date = _todo
