"""The fake embedder has the real model's shape and cannot be mistaken for it (plan PR 6).

FakeEmbedder stands in for intfloat/multilingual-e5-small until M1 (ADR 0005): DIM
dimensions, unit length, the query and passage prefixes applied inside, deterministic
from a hash of the prefixed text (not Python's hash(), which changes per process). Its
model id starts with "fake/" and its revision is "fake", and both are stored with every
vector, so a fake vector never passes for a real one.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from itertools import combinations

import pytest
from repo_files import ROOT

from app.config.embedding import DIM, MODEL_ID, PASSAGE_PREFIX, QUERY_PREFIX, REVISION
from app.embeddings.base import Embedder, passage_input, query_input
from app.embeddings.fake import FakeEmbedder, fake_vector

TEXTS = [f"Gesprächsnotiz {n}: Anleihen, Laufzeit {n} Jahre" for n in range(50)] + [
    f"Call note {n}: bonds, maturity {n} years" for n in range(50)
]

PROBE = (
    "import json; from app.embeddings.fake import FakeEmbedder; e = FakeEmbedder();"
    " print(json.dumps([e.embed_query('Anleihen'), e.embed_passages(['bonds', 'Termin'])]))"
)


def dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


def run_probe(hash_seed: str) -> object:
    env = {key: value for key, value in os.environ.items() if key != "PYTHONHASHSEED"}
    env["PYTHONHASHSEED"] = hash_seed
    result = subprocess.run(
        [sys.executable, "-P", "-c", PROBE],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_deterministic_across_processes() -> None:
    first, second = run_probe("1"), run_probe("2")
    assert first == second, "the vectors depend on the process"
    fake = FakeEmbedder()
    assert first == [fake.embed_query("Anleihen"), fake.embed_passages(["bonds", "Termin"])]


def test_distinct_texts_give_distinct_vectors() -> None:
    vectors = FakeEmbedder().embed_passages(TEXTS)
    assert len(vectors) == 100
    worst = max(dot(a, b) for a, b in combinations(vectors, 2))
    assert worst < 0.99, f"pairwise cosine up to {worst}"


def test_query_and_passage_differ() -> None:
    fake = FakeEmbedder()
    for text in TEXTS[:5]:
        assert fake.embed_query(text) != fake.embed_passages([text])[0], text


def test_prefixes_are_applied_inside() -> None:
    assert query_input("Anleihen") == QUERY_PREFIX + "Anleihen"
    assert passage_input("Anleihen") == PASSAGE_PREFIX + "Anleihen"
    fake = FakeEmbedder()
    assert fake.embed_query("Anleihen") == fake_vector(QUERY_PREFIX + "Anleihen")
    assert fake.embed_passages(["Anleihen"]) == [fake_vector(PASSAGE_PREFIX + "Anleihen")]


def test_batch_equals_single() -> None:
    fake = FakeEmbedder()
    assert fake.embed_passages(TEXTS[:10]) == [fake.embed_passages([t])[0] for t in TEXTS[:10]]
    assert fake.embed_passages([]) == []


def test_unit_norm_and_dimension() -> None:
    fake = FakeEmbedder()
    for vector in [fake.embed_query("x"), *fake.embed_passages(TEXTS[:20] + [""])]:
        assert len(vector) == DIM
        assert all(type(value) is float for value in vector)
        assert abs(dot(vector, vector) - 1.0) < 1e-9


def test_cannot_be_mistaken_for_the_model() -> None:
    fake = FakeEmbedder()
    assert fake.model_id.startswith("fake/")
    assert fake.model_id == "fake/" + MODEL_ID and fake.model_id != MODEL_ID
    assert fake.revision == "fake" and fake.revision != REVISION
    assert fake.dim == DIM
    assert isinstance(fake, Embedder)


@pytest.mark.parametrize("texts", ["Anleihen", b"bonds", [b"bonds"], [None]], ids=str)
def test_refuses_what_is_not_text(texts: object) -> None:
    with pytest.raises(TypeError):
        FakeEmbedder().embed_passages(texts)  # type: ignore[arg-type]
    if isinstance(texts, str | bytes):
        return
    with pytest.raises(TypeError):
        FakeEmbedder().embed_query(texts[0])  # type: ignore[index]
