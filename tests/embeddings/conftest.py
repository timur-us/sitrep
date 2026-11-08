"""The real model, loaded once per test session (ADR 0005).

The fixtures need `make model` first, and fail, never skip, without it: as tests/db does
without a database, so a CI job whose model step broke cannot pass with these tests
unrun.
"""

from __future__ import annotations

import pytest

from app.embeddings.model_files import ModelFileError
from app.embeddings.onnx import ModelTokenizer, OnnxEmbedder


@pytest.fixture(scope="session")
def embedder() -> OnnxEmbedder:
    try:
        return OnnxEmbedder()
    except ModelFileError as error:
        pytest.fail(f"the embedding model is not in place ({error}); run `make model`")


@pytest.fixture(scope="session")
def tokenizer(embedder: OnnxEmbedder) -> ModelTokenizer:
    return embedder.tokenizer
