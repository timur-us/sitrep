"""The embedding model: id, pinned revision, shape and the files allowed to load.

ADR 0005 records the decision and repeats these values in a yaml block;
tests/test_embedding_config.py keeps the two equal and anchors DIM and
MAX_TOKENS to the model's own config.json (embedding_model_config.json here).
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

__all__ = (
    "CHUNK_TOKENS",
    "DIM",
    "FILES",
    "MAX_TOKENS",
    "MODEL_ID",
    "PASSAGE_PREFIX",
    "QUERY_PREFIX",
    "REVISION",
)

MODEL_ID = "intfloat/multilingual-e5-small"
REVISION = "614241f622f53c4eeff9890bdc4f31cfecc418b3"  # a commit: a branch or tag can move
DIM = 384  # hidden_size in config.json
# The embedder adds these; e5 was trained with them and degrades without.
QUERY_PREFIX = "query: "
PASSAGE_PREFIX = "passage: "
MAX_TOKENS = 512  # max_position_embeddings: the embedder refuses a longer input
# A stored passage, PASSAGE_PREFIX and the special tokens included, counts at most this
# many tokens of the model's own tokenizer (app/embeddings/chunking.py; ADR 0005).
CHUNK_TOKENS = 400

# Path in the model repository at REVISION -> sha256 of its content. Only these
# files are downloaded (`make model`), and each is checked before every load
# (app/embeddings/model_files.py). The two ONNX runtime files are git LFS objects:
# their sha256 is the LFS oid at REVISION, and the downloaded bytes match it.
FILES: Mapping[str, str] = MappingProxyType(
    {
        "config.json": "69137736cab8b8903a07fe8afaafdda25aac55415a12a55d1bffa9f581abf959",
        "onnx/model.onnx": "ca456c06b3a9505ddfd9131408916dd79290368331e7d76bb621f1cba6bc8665",
        "onnx/tokenizer.json": "0b44a9d7b51c3c62626640cda0e2c2f70fdacdc25bbbd68038369d14ebdf4c39",
    }
)
