"""Scaffolding (M1 PR 16, commit 1): the cost estimate, built in the next commit."""

from __future__ import annotations

from typing import Any

ASSUMED = {"input_tokens": 12_000, "output_tokens": 1_500}


def _todo(*args: Any, **kwargs: Any) -> Any:
    raise NotImplementedError("M1 PR 16")


estimate = lines = ceiling_per_ask = measured = _todo
