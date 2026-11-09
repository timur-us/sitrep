"""The loader's checks before any write (app/db/load.py; ADR 0010), offline.

read_corpus() takes the reviewed corpus only: manifest.json must hash to
MANIFEST_SHA256 and every file to its manifest entry. Planted corpora, written by the
generator's own write(), reach the record checks through the manifest_sha256 seam; each
is refused with its rule, and no message carries a planted text. load_attack_documents()
applies the prefix rules, and prepare() cuts and embeds with a word tokenizer and the
fake embedder here; tests/db/test_loader.py runs the real model and the database.
"""

from __future__ import annotations

import copy
import hashlib
import re
from collections.abc import Callable
from pathlib import Path

import pytest
from adr_0010 import block

from app.config import branding
from app.config.embedding import CHUNK_TOKENS
from app.db import load
from app.embeddings.fake import FakeEmbedder
from data.synthetic import generate

Records = dict[str, list[dict[str, object]]]
CANARY = "Zebrafink-Kanarienvogel"  # planted into texts: never in a message


class Words:
    """A tokenizer that counts words, plus two special tokens (as tests/embeddings does)."""

    def count(self, text: str) -> int:
        return len(text.split()) + 2

    def offsets(self, text: str) -> list[tuple[int, int]]:
        return [match.span() for match in re.finditer(r"\S+", text)]


@pytest.fixture(scope="module")
def built() -> Records:
    return generate.build()


@pytest.fixture(scope="module")
def corpus(tmp_path_factory: pytest.TempPathFactory) -> load.Corpus:
    out = tmp_path_factory.mktemp("corpus")
    assert generate.main(["--out", str(out)]) == 0
    return load.read_corpus(out)


def plant(tmp_path: Path, records: Records) -> tuple[Path, str]:
    """records written as the generator writes them; the directory and its manifest hash."""
    digest = generate.write(tmp_path, records)
    return tmp_path, digest


def test_the_reviewed_corpus_reads(corpus: load.Corpus, built: Records) -> None:
    assert corpus.manifest_sha256 == load.MANIFEST_SHA256
    assert [a.advisor_id for a in corpus.advisors] == [p.id for p in branding.ADVISORS]
    assert (len(corpus.mandates), len(corpus.documents), len(corpus.sheets)) == (40, 344, 6)
    # Contacts from the mandate records, never from an e-mail's sender or recipients.
    assert {(m.advisor_id, m.id, m.contact) for m in corpus.mandates} == {
        (r["advisor_id"], r["id"], r["client_email"]) for r in built["mandates.jsonl"]
    }
    # Origin from the type; sheets with their exact URL.
    types = {r["id"]: r["type"] for r in built["documents.jsonl"]}
    assert all(d.origin == generate.ORIGIN[types[d.id]] for d in corpus.documents)
    assert {s.id: s.url for s in corpus.sheets} == {
        r["id"]: r["url"] for r in built["product_sheets.jsonl"]
    }
    # Every document's advisor is its mandate's.
    owners = {m.id: m.advisor_id for m in corpus.mandates}
    assert all(d.advisor_id == owners[d.mandate_id] for d in corpus.documents)


def test_another_corpus_is_refused(tmp_path: Path, built: Records) -> None:
    # The golden check: a corpus that is not the reviewed one, however consistent.
    records = copy.deepcopy(built)
    records["documents.jsonl"][0]["body"] += " Ein Satz mehr."
    directory, _ = plant(tmp_path, records)
    with pytest.raises(load.LoadError, match=r"manifest\.json: sha256 \w+\.\.\., not the review"):
        load.read_corpus(directory)


