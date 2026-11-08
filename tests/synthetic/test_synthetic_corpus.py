"""The synthetic corpus (`data/synthetic/generate.py`, M0 PR 8).

Deterministic, owned by the right advisor, labelled by type, free of real-looking data and
consistent in its figures. Each test reads a corpus generated into a temporary directory:
`data/synthetic/out/` is git-ignored and may be stale or absent.
"""

from __future__ import annotations

import ast
import collections
import hashlib
import inspect
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from corpus_checks import (
    attack_markers,
    figure_problems,
    foreign_hosts,
    ibans,
    phones,
    report_problems,
    reserved_domains,
    streets,
)
from repo_files import ROOT
from secret_patterns import find_secrets

from app.config import branding
from app.runners import corpus as runner_corpus
from data.synthetic import generate

# The sha256 of manifest.json for the committed templates and names. A change to the
# generator, a template or the names list changes it; update it on purpose, in the same PR.
GOLDEN_MANIFEST_SHA256 = "02235aa49e3b989562a6f9f573c4480f73d50ef3c7c340ee92a6a874d7d47177"

DATA_FILES = ("advisors.jsonl", "documents.jsonl", "mandates.jsonl", "product_sheets.jsonl")
FILES = (*DATA_FILES, "manifest.json")
LANGS = ("english", "german")
DOC_TYPES = ("kyc", "portfolio_report", "call_note", "email", "product_sheet")


def _jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.fixture(scope="module")
def out(tmp_path_factory: pytest.TempPathFactory) -> Path:
    directory = tmp_path_factory.mktemp("corpus")
    assert generate.main(["--out", str(directory)]) == 0
    return directory


@pytest.fixture(scope="module")
def corpus(out: Path) -> dict[str, list[dict]]:
    return {name: _jsonl(out / name) for name in DATA_FILES if (out / name).is_file()}


def _files(directory: Path) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in sorted(directory.glob("*"))}


