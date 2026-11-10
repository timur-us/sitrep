"""The real embedder (ADR 0005; plan M1, PR 4): what it computes, and what it loads.

Needs `make model` (tests/embeddings/conftest.py). What it computes: 384 floats of unit
length, the same in two processes, different for a query and a passage of the same
text, the same in a batch as alone, and closer for a paraphrase than for an unrelated
text, in German, English and across the two. What it loads: the bytes whose sha256
is pinned, handed to onnxruntime and tokenizers as bytes; a changed byte in any file,
a planted graph and a wrong config are refused before either library sees anything.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import onnx_proto as p
import pytest
from repo_files import ROOT

from app.config.embedding import DIM, FILES, MAX_TOKENS, MODEL_ID, REVISION
from app.embeddings.base import Embedder, passage_input, query_input
from app.embeddings.model_files import ModelFileError, model_dir, read_verified
from app.embeddings.onnx import (
    CONFIG,
    MODEL,
    TOKENIZER,
    EmbedderError,
    OnnxEmbedder,
    TextTooLong,
    load_tokenizer,
)
from app.embeddings.onnx_graph import graph_problems
from data.synthetic import generate

PROBE = (
    "import json, sys; from app.embeddings.onnx import OnnxEmbedder; e = OnnxEmbedder();"
    " v = [e.embed_query('Anleihenanteil'), *e.embed_passages(['Anleihen 40 %', 'Termin'])];"
    " print(json.dumps({'vectors': v, 'modules': sorted({m.partition('.')[0]"
    " for m in sys.modules})}))"
)
# Top-level modules that download or connect; none may be loaded by the embedder. Since
# M1 PR 6 the model client's SDK and its HTTP library are installed (ADR 0012), so they
# are named too.
NETWORK_MODULES = frozenset(
    {
        "aiohttp",
        "anthropic",
        "fsspec",
        "hf_xet",
        "httpcore",
        "httpcore2",
        "httpx",
        "httpx2",
        "huggingface_hub",
        "requests",
        "urllib3",
    }
)

# (query, paraphrase, unrelated): e5's cosines sit close together, so the margin is
# small but stable (measured 0.058 to 0.091 when this test was written).
PARAPHRASES = {
    "de": (
        "Wie hoch ist der Anleihenanteil im Depot?",
        "Der Anteil festverzinslicher Wertpapiere im Portfolio liegt bei 40 Prozent.",
        "Das Wetter in Hamburg ist heute regnerisch und kühl.",
    ),
    "de-meeting": (
        "Wann ist der nächste Gesprächstermin mit der Familie?",
        "Das nächste Treffen mit den Kunden ist für Dienstag vereinbart.",
        "Die Kantine bleibt am Freitag geschlossen.",
    ),
    "de-risk": (
        "Welche Risikobereitschaft hat die Kundin?",
        "Die Mandantin ist bereit, moderate Kursschwankungen in Kauf zu nehmen.",
        "Der Parkplatz wird nächste Woche neu markiert.",
    ),
    "en": (
        "How large is the bond allocation?",
        "Fixed-income securities make up 40 percent of the portfolio.",
        "The printer on the third floor is out of toner.",
    ),
    "en-to-de": (
        "How large is the bond allocation?",
        "Der Anteil festverzinslicher Wertpapiere im Portfolio liegt bei 40 Prozent.",
        "Das Wetter in Hamburg ist heute regnerisch und kühl.",
    ),
}
MARGIN = 0.03

# The recall sanity check (plan default 15): eight passages and four distractors.
PASSAGES = [
    "Die Familie wünscht eine Umschichtung von Aktien in Staatsanleihen bis Jahresende.",
    "Die Kundin hat ihre Adresse geändert und bittet um Zusendung der Unterlagen per Post.",
    "Im dritten Quartal erzielte das Depot eine Rendite von 2,1 Prozent nach Kosten.",
    "Der Kunde möchte 50.000 EUR für den Kauf einer Ferienwohnung entnehmen.",
    "The client asked to postpone the annual review meeting to January.",
    "The mandate's equity allocation exceeds the agreed maximum of 60 percent.",
    "A new sustainability fund was added to the product catalogue in October.",
    "The client confirmed that the source of funds is an inheritance from her father.",
    "Die Kantine bleibt am Freitag geschlossen.",
    "The printer on the third floor is out of toner.",
    "Der Parkplatz wird nächste Woche neu markiert.",
    "The office will close early on the last day of the year.",
]
SAME_LANGUAGE = [
    ("Möchte die Familie in Anleihen umschichten?", 0),
    ("Hat sich die Anschrift der Kundin geändert?", 1),
    ("Welche Performance hatte das Portfolio im letzten Quartal?", 2),
    ("Will der Kunde Geld für eine Immobilie abziehen?", 3),
    ("When does the client want the yearly review?", 4),
    ("Is the stock quota above its limit?", 5),
    ("Which ESG products are new?", 6),
    ("Where does the client's money come from?", 7),
]
# Measured when this test was written: English to German first, German to English
# third. Cross-language retrieval is weaker; PR 8 ranks with it in mind.
CROSS_LANGUAGE = [
    ("Does the family want to move from stocks into government bonds?", 0),
    ("Woher stammt das Vermögen der Kundin?", 7),
]


def dot(a: list[float], b: list[float]) -> float:
    return float(np.dot(a, b))


def rank(embedder: OnnxEmbedder, query: str, passages: list[list[float]], target: int) -> int:
    scores = np.array(passages) @ np.array(embedder.embed_query(query))
    return int(np.argsort(-scores, kind="stable").tolist().index(target)) + 1


def run_probe(hash_seed: str) -> dict[str, list]:
    env = {key: value for key, value in os.environ.items() if key != "PYTHONHASHSEED"}
    env["PYTHONHASHSEED"] = hash_seed
    result = subprocess.run(
        [sys.executable, "-P", "-c", PROBE],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.fixture(scope="module")
def probes() -> tuple[dict[str, list], dict[str, list]]:
    return run_probe("1"), run_probe("2")


def test_deterministic_across_processes(
    probes: tuple[dict[str, list], dict[str, list]], embedder: OnnxEmbedder
) -> None:
    first, second = probes
    assert first["vectors"] == second["vectors"], "the vectors depend on the process"
    here = [
        embedder.embed_query("Anleihenanteil"),
        *embedder.embed_passages(["Anleihen 40 %", "Termin"]),
    ]
    assert first["vectors"] == here


def test_no_hub_client_or_http_library_is_loaded(
    probes: tuple[dict[str, list], dict[str, list]],
) -> None:
    import importlib.util

    loaded = set(probes[0]["modules"])
    assert {"onnxruntime", "tokenizers", "numpy"} <= loaded, "the probe loaded no model"
    assert not loaded & NETWORK_MODULES, sorted(loaded & NETWORK_MODULES)
    # Not installed at all: pyproject.toml excludes it from the lock (tests/test_lock.py).
    assert importlib.util.find_spec("huggingface_hub") is None


def test_dimension_and_unit_norm(embedder: OnnxEmbedder) -> None:
    vectors = [embedder.embed_query("x"), *embedder.embed_passages(["Anleihen", "", "a" * 300])]
    for vector in vectors:
        assert len(vector) == DIM
        assert all(type(value) is float for value in vector)
        assert abs(dot(vector, vector) - 1.0) < 1e-9


def test_names_the_real_model(embedder: OnnxEmbedder) -> None:
    assert (embedder.model_id, embedder.revision, embedder.dim) == (MODEL_ID, REVISION, DIM)
    assert isinstance(embedder, Embedder)


def test_runs_on_the_cpu_only(embedder: OnnxEmbedder) -> None:
    assert embedder._session.get_providers() == ["CPUExecutionProvider"]


def test_query_and_passage_inputs_differ(embedder: OnnxEmbedder) -> None:
    for text in ("Anleihenanteil im Depot", "bond allocation"):
        query, (passage,) = embedder.embed_query(text), embedder.embed_passages([text])
        assert max(abs(a - b) for a, b in zip(query, passage, strict=True)) > 1e-3, text


def test_the_prefix_is_added_inside(
    embedder: OnnxEmbedder, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []
    real = embedder.tokenizer.ids

    def spy(text: str) -> list[int]:
        seen.append(text)
        return real(text)

    monkeypatch.setattr(embedder.tokenizer, "ids", spy)
    embedder.embed_query("Anleihen")
    embedder.embed_passages(["Termin", "bonds"])
    assert seen == [query_input("Anleihen"), passage_input("Termin"), passage_input("bonds")]


def test_batch_equals_single(embedder: OnnxEmbedder) -> None:
    # Lengths differ, so the batch pads and the mask has to hold.
    texts = ["Anleihen", "Der Anteil festverzinslicher Wertpapiere liegt bei 40 Prozent. " * 6]
    texts += [f"Gesprächsnotiz {n}: Laufzeit {n} Jahre" for n in range(12)]
    batch = embedder.embed_passages(texts)
    for text, vector in zip(texts, batch, strict=True):
        (alone,) = embedder.embed_passages([text])
        assert max(abs(a - b) for a, b in zip(vector, alone, strict=True)) < 1e-5, text
    assert embedder.embed_passages([]) == []


@pytest.mark.parametrize("case", PARAPHRASES, ids=str)
def test_a_paraphrase_lies_closer_than_an_unrelated_text(embedder: OnnxEmbedder, case: str) -> None:
    query, paraphrase, unrelated = PARAPHRASES[case]
    q = embedder.embed_query(query)
    near, far = embedder.embed_passages([paraphrase, unrelated])
    assert dot(q, near) > dot(q, far) + MARGIN, (case, dot(q, near), dot(q, far))


def test_recall_sanity_check(embedder: OnnxEmbedder) -> None:
    passages = embedder.embed_passages(PASSAGES)
    ranks = {query: rank(embedder, query, passages, target) for query, target in SAME_LANGUAGE}
    assert all(value == 1 for value in ranks.values()), ranks
    cross = {query: rank(embedder, query, passages, target) for query, target in CROSS_LANGUAGE}
    assert all(value <= 3 for value in cross.values()), cross


def test_no_input_is_truncated(embedder: OnnxEmbedder) -> None:
    # "a a a ..." is one token per word; the prefix and the special tokens come on top.
    words = MAX_TOKENS - embedder.tokenizer.count(query_input("a")) + 1
    fits = " ".join(["a"] * words)
    assert embedder.tokenizer.count(query_input(fits)) == MAX_TOKENS
    assert len(embedder.embed_query(fits)) == DIM
    with pytest.raises(TextTooLong) as error:
        embedder.embed_query(fits + " a")
    assert str(error.value) == f"{MAX_TOKENS + 1} tokens; the model takes at most {MAX_TOKENS}"
    # A batch is counted before any of it runs.
    with pytest.raises(TextTooLong):
        embedder.embed_passages(["short", fits + " a a a"])


@pytest.mark.parametrize("texts", ["Anleihen", b"bonds", [b"bonds"], [None]], ids=str)
def test_refuses_what_is_not_text(embedder: OnnxEmbedder, texts: object) -> None:
    with pytest.raises(TypeError):
        embedder.embed_passages(texts)  # type: ignore[arg-type]
    one = texts if isinstance(texts, str | bytes) else texts[0]  # type: ignore[index]
    if not isinstance(one, str):
        with pytest.raises(TypeError):
            embedder.embed_query(one)  # type: ignore[arg-type]


def test_the_pinned_model_passes_the_graph_check() -> None:
    assert graph_problems(read_verified(model_dir(), MODEL)) == []


def test_speed(embedder: OnnxEmbedder, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # The plan's evidence: load time and passages per second, printed past the capture
    # so the CI log shows them. The floor only catches a broken runtime.
    out = tmp_path / "corpus"
    assert generate.main(["--out", str(out)]) == 0
    with (out / "documents.jsonl").open(encoding="utf-8") as lines:
        documents = [json.loads(line) for line in lines][:64]
    texts = [f"{row['title']}\n\n{row['body']}" for row in documents]
    started = time.perf_counter()
    OnnxEmbedder()
    load = time.perf_counter() - started
    started = time.perf_counter()
    embedder.embed_passages(texts)
    seconds = time.perf_counter() - started
    tokens = sum(embedder.tokenizer.count(passage_input(text)) for text in texts) / len(texts)
    rate = len(texts) / seconds
    with capsys.disabled():
        print(
            f"\nembedder: load {load:.2f} s; {len(texts)} passages of {tokens:.0f} tokens"
            f" on average in {seconds:.2f} s, {rate:.1f} passages/s"
        )
    assert rate > 5, rate


class Spy:
    """Records each call, then makes it."""

    def __init__(self, real: object) -> None:
        self.real, self.calls = real, []

    def __call__(self, *args: object, **kwargs: object) -> object:
        self.calls.append((args, kwargs))
        return self.real(*args, **kwargs)  # type: ignore[operator]


@pytest.fixture
def spies(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """onnxruntime.InferenceSession and tokenizers.Tokenizer.from_str, recorded."""
    import onnxruntime
    import tokenizers

    session = Spy(onnxruntime.InferenceSession)
    from_str = Spy(tokenizers.Tokenizer.from_str)
    monkeypatch.setattr(onnxruntime, "InferenceSession", session)
    monkeypatch.setattr(tokenizers, "Tokenizer", SimpleNamespace(from_str=from_str))
    return SimpleNamespace(session=session, tokenizer=from_str)


@pytest.fixture(scope="module")
def model_copy(tmp_path_factory: pytest.TempPathFactory, embedder: OnnxEmbedder) -> Iterator[Path]:
    """A copy of the model directory, deleted afterwards (the model is 470 MB)."""
    target = tmp_path_factory.mktemp("model")
    for rel in FILES:
        (target / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(model_dir() / rel, target / rel)
    yield target
    shutil.rmtree(target)


def flip(path: Path) -> None:
    """Change one bit of one byte in the middle of the file; a second call undoes it."""
    with path.open("r+b") as handle:
        handle.seek(path.stat().st_size // 2)
        byte = handle.read(1)
        handle.seek(-1, os.SEEK_CUR)
        handle.write(bytes([byte[0] ^ 1]))


def test_the_verified_bytes_are_what_loads(model_copy: Path, spies: SimpleNamespace) -> None:
    OnnxEmbedder(model_copy)
    ((args, kwargs),) = spies.session.calls
    model = args[0]
    assert type(model) is bytes and hashlib.sha256(model).hexdigest() == FILES[MODEL]
    assert kwargs["providers"] == ["CPUExecutionProvider"]
    options = kwargs["sess_options"]
    assert options.get_session_config_entry("session.load_model_format") == "ONNX"
    assert options.use_deterministic_compute is True
    # Review finding sec-3: the model's own metadata never configures the session.
    assert kwargs["read_config_from_model"] == 0
    ((tokenizer_args, _),) = spies.tokenizer.calls
    (text,) = tokenizer_args
    assert hashlib.sha256(text.encode("utf-8")).hexdigest() == FILES[TOKENIZER]


@pytest.mark.parametrize("rel", sorted(FILES))
def test_a_changed_byte_is_refused_before_loading(
    rel: str, model_copy: Path, spies: SimpleNamespace
) -> None:
    flip(model_copy / rel)
    try:
        with pytest.raises(ModelFileError, match=f"^{re.escape(rel)}: sha256 differs from FILES"):
            OnnxEmbedder(model_copy)
        if rel == TOKENIZER:
            with pytest.raises(ModelFileError, match="sha256 differs"):
                load_tokenizer(model_copy)
    finally:
        flip(model_copy / rel)
    assert spies.session.calls == [] and spies.tokenizer.calls == []


def test_a_missing_file_is_refused(model_copy: Path, spies: SimpleNamespace) -> None:
    moved = model_copy / "moved.json"
    (model_copy / CONFIG).rename(moved)
    try:
        with pytest.raises(ModelFileError, match=r"^config\.json: missing \(run make model\)$"):
            OnnxEmbedder(model_copy)
    finally:
        moved.rename(model_copy / CONFIG)
    assert spies.session.calls == []


def plant(directory: Path, model: bytes, config: bytes = b'{"hidden_size": 384}') -> dict[str, str]:
    """A model directory with the real tokenizer, and the files mapping that pins it."""
    contents = {CONFIG: config, MODEL: model, TOKENIZER: read_verified(model_dir(), TOKENIZER)}
    for rel, data in contents.items():
        (directory / rel).parent.mkdir(parents=True, exist_ok=True)
        (directory / rel).write_bytes(data)
    return {rel: hashlib.sha256(data).hexdigest() for rel, data in contents.items()}


PLANTED = {
    "external data": (
        p.add_model(weights=p.tensor("W", (0.0, 0.0), location="w.bin")),
        "graph.initializer[0]: data_location EXTERNAL",
    ),
    "a custom domain": (
        p.unary_model("Op", domain="custom.example", opsets=[(None, 17), ("custom.example", 1)]),
        "opset_import[1]: operator domain 'custom.example' is not allowed",
    ),
    "not ONNX": (b"\x00" * 64, "not a well-formed ONNX protobuf"),
}


@pytest.mark.parametrize("case", PLANTED, ids=str)
def test_a_planted_graph_is_refused_before_loading(
    case: str, tmp_path: Path, spies: SimpleNamespace, embedder: OnnxEmbedder
) -> None:
    model, problem = PLANTED[case]
    files = plant(tmp_path, model)
    with pytest.raises(EmbedderError) as error:
        OnnxEmbedder(tmp_path, files=files)
    assert str(error.value).startswith(f"{MODEL}: refused: {problem}"), str(error.value)
    assert spies.session.calls == [] and spies.tokenizer.calls == []


def test_a_clean_planted_graph_reaches_onnxruntime(
    tmp_path: Path, spies: SimpleNamespace, embedder: OnnxEmbedder
) -> None:
    # Control for the test above: past the graph check, the bytes do reach both
    # libraries; this model's inputs are not the embedder's, so it is refused after.
    model = p.add_model()
    files = plant(tmp_path, model)
    with pytest.raises(EmbedderError, match="inputs"):
        OnnxEmbedder(tmp_path, files=files)
    ((args, _),) = spies.session.calls
    assert args[0] == model
    assert len(spies.tokenizer.calls) == 1


def test_a_wrong_config_is_refused_before_loading(
    tmp_path: Path, spies: SimpleNamespace, embedder: OnnxEmbedder
) -> None:
    for config in (b'{"hidden_size": 768}', b"[384]", b"not json"):
        files = plant(tmp_path, p.add_model(), config=config)
        with pytest.raises(EmbedderError, match=r"^config\.json: "):
            OnnxEmbedder(tmp_path, files=files)
    assert spies.session.calls == [] and spies.tokenizer.calls == []


def test_files_must_be_the_three(tmp_path: Path) -> None:
    for files in ({}, {CONFIG: "0" * 64}, dict(FILES) | {"onnx/extra.onnx": "0" * 64}):
        with pytest.raises(ModelFileError, match="files must be exactly"):
            OnnxEmbedder(tmp_path, files=files)


# Review finding test-10: the session's inputs and output, each refused alone, with a
# model shaped like the embedder's that does load (the control).
SESSION_INPUTS = (
    f"{MODEL}: inputs are not the expected int64 inputs (input_ids, attention_mask,"
    " optionally token_type_ids)"
)
SESSION_OUTPUT = f"{MODEL}: the first output is not last_hidden_state with {DIM} dimensions"


@pytest.mark.parametrize(
    ("planted", "message"),
    [
        ({"output": "pooler_output"}, SESSION_OUTPUT),
        ({"width": 768}, SESSION_OUTPUT),
        ({"elem_type": p.FLOAT}, SESSION_INPUTS),
    ],
    ids=["output name", "output width", "input type"],
)
def test_the_session_must_match_the_embedder(
    tmp_path: Path, embedder: OnnxEmbedder, planted: dict[str, int | str], message: str
) -> None:
    assert isinstance(
        OnnxEmbedder(tmp_path, files=plant(tmp_path, p.encoder_model())), OnnxEmbedder
    )
    files = plant(tmp_path, p.encoder_model(**planted))
    with pytest.raises(EmbedderError) as error:
        OnnxEmbedder(tmp_path, files=files)
    assert str(error.value) == message


def test_a_refusal_names_no_model_input(tmp_path: Path, embedder: OnnxEmbedder) -> None:
    # Review finding sec-10: the message carries the expected names, never the model's.
    files = plant(tmp_path, p.add_model())  # its input is "X"
    with pytest.raises(EmbedderError) as error:
        OnnxEmbedder(tmp_path, files=files)
    assert str(error.value) == SESSION_INPUTS


ORT_CONFIG = '{"session_options": {"enable_profiling": 1}}'


@pytest.mark.parametrize("value", ["1", "0", ""])
def test_the_model_cannot_configure_the_session(
    model_copy: Path, spies: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    # Review finding sec-3: onnxruntime's wrapper reads ORT_LOAD_CONFIG_FROM_MODEL, and
    # then the model's metadata sets session options. While it is set at all, the
    # embedder refuses before onnxruntime is called.
    monkeypatch.setenv("ORT_LOAD_CONFIG_FROM_MODEL", value)
    with pytest.raises(EmbedderError, match="ORT_LOAD_CONFIG_FROM_MODEL is set"):
        OnnxEmbedder(model_copy)
    assert spies.session.calls == []


def test_a_model_config_changes_the_session_without_the_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Control for the test above: with the variable and no read_config_from_model=0, a
    # planted ort_config turns profiling on, and onnxruntime writes a file.
    import onnxruntime

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ORT_LOAD_CONFIG_FROM_MODEL", "1")
    model = p.add_model() + p.field(14, p.entry("ort_config", ORT_CONFIG))
    session = onnxruntime.InferenceSession(model, providers=["CPUExecutionProvider"])
    del session
    assert [path.name.startswith("onnxruntime_profile_") for path in tmp_path.iterdir()] == [True]