def test_a_changed_file_is_refused(tmp_path: Path) -> None:
    # The manifest is the reviewed one; a data file no longer matches it.
    assert generate.main(["--out", str(tmp_path)]) == 0
    documents = tmp_path / "documents.jsonl"
    documents.write_bytes(
        documents.read_bytes().replace(FAMILY.encode(), FAMILY.encode()[:-1] + b"x", 1)
    )
    with pytest.raises(load.LoadError, match=r"documents\.jsonl: sha256 differs from manifest"):
        load.read_corpus(tmp_path)
    with pytest.raises(load.LoadError, match="missing; run `make data`"):
        load.read_corpus(tmp_path / "nowhere")


def test_odd_files_are_refused(tmp_path: Path, built: Records) -> None:
    directory, digest = plant(tmp_path, copy.deepcopy(built))
    (directory / "mandates.jsonl").unlink()
    (directory / "mandates.jsonl").mkdir()
    with pytest.raises(load.LoadError, match=r"mandates\.jsonl: not a regular file"):
        load.read_corpus(directory, manifest_sha256=digest)
    (directory / "mandates.jsonl").rmdir()
    with pytest.raises(load.LoadError, match=r"mandates\.jsonl: missing"):
        load.read_corpus(directory, manifest_sha256=digest)


def test_a_manifest_with_other_files_is_refused(tmp_path: Path, built: Records) -> None:
    records = copy.deepcopy(built) | {"extra.jsonl": [{"x": 1}]}
    directory, digest = plant(tmp_path, records)
    with pytest.raises(load.LoadError, match="its files are not exactly"):
        load.read_corpus(directory, manifest_sha256=digest)


def test_a_repeated_key_is_refused(tmp_path: Path, built: Records) -> None:
    directory, _ = plant(tmp_path, copy.deepcopy(built))
    path = directory / "mandates.jsonl"
    path.write_bytes(path.read_bytes().replace(b'{"advisor_id":', b'{"id":"x","advisor_id":', 1))
    manifest = (directory / "manifest.json").read_text(encoding="utf-8")
    old = hashlib.sha256(generate.serialise(built["mandates.jsonl"])).hexdigest()
    new = hashlib.sha256(path.read_bytes()).hexdigest()
    (directory / "manifest.json").write_text(manifest.replace(old, new), encoding="utf-8")
    digest = hashlib.sha256((directory / "manifest.json").read_bytes()).hexdigest()
    with pytest.raises(load.LoadError, match=r"mandates\.jsonl:1: an object names a key twice"):
        load.read_corpus(directory, manifest_sha256=digest)


def _document(records: Records, doc_type: str = "email") -> dict[str, object]:
    return next(r for r in records["documents.jsonl"] if r["type"] == doc_type)


def _sheet(records: Records, lang: str = "german") -> dict[str, object]:
    return next(r for r in records["product_sheets.jsonl"] if r["lang"] == lang)


def _mandate(records: Records, advisor_id: str) -> dict[str, object]:
    return next(r for r in records["mandates.jsonl"] if r["advisor_id"] == advisor_id)


def _duplicate_document(r: Records) -> None:
    r["documents.jsonl"][1]["id"] = r["documents.jsonl"][0]["id"]


def _sheet_id_as_document(r: Records) -> None:
    r["documents.jsonl"][0]["id"] = r["product_sheets.jsonl"][0]["id"]


def _duplicate_mandate(r: Records) -> None:
    r["mandates.jsonl"][1]["id"] = r["mandates.jsonl"][0]["id"]


def _set(getter: Callable[[Records], dict[str, object]], key: str, value: object):
    def mutate(records: Records) -> None:
        getter(records)[key] = value

    return mutate


def _drop(getter: Callable[[Records], dict[str, object]], key: str):
    def mutate(records: Records) -> None:
        del getter(records)[key]

    return mutate


def _foreign_document(r: Records) -> None:
    # A document of one advisor under another advisor's mandate.
    document = next(d for d in r["documents.jsonl"] if d["advisor_id"] == MINE)
    document["advisor_id"] = branding.ADVISORS[1].id


