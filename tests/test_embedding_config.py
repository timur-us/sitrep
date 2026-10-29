"""The embedding constants are anchored to the real model and agree with ADR 0005.

app/config/embedding_model_config.json is the model's config.json at REVISION,
committed as data. Its sha256 is in FILES, so DIM and MAX_TOKENS are checked
against a hash-pinned artefact of the real model, not against a number someone
typed twice.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import PurePosixPath
from types import ModuleType, SimpleNamespace

import yaml
from repo_files import ROOT

from app.config import embedding

MODEL_CONFIG = ROOT / "app" / "config" / "embedding_model_config.json"
CONFIG_PATH = "config.json"  # its path in the model repository, a key of FILES
DECISIONS = ROOT / "docs" / "decisions"

CONSTANTS = (
    "MODEL_ID",
    "REVISION",
    "DIM",
    "QUERY_PREFIX",
    "PASSAGE_PREFIX",
    "MAX_TOKENS",
    "FILES",
)
SHA256 = re.compile(r"[0-9a-f]{64}")
COMMIT = re.compile(r"[0-9a-f]{40}")
# Formats that can run code when loaded (pickle and its wrappers).
PICKLE_SUFFIXES = {".bin", ".pt", ".pth", ".pkl", ".pickle", ".ckpt"}
YAML_BLOCK = re.compile(r"^```yaml\n(.*?)^```", re.DOTALL | re.MULTILINE)


def yaml_mismatches(adr: str, module: ModuleType | SimpleNamespace) -> list[str]:
    """Differences between the ADR's one yaml block and the module's constants."""
    blocks = YAML_BLOCK.findall(adr)
    if len(blocks) != 1:
        return [f"expected one yaml block, found {len(blocks)}"]
    facts = yaml.safe_load(blocks[0])
    if not isinstance(facts, dict):
        return ["the yaml block is not a mapping"]
    expected = {name.lower(): getattr(module, name, None) for name in CONSTANTS}
    problems = [f"{key}: not in the ADR" for key in expected if key not in facts]
    problems += [f"{key}: in the ADR, not a constant" for key in facts if key not in expected]
    for key, value in expected.items():
        if key in facts and key != "files" and facts[key] != value:
            problems.append(f"{key}: ADR {facts[key]!r}, embedding.py {value!r}")
    if isinstance(facts.get("files"), dict):
        files = dict(expected["files"] or {})
        for path in sorted(files.keys() | facts["files"].keys()):
            if facts["files"].get(path) != files.get(path):
                problems.append(f"files[{path}]: ADR and embedding.py differ")
    elif "files" in facts:
        problems.append("files: not a mapping in the ADR")
    return problems


def _adr_0005() -> str:
    found = sorted(DECISIONS.glob("0005-*.md")) if DECISIONS.is_dir() else []
    assert len(found) == 1, f"expected one ADR 0005 in docs/decisions, found {len(found)}"
    return found[0].read_text(encoding="utf-8")


def test_constants_are_exported() -> None:
    assert set(embedding.__all__) == set(CONSTANTS), f"embedding.__all__ = {embedding.__all__}"


def test_revision_is_a_commit() -> None:
    assert COMMIT.fullmatch(embedding.REVISION), (
        f"REVISION {embedding.REVISION!r} is not a 40-hex commit (a branch or tag can move)"
    )
    assert re.fullmatch(r"[\w.-]+/[\w.-]+", embedding.MODEL_ID), (
        f"MODEL_ID {embedding.MODEL_ID!r} is not <org>/<name>"
    )


def test_dim_anchored_to_model() -> None:
    files = embedding.FILES
    assert files, "FILES is empty"
    bad = [path for path, digest in files.items() if not SHA256.fullmatch(digest)]
    assert not bad, f"not 64 lowercase hex: {bad}"
    pickled = [path for path in files if PurePosixPath(path).suffix in PICKLE_SUFFIXES]
    assert not pickled, f"pickle-based files in FILES: {pickled}"

    assert MODEL_CONFIG.is_file(), f"{MODEL_CONFIG.relative_to(ROOT)} is missing"
    assert CONFIG_PATH in files, f"FILES has no {CONFIG_PATH!r}"
    digest = hashlib.sha256(MODEL_CONFIG.read_bytes()).hexdigest()
    assert digest == files[CONFIG_PATH], "the committed config.json does not match its sha256"

    config = json.loads(MODEL_CONFIG.read_text(encoding="utf-8"))
    assert config["hidden_size"] == embedding.DIM, (
        f"hidden_size {config['hidden_size']} != DIM {embedding.DIM}"
    )
    assert 0 < embedding.MAX_TOKENS <= config["max_position_embeddings"], (
        f"MAX_TOKENS {embedding.MAX_TOKENS} outside 1..{config['max_position_embeddings']}"
    )


def test_files_is_read_only() -> None:
    try:
        embedding.FILES["extra.onnx"] = "0" * 64  # type: ignore[index]
    except TypeError:
        return
    raise AssertionError("FILES can be changed at runtime")


def test_prefixes() -> None:
    prefixes = (embedding.QUERY_PREFIX, embedding.PASSAGE_PREFIX)
    assert all(prefixes), f"empty prefix: {prefixes}"
    assert embedding.QUERY_PREFIX != embedding.PASSAGE_PREFIX, "query and passage prefix are equal"


def test_adr_0005_matches_config() -> None:
    problems = yaml_mismatches(_adr_0005(), embedding)
    assert not problems, "ADR 0005 and app/config/embedding.py differ:\n" + "\n".join(problems)


def test_planted_mismatches() -> None:
    module = SimpleNamespace(
        MODEL_ID="org/model",
        REVISION="a" * 40,
        DIM=384,
        QUERY_PREFIX="query: ",
        PASSAGE_PREFIX="passage: ",
        MAX_TOKENS=512,
        FILES={"config.json": "b" * 64, "onnx/model.onnx": "c" * 64},
    )
    good = (
        "```yaml\nmodel_id: org/model\nrevision: " + "a" * 40 + "\ndim: 384\n"
        'query_prefix: "query: "\npassage_prefix: "passage: "\nmax_tokens: 512\n'
        "files:\n  config.json: " + "b" * 64 + "\n  onnx/model.onnx: " + "c" * 64 + "\n```\n"
    )
    assert yaml_mismatches(good, module) == []

    changed = (
        good.replace("dim: 384", "dim: 768")
        .replace('query_prefix: "query: "', "query_prefix: query:x")
        .replace("max_tokens: 512\n", "max_tokens: 512\nlicence: mit\n")
        .replace("  onnx/model.onnx: " + "c" * 64 + "\n", "  onnx/model.onnx: " + "d" * 64 + "\n")
    )
    assert yaml_mismatches(changed, module) == [
        "licence: in the ADR, not a constant",
        "dim: ADR 768, embedding.py 384",
        "query_prefix: ADR 'query:x', embedding.py 'query: '",
        "files[onnx/model.onnx]: ADR and embedding.py differ",
    ]
    assert yaml_mismatches(good.replace("dim: 384\n", ""), module) == ["dim: not in the ADR"]
    assert yaml_mismatches(good + good, module) == ["expected one yaml block, found 2"]
    assert yaml_mismatches("no block\n", module) == ["expected one yaml block, found 0"]
