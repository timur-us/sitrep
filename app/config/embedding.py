"""The embedding model: id, pinned revision, shape and the files allowed to load.

ADR 0005 records the decision and repeats these values in a yaml block;
tests/test_embedding_config.py keeps the two equal and anchors DIM and
MAX_TOKENS to the model's own config.json (embedding_model_config.json here).
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

__all__ = (
    "DIM",
    "FILES",
    "MAX_TOKENS",
    "MODEL_ID",
    "PASSAGE_PREFIX",
    "QUERY_PREFIX",
    "REVISION",
)

MODEL_ID = ""
REVISION = ""
DIM = 0
QUERY_PREFIX = ""
PASSAGE_PREFIX = ""
MAX_TOKENS = 0

# Path in the model repository at REVISION -> sha256 of its content.
FILES: Mapping[str, str] = MappingProxyType({})
