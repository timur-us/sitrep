"""The real embedder: intfloat/multilingual-e5-small in onnxruntime, on the CPU (ADR 0005).

The only module in app/ that imports onnxruntime, tokenizers or numpy
(tests/test_import_allowlist.py).

Loading, in this order, before onnxruntime or tokenizers sees a byte:
1. every file of FILES is read once through model_files.read_verified(), which checks
   its sha256; nothing else is opened;
2. the model's config.json names DIM;
3. onnx_graph.graph_problems() refuses external data and any operator domain but the
   standard one.

Then tokenizers and onnxruntime get those verified bytes, never a path. The session runs
on CPUExecutionProvider alone (the wheel also ships an Azure provider that calls remote
endpoints), takes the ONNX format only, computes deterministically, and registers no
custom operator library. The model's metadata never configures it: onnxruntime's wrapper
would take session options from it while ORT_LOAD_CONFIG_FROM_MODEL is set, so the
session is built with read_config_from_model=0 and refused while that variable is set at
all (review finding sec-3). Its inputs and output must be the ones this module feeds and
reads; a refusal names the expected ones, never the model's.

Embedding follows the model card at REVISION: the prefixed text through the model's own
tokenizer, the last hidden state averaged over the attention mask (mean pooling), then
divided by its L2 norm. Nothing is truncated: an input over MAX_TOKENS tokens, prefix
and special tokens included, is refused with TextTooLong; app/embeddings/chunking.py
keeps stored passages at CHUNK_TOKENS or fewer.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path

import numpy as np
import onnxruntime as ort
import tokenizers

from app.config.embedding import DIM, FILES, MAX_TOKENS, MODEL_ID, REVISION
from app.embeddings.base import passage_list, query_input
from app.embeddings.model_files import ModelFileError, model_dir, read_verified
from app.embeddings.onnx_graph import graph_problems

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
PROVIDERS = ["CPUExecutionProvider"]
# The inputs the embedder feeds (all int64, [batch, sequence]) and the output it reads.
INPUTS = ("input_ids", "attention_mask", "token_type_ids")
REQUIRED_INPUTS = frozenset({"input_ids", "attention_mask"})
OUTPUT = "last_hidden_state"
# onnxruntime's wrapper reads it, and then lets the model's metadata set session options.
CONFIG_FROM_MODEL = "ORT_LOAD_CONFIG_FROM_MODEL"
# Texts per run of the model. Attention holds batch x heads x length^2 floats:
# 8 passages of 400 tokens take about 60 MB.
BATCH = 8
_EPS = 1e-12  # the model card's F.normalize: the norm is at least this


class EmbedderError(Exception):
    """The model or tokenizer is not what this module was written for."""


class TextTooLong(ValueError):
    """An input over MAX_TOKENS tokens. The message carries counts, never the text."""


class ModelTokenizer:
    """The model's own tokenizer, built from verified bytes; no truncation, no padding."""

    def __init__(self, tokenizer_json: bytes) -> None:
        tokenizer = tokenizers.Tokenizer.from_str(tokenizer_json.decode("utf-8"))
        tokenizer.no_truncation()
        tokenizer.no_padding()
        self._tokenizer = tokenizer
        pad = tokenizer.token_to_id("<pad>")
        if pad is None:
            raise EmbedderError(f"{TOKENIZER}: no <pad> token")
        self.pad_id: int = pad

    def _encode(self, text: str, special: bool) -> tokenizers.Encoding:
        if type(text) is not str:
            raise TypeError(f"the tokenizer takes str, not {type(text).__name__}")
        return self._tokenizer.encode(text, add_special_tokens=special)

    def ids(self, text: str) -> list[int]:
        """The ids the model sees for text as given, special tokens included."""
        return self._encode(text, True).ids

    def count(self, text: str) -> int:
        """How many tokens the model sees for text as given, special tokens included."""
        return len(self.ids(text))

    def offsets(self, text: str) -> list[tuple[int, int]]:
        """(start, end) in characters of text for each token, special tokens left out."""
        return [tuple(span) for span in self._encode(text, False).offsets]


def load_tokenizer(
    directory: Path | None = None, *, files: Mapping[str, str] = FILES
) -> ModelTokenizer:
    """The tokenizer alone, for the chunker; tokenizer.json is checked like every load."""
    folder = model_dir() if directory is None else directory
    return ModelTokenizer(read_verified(folder, TOKENIZER, files))


def _check_config(config_json: bytes) -> None:
    try:
        config = json.loads(config_json)
    except ValueError:
        raise EmbedderError(f"{CONFIG}: not JSON") from None
    if not isinstance(config, dict) or config.get("hidden_size") != DIM:
        raise EmbedderError(f"{CONFIG}: hidden_size is not {DIM}")