def _sheet_in_documents(r: Records) -> None:
    sheet = dict(_sheet(r))
    del sheet["url"]
    r["documents.jsonl"].append(
        sheet | {"advisor_id": MINE, "mandate_id": DEMO, "sender": None, "recipients": []}
    )


def _email_in_sheets(r: Records) -> None:
    sheet = _sheet(r)
    sheet |= {"type": "email", "origin": "mailbox", "trust": "untrusted"}


def _a_fourth_advisor(r: Records) -> None:
    r["advisors.jsonl"].append(
        {"advisor_id": ATTACKER_ID.id, "display_name": ATTACKER_ID.name, "email": ATTACKER_ID.email}
    )


FIRST_DOC = _document
SHEET_DE = _sheet
MINE = branding.DEMO_MANDATE.advisor_id
DEMO = branding.DEMO_MANDATE.id
FAMILY = branding.DEMO_MANDATE.family
OTHER = next(p.id for p in branding.ADVISORS if p.id != MINE)
ATTACKER_ID = next(p for p in branding.CAST if p.role is branding.Role.ATTACKER)
STAFF = branding.DOMAINS.staff
ATTACKER = branding.DOMAINS.attacker

PLANTED: list[tuple[str, Callable[[Records], None], str]] = [
    ("duplicate document id", _duplicate_document, "a duplicate id"),
    ("a document has a sheet's id", _sheet_id_as_document, "a duplicate id"),
    ("duplicate mandate id", _duplicate_mandate, "a duplicate id"),
    ("rt- document", _set(FIRST_DOC, "id", "rt-mail-1"), "starts with the attack-id prefix"),
    ("RT- document", _set(FIRST_DOC, "id", "RT-mail-1"), "starts with the attack-id prefix"),
    ("Rt- sheet", _set(SHEET_DE, "id", "Rt-sheet"), "starts with the attack-id prefix"),
    (
        "rt- mandate",
        _set(lambda r: r["mandates.jsonl"][0], "id", "rt-family"),
        "starts with the attack-id prefix",
    ),
    ("id not a slug", _set(FIRST_DOC, "id", f"{FAMILY} Mail"), "not a lower-case slug"),
    ("id too long", _set(FIRST_DOC, "id", "a" * 129), "not a lower-case slug"),
    ("unknown type", _set(FIRST_DOC, "type", "memo"), "the type has no origin"),
    # PR 1 review sec-1: an e-mail that claims an internal origin or trust.
    ("e-mail as call notes", _set(FIRST_DOC, "origin", "call_notes"), "origin disagrees"),
    ("e-mail as internal", _set(FIRST_DOC, "trust", "internal"), "trust disagrees"),
    ("a sheet in documents", _sheet_in_documents, "does not belong in this file"),
    ("an e-mail in sheets", _email_in_sheets, "does not belong in this file"),
    ("another advisor's document", _foreign_document, "not its mandate's advisor"),
    (
        "unknown mandate",
        _set(FIRST_DOC, "mandate_id", "nobody"),
        "its mandate is not in mandates.jsonl",
    ),
    ("a fourth advisor", _a_fourth_advisor, "not exactly the cast's advisors"),
    (
        "a renamed advisor",
        _set(lambda r: r["advisors.jsonl"][0], "display_name", "Alicia"),
        "not exactly the cast's advisors",
    ),
    (
        "a mandate of nobody",
        _set(lambda r: r["mandates.jsonl"][0], "advisor_id", ATTACKER_ID.id),
        "its advisor is not one of the cast's",
    ),
    ("an extra key", _set(FIRST_DOC, "priority", "high"), "unexpected keys ['priority']"),
    ("a missing key", _drop(FIRST_DOC, "origin"), "missing ['origin']"),
    ("a title that is not text", _set(FIRST_DOC, "title", 7), "title is not a non-empty string"),
    ("an empty body", _set(FIRST_DOC, "body", "  \n"), "body is not a non-empty string"),
    ("a NUL", _set(FIRST_DOC, "body", f"{CANARY}\x00"), "body holds a NUL character"),
    (
        "a text over the cap",
        _set(FIRST_DOC, "body", CANARY * (load.MAX_TEXT_CHARS // len(CANARY) + 1)),
        f"longer than {load.MAX_TEXT_CHARS} characters",
    ),
    ("another language", _set(FIRST_DOC, "lang", "french"), "lang is not one of"),
    # Contacts (the input of M2's recipient allowlist).
    *[
        (
            f"contact {label}",
            _set(lambda r: _mandate(r, MINE), "client_email", value),
            "client_email is not a lower-case staff-domain address",
        )
        for label, value in [
            ("on the attacker's domain", f"{DEMO}.family@{ATTACKER}"),
            ("in upper case", f"{FAMILY}.Family@{STAFF}"),
            ("with a line break", f"{DEMO}@{STAFF}\r\nBcc: x@{ATTACKER}"),
            ("on a sub-domain", f"{DEMO}@mail.{STAFF}"),
            ("with a display name", f"{FAMILY} <{DEMO}@{STAFF}>"),
        ]
    ],
    # Product-sheet URLs (the input of M2's link allowlist).
    *[
        (f"url {label}", _set(SHEET_DE, "url", value), "url is not https://")
        for label, value in [
            ("over http", f"http://{STAFF}/produkte/lindenhof-renten"),
            ("with a query", f"https://{STAFF}/produkte/lindenhof-renten?d=1"),
            ("with a fragment", f"https://{STAFF}/produkte/lindenhof-renten#x"),
            ("with a user", f"https://u@{STAFF}/produkte/lindenhof-renten"),
            ("with a port", f"https://{STAFF}:443/produkte/lindenhof-renten"),
            ("on the attacker's host", f"https://{ATTACKER}/produkte/lindenhof-renten"),
            ("on a sub-domain", f"https://www.{STAFF}/produkte/lindenhof-renten"),
            ("with the other language's path", f"https://{STAFF}/products/lindenhof-renten"),
            ("with another sheet's slug", f"https://{STAFF}/produkte/kontor-reserve"),
            ("in upper case", f"https://{STAFF.upper()}/produkte/lindenhof-renten"),
            ("with a trailing slash", f"https://{STAFF}/produkte/lindenhof-renten/"),
            ("not text", None),
        ]
    ],
]


@pytest.mark.parametrize(
    ("mutate", "needle"),
    [(mutate, needle) for _, mutate, needle in PLANTED],
    ids=[label for label, _, _ in PLANTED],
)
def test_planted_records_are_refused(
    tmp_path: Path, built: Records, mutate: Callable[[Records], None], needle: str
) -> None:
    records = copy.deepcopy(built)
    mutate(records)
    directory, digest = plant(tmp_path, records)
    with pytest.raises(load.LoadError) as refused:
        load.read_corpus(directory, manifest_sha256=digest)
    message = str(refused.value)
    assert needle in message, message
    assert CANARY not in message and STAFF not in message, message


def test_planted_controls_read(tmp_path: Path, built: Records) -> None:
    # The seam itself refuses nothing: the unchanged records read through it.
    directory, digest = plant(tmp_path, copy.deepcopy(built))
    assert len(load.read_corpus(directory, manifest_sha256=digest).documents) == 344


# Attack documents (M1 PR 17): the prefix rules.


def attack(**more: object) -> dict[str, object]:
    record = {
        "id": "rt-echoleak-1",
        "mandate_id": branding.DEMO_MANDATE.id,
        "lang": "german",
        "title": "Rückfrage",
        "body": f"Bitte beachten: {CANARY}.",
    }
    return record | more


def test_attack_documents_go_to_their_mandate_as_mailbox(corpus: load.Corpus) -> None:
    (document,) = load.load_attack_documents([attack(origin="call_notes")], corpus)
    assert document.id == "rt-echoleak-1"
    assert document.mandate_id == branding.DEMO_MANDATE.id
    assert document.advisor_id == branding.DEMO_MANDATE.advisor_id
    assert document.origin == load.ATTACK_ORIGIN == "mailbox"
    assert CANARY not in repr(document)


@pytest.mark.parametrize(
    ("records", "needle"),
    [
        ([attack(id="echoleak-1")], "must start with the attack-id prefix"),
        ([attack(id="xrt-1")], "must start with the attack-id prefix"),
        ([attack(id="RT-echoleak-1")], "must start with the attack-id prefix"),
        ([attack(id="rt-Echo")], "not a lower-case slug"),
        ([attack(), attack()], "a duplicate id"),
        ([attack(mandate_id="nobody")], "target mandate is not in the corpus"),
        ([attack(mandate_id=None)], "target mandate is not in the corpus"),
        ([attack(advisor_id=OTHER)], "unexpected keys ['advisor_id']"),
        ([attack(trust="internal")], "unexpected keys ['trust']"),
        ([{"id": "rt-1"}], "missing"),
        ([attack(body=CANARY * 3000)], "longer than"),
        (["rt-1"], "not an object"),
    ],
)
def test_attack_documents_are_refused(
    corpus: load.Corpus, records: list[object], needle: str
) -> None:
    with pytest.raises(load.LoadError) as refused:
        load.load_attack_documents(records, corpus)  # type: ignore[arg-type]
    assert needle in str(refused.value) and CANARY not in str(refused.value), refused.value


def test_an_attack_id_may_not_repeat_a_corpus_id(corpus: load.Corpus) -> None:
    # The prefix rules keep the two apart; the duplicate check holds without them too.
    planted = load.Corpus(
        advisors=corpus.advisors,
        mandates=corpus.mandates,
        documents=(*corpus.documents[:1], _replace_id(corpus.documents[1], "rt-echoleak-1")),
        sheets=(),
        manifest_sha256="",
    )
    with pytest.raises(load.LoadError, match="a duplicate id"):
        load.load_attack_documents([attack()], planted)


def _replace_id(document: load.Document, new_id: str) -> load.Document:
    return load.Document(
        advisor_id=document.advisor_id,
        id=new_id,
        mandate_id=document.mandate_id,
        lang=document.lang,
        origin=document.origin,
        title=document.title,
        body=document.body,
    )


# Cutting and embedding, before anything is written (word tokenizer, fake embedder).


def small(corpus: load.Corpus, *, body: str | None = None) -> load.Corpus:
    """One document per advisor and one sheet; body replaces the first document's."""
    documents = []
    for advisor in corpus.advisors:
        first = next(d for d in corpus.documents if d.advisor_id == advisor.advisor_id)
        documents.append(first)
    if body is not None:
        first = documents[0]
        documents[0] = load.Document(
            advisor_id=first.advisor_id,
            id=first.id,
            mandate_id=first.mandate_id,
            lang=first.lang,
            origin=first.origin,
            title=first.title,
            body=body,
        )
    return load.Corpus(
        advisors=corpus.advisors,
        mandates=corpus.mandates,
        documents=tuple(documents),
        sheets=corpus.sheets[:1],
        manifest_sha256=corpus.manifest_sha256,
    )


def test_prepare_cuts_long_documents_and_keeps_the_rest_whole(corpus: load.Corpus) -> None:
    long = " ".join(f"Wort{n}." for n in range(2 * CHUNK_TOKENS))
    prepared = load.prepare(small(corpus, body=long), FakeEmbedder(), Words())
    first = prepared.advisors[0]
    pieces = [c for c in first.chunks if c.document_id == first.documents[0].id]
    assert len(pieces) >= 3, len(pieces)
    assert [c.ordinal for c in pieces] == list(range(len(pieces)))
    assert [c.id for c in pieces] == [f"{first.documents[0].id}#{n}" for n in range(len(pieces))]
    assert all(Words().count("passage: " + c.body) <= CHUNK_TOKENS for c in pieces)
    assert " ".join(c.body for c in pieces).endswith(long.split()[-1])
    # Each other document is one passage; every vector has the embedder's size.
    for batch in prepared.advisors[1:]:
        assert len(batch.chunks) == len(batch.documents) == 1
    assert all(len(c.embedding) == FakeEmbedder.dim for b in prepared.advisors for c in b.chunks)
    assert (prepared.model_id, prepared.revision) == (FakeEmbedder.model_id, "fake")
    assert [b.advisor.advisor_id for b in prepared.advisors] == [p.id for p in branding.ADVISORS]
    assert len(prepared.sheets) == 1


def test_prepare_refuses_a_sheet_over_one_passage(corpus: load.Corpus) -> None:
    sheet = corpus.sheets[0]
    long = load.ProductSheet(
        id=sheet.id,
        lang=sheet.lang,
        url=sheet.url,
        origin=sheet.origin,
        title=sheet.title,
        body=" ".join([CANARY] * CHUNK_TOKENS),
    )
    planted = small(corpus)
    planted = load.Corpus(
        planted.advisors, planted.mandates, planted.documents, (long,), planted.manifest_sha256
    )
    with pytest.raises(load.LoadError, match="a sheet is one passage, refused, never cut") as error:
        load.prepare(planted, FakeEmbedder(), Words())
    assert CANARY not in str(error.value)


def test_prepare_takes_attacks_from_load_attack_documents_only(corpus: load.Corpus) -> None:
    base = small(corpus)
    attacks = load.load_attack_documents([attack()], corpus)
    prepared = load.prepare(base, FakeEmbedder(), Words(), attacks=attacks)
    mine = prepared.advisors[0]
    assert attacks[0] in mine.documents
    assert any(c.document_id == attacks[0].id for c in mine.chunks)
    # Hand-made attack documents: another origin, no prefix, the wrong advisor.
    for forged in (
        load.Document(**{**_fields(attacks[0]), "origin": "call_notes"}),
        load.Document(**{**_fields(attacks[0]), "id": "echoleak-1"}),
        load.Document(**{**_fields(attacks[0]), "advisor_id": branding.ADVISORS[1].id}),
    ):
        with pytest.raises(load.LoadError, match="only documents from load_attack_documents"):
            load.prepare(base, FakeEmbedder(), Words(), attacks=(forged,))


def _fields(document: load.Document) -> dict[str, object]:
    return {
        "advisor_id": document.advisor_id,
        "id": document.id,
        "mandate_id": document.mandate_id,
        "lang": document.lang,
        "origin": document.origin,
        "title": document.title,
        "body": document.body,
    }


def test_no_text_in_a_repr(corpus: load.Corpus) -> None:
    prepared = load.prepare(small(corpus), FakeEmbedder(), Words())
    for item in (*corpus.documents[:3], *corpus.sheets[:1]):
        assert item.body not in repr(item) and item.title not in repr(item), repr(item)
    piece = prepared.advisors[0].chunks[0]
    assert piece.body not in repr(piece) and str(piece.embedding[0]) not in repr(piece)


# The loader's vocabulary is ADR 0010's (the schema's CHECKs: tests/db/test_loader.py).


def test_the_vocabulary_is_adr_0010s() -> None:
    facts = block()
    assert load.DOCUMENT_ORIGINS == set(facts["tables"]["documents"])
    assert load.SHEET_ORIGINS == set(facts["tables"]["product_sheets"])
    assert load.ATTACK_ID_PREFIX == facts["attack_id_prefix"]
    assert load.ATTACK_ORIGIN == facts["attack_origin"]
    # The generator's types land in the loader's tables, every one.
    assert set(generate.ORIGIN.values()) == load.DOCUMENT_ORIGINS | load.SHEET_ORIGINS