def _run_generator(out: Path, cwd: Path, hash_seed: str, tz: str, lc_all: str) -> None:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONHASHSEED": hash_seed,
        "TZ": tz,
        "LC_ALL": lc_all,
        "NO_PROXY": "*",
    }
    result = subprocess.run(
        [sys.executable, "-P", "-m", "data.synthetic.generate", "--out", str(out)],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, f"generator exited {result.returncode}: {result.stderr}"


def test_deterministic(tmp_path: Path, out: Path) -> None:
    # Two processes that differ in hash seed, time zone and locale, started outside the
    # repository, and this process: byte-identical files.
    first, second = tmp_path / "first", tmp_path / "second"
    _run_generator(first, tmp_path, "0", "UTC", "C")
    _run_generator(second, tmp_path, "4242", "Pacific/Chatham", "de_DE.UTF-8")
    files = _files(first)
    assert sorted(files) == sorted(FILES), f"files written: {sorted(files)}"
    assert files == _files(second), "two runs differ"
    assert files == _files(out), "the subprocess and the in-process run differ"

    manifest = json.loads(files["manifest.json"])
    assert manifest["seed"] == generate.SEED and manifest["generator_version"]
    assert sorted(manifest) == ["files", "generator_version", "seed"], sorted(manifest)
    hashes = {name: hashlib.sha256(files[name]).hexdigest() for name in DATA_FILES}
    assert manifest["files"] == hashes, "the manifest does not match the files"
    digest = hashlib.sha256(files["manifest.json"]).hexdigest()
    assert digest == GOLDEN_MANIFEST_SHA256, f"manifest sha256 {digest}"


def test_counts_and_ownership(corpus: dict[str, list[dict]]) -> None:
    advisors = branding.ADVISORS
    demo = branding.DEMO_MANDATE
    assert corpus.get("advisors.jsonl") == [
        {"advisor_id": person.id, "display_name": person.name, "email": person.email}
        for person in advisors
    ], "advisors.jsonl is not the advisors from branding"

    mandates = corpus.get("mandates.jsonl", [])
    assert len(mandates) == 40, f"{len(mandates)} mandates"
    per_advisor = collections.Counter(mandate["advisor_id"] for mandate in mandates)
    expected = {person.id: 13 for person in advisors} | {demo.advisor_id: 14}
    assert per_advisor == expected, f"mandates per advisor: {dict(per_advisor)}"
    assert min(per_advisor.values()) >= 12
    by_id = {mandate["id"]: mandate for mandate in mandates}
    assert len(by_id) == len(mandates), "duplicate mandate ids"
    assert len({mandate["family"] for mandate in mandates}) == len(mandates), "duplicate families"
    demo_row = by_id.get(demo.id)
    assert demo_row is not None and demo_row["family"] == demo.family, "the demo mandate is missing"
    assert demo_row["advisor_id"] == demo.advisor_id, f"the demo mandate: {demo_row}"
    names = set(generate.load_family_names())
    others = {mandate["family"] for mandate in mandates} - {demo.family}
    assert others <= names, f"families not from the list: {sorted(others - names)}"

    documents = corpus.get("documents.jsonl", [])
    ids = [(document["advisor_id"], document["id"]) for document in documents]
    assert len(set(ids)) == len(ids) == len({document["id"] for document in documents})
    sheet_ids = [sheet["id"] for sheet in corpus.get("product_sheets.jsonl", [])]
    every_id = [document["id"] for document in documents] + sheet_ids
    assert len(set(every_id)) == len(every_id), "a product sheet id repeats a document id"
    per_mandate: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for document in documents:
        mandate = by_id.get(document["mandate_id"])
        assert mandate is not None, f"{document['id']}: no mandate {document['mandate_id']!r}"
        assert document["advisor_id"] == mandate["advisor_id"], (
            f"{document['id']}: advisor {document['advisor_id']}, mandate's {mandate['advisor_id']}"
        )
        assert document["lang"] == mandate["lang"], f"{document['id']}: not the mandate's language"
        per_mandate[mandate["id"]][document["type"]] += 1
    assert sorted(per_mandate) == sorted(by_id), "a mandate without documents"
    for mandate_id, counts in per_mandate.items():
        assert counts["kyc"] == 1 and counts["portfolio_report"] == 1, f"{mandate_id}: {counts}"
        assert 2 <= counts["call_note"] <= 3, f"{mandate_id}: {counts['call_note']} call notes"
        assert 3 <= counts["email"] <= 6, f"{mandate_id}: {counts['email']} e-mails"
        assert set(counts) == {"kyc", "portfolio_report", "call_note", "email"}, counts

    sheets = corpus.get("product_sheets.jsonl", [])
    assert len(sheets) == 6, f"{len(sheets)} product sheets"
    for sheet in sheets:
        assert sheet["type"] == "product_sheet", sheet["id"]
        assert not {"advisor_id", "mandate_id"} & set(sheet), f"{sheet['id']} is not global"

    everything = documents + sheets
    seen = {(document["type"], document["lang"]) for document in everything}
    assert seen == {(kind, lang) for kind in DOC_TYPES for lang in LANGS}, sorted(seen)
    # About 70 % German: by mandate (28 of 40), and so about the same over the documents.
    assert sum(mandate["lang"] == "german" for mandate in mandates) == 28
    assert by_id[demo.id]["lang"] == "german", "the demo mandate is German"
    german = sum(document["lang"] == "german" for document in everything) / len(everything)
    assert 0.65 <= german <= 0.75, f"German share {german:.3f}"


def test_trust_from_type(corpus: dict[str, list[dict]]) -> None:
    documents = corpus.get("documents.jsonl", []) + corpus.get("product_sheets.jsonl", [])
    assert len(documents) >= 300, f"{len(documents)} documents"
    assert set(generate.ORIGIN) == set(DOC_TYPES), generate.ORIGIN
    for document in documents:
        expected = "untrusted" if document["type"] == "email" else "internal"
        assert document["trust"] == expected, f"{document['id']}: trust {document['trust']!r}"
        assert document["origin"] == generate.ORIGIN[document["type"]], document["id"]
    assert generate.ORIGIN["email"] == "mailbox"

    # The type alone decides: an e-mail from the advisor's own staff address is as
    # untrusted as the client's.
    # On purpose: the one input trust_for may read is the type. A sender or a text
    # parameter would be the first step towards trust computed from an address.
    assert list(inspect.signature(generate.trust_for).parameters) == ["doc_type"]
    staff = {person.email for person in branding.ADVISORS}
    mails = [document for document in documents if document["type"] == "email"]
    from_staff = [mail for mail in mails if mail["sender"] in staff]
    from_clients = [mail for mail in mails if mail["sender"] not in staff]
    assert from_staff and from_clients, (len(from_staff), len(from_clients))
    assert {mail["trust"] for mail in from_staff + from_clients} == {"untrusted"}
    for document in documents:
        if document["type"] != "email" and "sender" in document:
            assert document["sender"] is None and document["recipients"] == [], document["id"]


def _strings(node: object) -> list[str]:
    if isinstance(node, str):
        return [node]
    if isinstance(node, dict):
        return [text for key, value in node.items() for text in _strings(key) + _strings(value)]
    if isinstance(node, list):
        return [text for value in node for text in _strings(value)]
    return []


def test_no_real_looking_data(out: Path) -> None:
    domains = branding.DOMAINS
    texts = []
    for name in DATA_FILES:
        path = out / name
        assert path.is_file(), f"{name} was not written"
        texts += _strings(_jsonl(path))
    assert len(texts) >= 3000, f"only {len(texts)} strings"

    problems = []
    for text in texts:
        for label, found in (
            ("IBAN", ibans(text)),
            ("phone", phones(text)),
            ("street", streets(text)),
            ("host", foreign_hosts(text)),
            ("secret", [hit.pattern for hit in find_secrets(text)]),
        ):
            problems += [f"{label}: {value!r}" for value in found]
        problems += [f"domain: {domain}" for domain in reserved_domains(text)]
    assert not problems, "\n".join(sorted(set(problems))[:30])

    # Non-vacuity: the host check saw addresses and URLs, all of them on the staff domain.
    joined = "\n".join(texts)
    # An address at the end of a sentence takes the full stop along.
    addresses = [host.rstrip(".") for host in runner_corpus.EMAIL.findall(joined)]
    assert len(addresses) >= 100 and set(addresses) == {domains.staff}, set(addresses)
    urls = runner_corpus.URL.findall(joined)
    assert len(urls) >= 6 and set(urls) == {domains.staff}, set(urls)


def test_no_attack_content(out: Path) -> None:
    # Attacks enter from redteam/attacks/ in M1, never from the generator: no instruction
    # aimed at an assistant, no markup that renders, and nobody of the cast but the advisors.
    others = [person for person in branding.CAST if person.role is not branding.Role.ADVISOR]
    names = re.compile(
        r"\b(?:" + "|".join(re.escape(person.name) for person in others) + r")\b", re.IGNORECASE
    )
    texts = [text for name in DATA_FILES for text in _strings(_jsonl(out / name))]
    assert len(texts) >= 3000, f"only {len(texts)} strings"
    problems = [f"marker {marker!r}" for text in texts for marker in attack_markers(text)]
    problems += [f"cast {match.group(0)!r}" for text in texts for match in names.finditer(text)]
    assert not problems, "\n".join(sorted(set(problems))[:30])


def test_figures_consistent(corpus: dict[str, list[dict]]) -> None:
    documents = corpus.get("documents.jsonl", [])
    reports = {doc["mandate_id"]: doc for doc in documents if doc["type"] == "portfolio_report"}
    notes = [doc for doc in documents if doc["type"] == "call_note"]
    assert len(reports) == 40 and len(notes) >= 80, (len(reports), len(notes))
    problems = []
    for mandate_id, report in sorted(reports.items()):
        problems += [
            f"{report['id']}: {p}" for p in report_problems(report["body"], report["lang"])
        ]
    for note in notes:
        report = reports[note["mandate_id"]]
        problems += [f"{note['id']}: {p}" for p in figure_problems(report["body"], note["body"])]
    assert not problems, "\n".join(problems[:30])


def test_call_notes_are_dated_after_their_report(corpus: dict[str, list[dict]]) -> None:
    # Fable's review: a note quotes the report's figures, so it cannot predate the report.
    documents = corpus.get("documents.jsonl", [])
    reports = {doc["mandate_id"]: doc for doc in documents if doc["type"] == "portfolio_report"}
    notes = [doc for doc in documents if doc["type"] == "call_note"]
    assert len(reports) == 40 and len(notes) >= 80, (len(reports), len(notes))
    early = [
        f"{note['id']} {note['date']} < {reports[note['mandate_id']]['date']}"
        for note in notes
        if note["date"] < reports[note["mandate_id"]]["date"]  # ISO dates sort as dates
    ]
    assert not early, f"{len(early)} of {len(notes)} call notes predate their report:\n" + (
        "\n".join(early[:10])
    )
    # Control: the check reads real dates, and the corpus' "today" bounds them.
    assert all(note["date"] <= generate.AS_OF.isoformat() for note in notes)
    assert all(report["date"] == generate.REPORT_DATE.isoformat() for report in reports.values())


# Any mention of the report, German or English, in a title or a body.
MENTIONS_REPORT = re.compile(r"vermögensbericht|\bbericht\b|\breports?\b", re.IGNORECASE)


def test_emails_that_mention_the_report_are_dated_after_it(
    corpus: dict[str, list[dict]],
) -> None:
    # Timur's answer on #18: an e-mail about the report cannot predate it. E-mails that
    # do not mention it may carry any date.
    documents = corpus.get("documents.jsonl", [])
    reports = {doc["mandate_id"]: doc for doc in documents if doc["type"] == "portfolio_report"}
    mails = [doc for doc in documents if doc["type"] == "email"]
    about = [mail for mail in mails if MENTIONS_REPORT.search(mail["title"] + "\n" + mail["body"])]
    assert len(reports) == 40 and len(about) >= 10, (len(reports), len(about))
    assert {mail["lang"] for mail in about} == set(LANGS), "both languages mention the report"
    early = [
        f"{mail['id']} {mail['date']} < {reports[mail['mandate_id']]['date']}"
        for mail in about
        if mail["date"] < reports[mail["mandate_id"]]["date"]  # ISO dates sort as dates
    ]
    assert not early, f"{len(early)} of {len(about)} e-mails about the report predate it:\n" + (
        "\n".join(early[:10])
    )
    assert all(mail["date"] <= generate.AS_OF.isoformat() for mail in mails)


def test_report_mention_pattern() -> None:
    # Control for the pattern above, on both templates that name the report and on text
    # that does not.
    for text in ("Frage zum Vermögensbericht", "thank you for the portfolio report", "den Bericht"):
        assert MENTIONS_REPORT.search(text), text
    for text in ("Unterlagen für die Steuererklärung", "Moving our meeting", "reportedly"):
        assert not MENTIONS_REPORT.search(text), text


TEMPLATES = ROOT / "data" / "synthetic" / "templates"


def test_templates_are_placeholders_only() -> None:
    names = {lang: sorted(p.name for p in (TEMPLATES / lang).glob("*")) for lang in ("de", "en")}
    assert names["de"] == names["en"], f"the languages differ: {names}"
    assert len(names["de"]) >= len(generate.KINDS), names["de"]
    assert all(name.endswith(".txt") for name in names["de"]), names["de"]

    used: set[str] = set()
    problems = []
    for path in sorted(TEMPLATES.glob("*/*.txt")):
        rel = path.relative_to(TEMPLATES).as_posix()
        text = path.read_text(encoding="utf-8")
        # Every value comes from the generator: no digit, address or URL in a template.
        for label, pattern in (("digit", r"\d"), ("address", "@"), ("URL", "://")):
            if re.search(pattern, text):
                problems.append(f"{rel}: a {label}")
        if re.search(r"[{}]", generate.PLACEHOLDER.sub("", text)):
            problems.append(f"{rel}: a brace outside a placeholder")
        used |= set(generate.PLACEHOLDER.findall(text))
    assert not problems, "\n".join(problems)
    assert used == generate.PLACEHOLDERS, f"declared, unused: {generate.PLACEHOLDERS - used}"
    loaded = generate.load_templates()
    assert sorted(loaded) == sorted(LANGS)
    for lang, kinds in loaded.items():
        assert sorted(kinds) == sorted(generate.KINDS) and all(kinds.values()), lang


def test_templates_are_filled_as_text(tmp_path: Path) -> None:
    # One pass, no lookups: a value is never read as a template, and str.format's
    # attribute and index syntax is a stray brace, not a lookup.
    assert generate.fill("{a} {b}", {"a": "{b}", "b": "x"}, "t") == "{b} x"
    for text, message in (
        ("{nope}", "unknown placeholder {nope}"),
        ("{family.__class__}", "stray '{'"),
        ("{family[0]}", "stray '{'"),
        ("a } b", "stray '}'"),
    ):
        with pytest.raises(generate.TemplateError, match=re.escape(message)):
            generate.fill(text, {"family": "x"}, "t")

    for lang in ("de", "en"):
        (tmp_path / lang).mkdir()
        for path in (TEMPLATES / lang).glob("*.txt"):
            (tmp_path / lang / path.name).write_bytes(path.read_bytes())
    generate.load_templates(tmp_path)  # control: the copy loads
    (tmp_path / "en" / "note_9.txt").write_text("Title\n\n{advisor_id}\n", encoding="utf-8")
    with pytest.raises(generate.TemplateError, match=r"en/note_9.txt: unknown placeholders"):
        generate.load_templates(tmp_path)


def test_family_names_list() -> None:
    names = generate.load_family_names()
    assert len(names) >= 39, f"{len(names)} names"
    assert len({name.lower() for name in names}) == len(names), "duplicate names"
    assert all(re.fullmatch(r"[A-Z][a-z]+", name) for name in names), "not one ASCII word each"
    reserved = {value.lower() for person in branding.CAST for value in (person.id, person.name)}
    reserved |= {branding.DEMO_MANDATE.family.lower(), branding.DEMO_MANDATE.id, branding.SLUG}
    reserved |= {stem.lower() for stem in branding.BANK.name_stems}
    assert not reserved & {name.lower() for name in names}, reserved & set(names)


GENERATOR = ROOT / "data" / "synthetic" / "generate.py"
# Reviewed: the standard library, and the branding single source.
GENERATOR_IMPORTS = {
    "__future__",
    "argparse",
    "dataclasses",
    "datetime",
    "hashlib",
    "json",
    "pathlib",
    "random",
    "re",
    "sys",
    "app.config",
}


def test_generator_imports_standard_library_only() -> None:
    tree = ast.parse(GENERATOR.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
            if node.module == "app.config":
                assert [alias.name for alias in node.names] == ["branding"]
    assert imported == GENERATOR_IMPORTS, f"imports: {sorted(imported)}"
    stdlib = {name for name in imported if name != "app.config"}
    assert all(name.split(".")[0] in sys.stdlib_module_names for name in stdlib)
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert not called & {"__import__", "eval", "exec", "compile", "open"}, called


def test_generator_refuses_symlinks(tmp_path: Path) -> None:
    # A link planted in the git-ignored output must not redirect the writes elsewhere.
    victim = tmp_path / "victim.txt"
    victim.write_text("keep\n", encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    linked_dir = tmp_path / "linked_out"
    linked_dir.symlink_to(elsewhere, target_is_directory=True)
    assert generate.main(["--out", str(linked_dir)]) == 2
    assert list(elsewhere.iterdir()) == [], "the generator wrote through a linked directory"

    out = tmp_path / "out"
    out.mkdir()
    (out / "documents.jsonl").symlink_to(victim)
    assert generate.main(["--out", str(out)]) == 2
    assert victim.read_text(encoding="utf-8") == "keep\n", "the generator wrote through a link"
    assert generate.main(["--out", str(tmp_path / "fresh")]) == 0  # control


def test_make_data_runs_the_generator() -> None:
    env = {key: value for key, value in os.environ.items() if not key.startswith("MAKE")}
    result = subprocess.run(
        ["make", "--no-print-directory", "-n", "data"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"make -n data: {result.stderr.strip()}"
    assert "uv run python -P -m data.synthetic.generate" in result.stdout, result.stdout