def _session(model: bytes) -> ort.InferenceSession:
    if CONFIG_FROM_MODEL in os.environ:
        raise EmbedderError(
            f"{CONFIG_FROM_MODEL} is set: the model's metadata would configure the session"
        )
    options = ort.SessionOptions()
    # The bytes are an ONNX protobuf: never parsed as onnxruntime's own ORT format.
    options.add_session_config_entry("session.load_model_format", "ONNX")
    options.use_deterministic_compute = True
    options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    options.inter_op_num_threads = 1
    options.log_severity_level = 3  # errors only
    session = ort.InferenceSession(
        model, sess_options=options, providers=PROVIDERS, read_config_from_model=0
    )
    if session.get_providers() != PROVIDERS:
        raise EmbedderError(f"{MODEL}: providers {session.get_providers()}, expected {PROVIDERS}")
    inputs = {item.name: item.type for item in session.get_inputs()}
    if not REQUIRED_INPUTS <= inputs.keys() <= set(INPUTS) or any(
        kind != "tensor(int64)" for kind in inputs.values()
    ):
        raise EmbedderError(
            f"{MODEL}: inputs are not the expected int64 inputs"
            " (input_ids, attention_mask, optionally token_type_ids)"
        )
    outputs = session.get_outputs()
    if not outputs or outputs[0].name != OUTPUT or outputs[0].shape[-1:] != [DIM]:
        raise EmbedderError(f"{MODEL}: the first output is not {OUTPUT} with {DIM} dimensions")
    return session


class OnnxEmbedder:
    """The Embedder of app/embeddings/base.py, for intfloat/multilingual-e5-small.

    `directory` defaults to model_files.model_dir(). `files` is for tests that plant
    files with their own hashes; the app never passes it.
    """

    model_id = MODEL_ID
    revision = REVISION
    dim = DIM

    def __init__(self, directory: Path | None = None, *, files: Mapping[str, str] = FILES) -> None:
        folder = model_dir() if directory is None else directory
        if set(files) != {CONFIG, MODEL, TOKENIZER}:
            raise ModelFileError(f"files must be exactly {sorted({CONFIG, MODEL, TOKENIZER})}")
        verified = {rel: read_verified(folder, rel, files) for rel in sorted(files)}
        _check_config(verified[CONFIG])
        problems = graph_problems(verified[MODEL])
        if problems:
            raise EmbedderError(f"{MODEL}: refused: " + "; ".join(problems[:5]))
        self.tokenizer = ModelTokenizer(verified[TOKENIZER])
        self._session = _session(verified[MODEL])
        self._inputs = [item.name for item in self._session.get_inputs()]

    def embed_query(self, text: str) -> list[float]:
        return self._embed([query_input(text)])[0]

    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        return self._embed(passage_list(texts))

    def _embed(self, inputs: list[str]) -> list[list[float]]:
        """One unit vector per input text, in order; each text already carries its prefix."""
        ids = [self.tokenizer.ids(text) for text in inputs]
        for tokens in ids:
            if len(tokens) > MAX_TOKENS:
                raise TextTooLong(f"{len(tokens)} tokens; the model takes at most {MAX_TOKENS}")
        vectors: list[list[float]] = [[] for _ in ids]
        # Similar lengths together, so a batch carries little padding.
        order = sorted(range(len(ids)), key=lambda index: len(ids[index]))
        for start in range(0, len(order), BATCH):
            batch = order[start : start + BATCH]
            for index, vector in zip(batch, self._run([ids[i] for i in batch]), strict=True):
                vectors[index] = vector
        return vectors

    def _run(self, batch: list[list[int]]) -> list[list[float]]:
        length = max(len(tokens) for tokens in batch)
        input_ids = np.full((len(batch), length), self.tokenizer.pad_id, dtype=np.int64)
        mask = np.zeros((len(batch), length), dtype=np.int64)
        for row, tokens in enumerate(batch):
            input_ids[row, : len(tokens)] = tokens
            mask[row, : len(tokens)] = 1
        feeds = {"input_ids": input_ids, "attention_mask": mask}
        if "token_type_ids" in self._inputs:
            feeds["token_type_ids"] = np.zeros_like(input_ids)
        (hidden,) = self._session.run([OUTPUT], feeds)
        # Mean pooling over the real tokens, then the L2 norm (the model card's
        # average_pool and F.normalize), in float64.
        weights = mask[..., None].astype(np.float64)
        pooled = (hidden.astype(np.float64) * weights).sum(axis=1) / weights.sum(axis=1)
        norms = np.maximum(np.linalg.norm(pooled, axis=1, keepdims=True), _EPS)
        return (pooled / norms).tolist()
