"""Scaffolding (M1 PR 4, the tests-first commit): the names the tests import, nothing more.

The real embedder replaces this file in the next commit.
"""

from __future__ import annotations

__all__ = (
    "EmbedderError",
    "ModelTokenizer",
    "OnnxEmbedder",
    "TextTooLong",
    "load_tokenizer",
)

CONFIG = "config.json"
MODEL = "onnx/model.onnx"
TOKENIZER = "onnx/tokenizer.json"


class EmbedderError(Exception):
    pass


class TextTooLong(ValueError):
    pass


class ModelTokenizer:
    def __init__(self, *args: object, **kwargs: object) -> None:
        raise NotImplementedError("M1 PR 4")


class OnnxEmbedder:
    def __init__(self, *args: object, **kwargs: object) -> None:
        raise NotImplementedError("M1 PR 4")


def load_tokenizer(*args: object, **kwargs: object) -> ModelTokenizer:
    raise NotImplementedError("M1 PR 4")
