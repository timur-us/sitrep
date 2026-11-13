"""Load the synthetic corpus as the owner: `python -P -m app.db.load` (`make load`; ADR 0010).

**Who writes.** The app role only reads (ADR 0008). The loader logs in as the migrator
and becomes the owner through app/db/migrate.py's become_owner(), which refuses any other
login. It writes one advisor per transaction through app/db/session.py's
owner_advisor_scope(), which checks the session again and sets the advisor for that
transaction, so FORCE ROW LEVEL SECURITY scopes every statement: WITH CHECK and the
composite foreign keys refuse another advisor's row. Then the product sheets, the one
global table, in a transaction of their own. Request code can reach neither this module
nor the helper (tests/test_loader_scope.py).

**What it loads.** `data/synthetic/out/` (`make data`), only when manifest.json is the
reviewed corpus (its sha256 is MANIFEST_SHA256, which tests/synthetic/ pins to the
generator's output) and every file's sha256 is the manifest's. read_corpus() then checks
every record before anything is written. A refusal names the file, the line or record id
and the rule, never the text:
- origin comes from the record's type through the generator's ORIGIN. A type without
  one, or a record whose own origin or trust disagrees, is refused (PR 1 review sec-1);
  trust is the database's generated column and is never written;
- ids are lower-case slugs, unique, and none starts with the attack-id prefix, in any
  case;
- the advisors are exactly the cast's, and each document's advisor is its mandate's;
- each mandate's contact is its record's client_email, on the staff domain, used by no
  other mandate and by no one of the cast: never an address from an e-mail (the input of
  M2's recipient allowlist);
- each sheet's url is exactly https://<staff domain>/<its language's path>/<its slug>,
  so no port, query, fragment or user part (the input of M2's link allowlist);
- a title and body longer than MAX_TEXT_CHARS together, holding a NUL, or not valid
  UTF-8 text, are refused before anything is cut (PR 4 review sec-4); an unexpected key
  is named only when it is a plain field name.

**Replacing.** Each advisor's corpus rows (chunks, documents, contacts, mandates) are
deleted in that advisor's transaction and written again, never truncated; the advisor
row is upserted. So a load is idempotent, and a load without attack documents leaves
none. Documents are cut into passages of at most CHUNK_TOKENS tokens of the model's own
tokenizer (app/embeddings/chunking.py), and every vector is stored with the model id and
revision. A product sheet is one passage and one vector: a longer one is refused, never
cut. Everything is chunked and embedded before the first write.

**Attack documents** (M1 PR 17) enter only through load_attack_documents() and the
attacks argument of prepare(): ids with the prefix, into a target mandate that exists in
the corpus, under that mandate's advisor, as mailbox whatever the case says. The command
line has no flag for them yet; PR 17 adds it.

The local loader only: the image carries neither data/ nor the model (M3).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psycopg

from app.config import branding
from app.config.corpus import MANIFEST_SHA256
from app.config.settings import Settings, ignored_url_notices
from app.db import queries
from app.db.connect import CheckedConnection, connect
from app.db.migrate import MigrationError, become_owner
from app.db.session import GuardError, owner_advisor_scope
from app.embeddings.base import Embedder
from app.embeddings.chunking import TokenCounter, chunk
from app.embeddings.model_files import ModelFileError
from app.embeddings.onnx import EmbedderError, OnnxEmbedder
from data.synthetic import generate

CORPUS = generate.DEFAULT_OUT
MANIFEST = "manifest.json"
DATA_FILES = ("advisors.jsonl", "mandates.jsonl", "documents.jsonl", "product_sheets.jsonl")
# MANIFEST_SHA256, the sha256 of the reviewed corpus's manifest.json, lives in
# app/config/corpus.py since M1 PR 16: the eval gate's stale check reads it too.
MAX_FILE_BYTES = 16 * 1024 * 1024
# Title and body together, in characters; the corpus's longest document has about 1200.
MAX_TEXT_CHARS = 32_000

# ADR 0010's yaml block, as the loader applies it (tests/test_loader_corpus.py pins them).
DOCUMENT_ORIGINS = frozenset({"kyc_register", "portfolio_system", "call_notes", "mailbox"})
SHEET_ORIGINS = frozenset({"product_catalogue"})
ATTACK_ID_PREFIX = "rt-"
ATTACK_ORIGIN = "mailbox"

ID = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
KEY_NAME = re.compile(r"[a-z_]{1,32}")
MAX_ID = 128
LANGS = frozenset(generate.LANGS)
CONTACT = re.compile(r"[a-z0-9]+(?:[.-][a-z0-9]+)*@" + re.escape(branding.DOMAINS.staff))
SHEET_URL = re.compile(
    r"https://" + re.escape(branding.DOMAINS.staff) + r"/([a-z]+)/([a-z0-9]+(?:-[a-z0-9]+)*)"
)
SHEET_ID_PREFIX = "product-"

ADVISOR_KEYS = frozenset({"advisor_id", "display_name", "email"})
MANDATE_KEYS = frozenset({"advisor_id", "id", "family", "lang", "client_email"})
DOCUMENT_KEYS = frozenset(
    {
        "advisor_id",
        "mandate_id",
        "id",
        "type",
        "lang",
        "title",
        "body",
        "date",
        "origin",
        "trust",
        "sender",
        "recipients",
    }
)
SHEET_KEYS = frozenset({"id", "type", "lang", "title", "body", "date", "origin", "trust", "url"})
# An attack record may name an origin; it is ignored, since every attack is mailbox.
ATTACK_KEYS = frozenset({"id", "mandate_id", "lang", "title", "body"})
ATTACK_OPTIONAL = frozenset({"origin"})


class LoadError(Exception):
    """A corpus, a record or a row the loader refuses. Names files, ids and rules only."""


@dataclass(frozen=True)
class Advisor:
    advisor_id: str
    display_name: str


@dataclass(frozen=True)
class Mandate:
    advisor_id: str
    id: str
    # Client data: kept out of a repr (review finding sec-6).
    family: str = field(repr=False)
    contact: str = field(repr=False)


@dataclass(frozen=True)
class Document:
    advisor_id: str
    id: str
    mandate_id: str
    lang: str
    origin: str
    title: str = field(repr=False)
    body: str = field(repr=False)


@dataclass(frozen=True)
class ProductSheet:
    id: str
    lang: str
    url: str
    origin: str
    title: str = field(repr=False)
    body: str = field(repr=False)


@dataclass(frozen=True)
class Corpus:
    advisors: tuple[Advisor, ...]
    mandates: tuple[Mandate, ...]
    documents: tuple[Document, ...]
    sheets: tuple[ProductSheet, ...]
    manifest_sha256: str


@dataclass(frozen=True)
class Chunk:
    advisor_id: str
    id: str
    document_id: str
    ordinal: int
    body: str = field(repr=False)
    embedding: tuple[float, ...] = field(repr=False)


@dataclass(frozen=True)
class AdvisorBatch:
    """One advisor's rows: what one owner_advisor_scope() transaction writes."""

    advisor: Advisor
    mandates: tuple[Mandate, ...]
    documents: tuple[Document, ...]
    chunks: tuple[Chunk, ...]


@dataclass(frozen=True)
class SheetRow:
    sheet: ProductSheet
    embedding: tuple[float, ...] = field(repr=False)


@dataclass(frozen=True)
class Prepared:
    """Every row of a load, chunked and embedded: write() only writes."""

    advisors: tuple[AdvisorBatch, ...]
    sheets: tuple[SheetRow, ...]
    model_id: str
    revision: str


# Reading and checking the corpus.


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read(directory: Path, name: str) -> bytes:
    path = directory / name
    try:
        info = os.stat(path)
    except FileNotFoundError:
        raise LoadError(f"{name}: missing; run `make data`") from None
    if not stat.S_ISREG(info.st_mode):
        raise LoadError(f"{name}: not a regular file")
    if info.st_size > MAX_FILE_BYTES:
        raise LoadError(f"{name}: larger than {MAX_FILE_BYTES} bytes")
    return path.read_bytes()


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    found: dict[str, Any] = {}
    for key, value in pairs:
        if key in found:
            raise LoadError("an object names a key twice")
        found[key] = value
    return found


def _json(text: str, where: str) -> object:
    try:
        return json.loads(text, object_pairs_hook=_unique_keys)
    except LoadError as error:
        raise LoadError(f"{where}: {error}") from None
    except ValueError:
        raise LoadError(f"{where}: not JSON") from None


def _jsonl(data: bytes, name: str) -> list[dict[str, Any]]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise LoadError(f"{name}: not UTF-8") from None
    if not text.endswith("\n"):
        raise LoadError(f"{name}: does not end with a line break")
    records = []
    for number, line in enumerate(text[:-1].split("\n"), start=1):
        record = _json(line, f"{name}:{number}")
        if not isinstance(record, dict):
            raise LoadError(f"{name}:{number}: not a JSON object")
        records.append(record)
    return records


def read_corpus(directory: Path = CORPUS, *, manifest_sha256: str = MANIFEST_SHA256) -> Corpus:
    """The corpus in directory, checked; LoadError otherwise.

    manifest_sha256 is for tests that plant a corpus of their own; main() never passes it.
    """
    manifest_bytes = _read(directory, MANIFEST)
    digest = _sha256(manifest_bytes)
    if digest != manifest_sha256:
        raise LoadError(
            f"{MANIFEST}: sha256 {digest[:12]}..., not the reviewed corpus; run `make data`"
        )
    try:
        manifest = _json(manifest_bytes.decode("utf-8"), MANIFEST)
    except UnicodeDecodeError:
        raise LoadError(f"{MANIFEST}: not UTF-8") from None
    files = manifest.get("files") if isinstance(manifest, dict) else None
    if not isinstance(files, dict) or sorted(files) != sorted(DATA_FILES):
        raise LoadError(f"{MANIFEST}: its files are not exactly {list(DATA_FILES)}")
    records = {}
    for name in DATA_FILES:
        data = _read(directory, name)
        if _sha256(data) != files[name]:
            raise LoadError(f"{name}: sha256 differs from {MANIFEST}")
        records[name] = _jsonl(data, name)
    return parse_corpus(records, digest)


def _keys(record: Mapping[str, Any], required: frozenset[str], where: str) -> None:
    missing = sorted(required - record.keys())
    if missing:
        raise LoadError(f"{where}: missing {missing}")
    extra = sorted(record.keys() - required)
    if extra:
        # A key is named only when it is a plain field name: an attack record's keys are
        # not hash-pinned, and a key can carry any text (review finding sec-6).
        if all(type(key) is str and KEY_NAME.fullmatch(key) for key in extra):
            raise LoadError(f"{where}: unexpected keys {extra}")
        raise LoadError(f"{where}: {len(extra)} unexpected key{'s' if len(extra) > 1 else ''}")


def _id(value: object, where: str, what: str = "id") -> str:
    """A corpus id: a lower-case slug that does not start with the attack-id prefix."""
    if type(value) is not str:
        raise LoadError(f"{where}: {what} is not a string")
    if value.casefold().startswith(ATTACK_ID_PREFIX):
        raise LoadError(f"{where}: {what} starts with the attack-id prefix")
    if len(value) > MAX_ID or not ID.fullmatch(value):
        raise LoadError(f"{where}: {what} is not a lower-case slug of at most {MAX_ID}")
    return value


def _lang(value: object, where: str) -> str:
    if type(value) is not str or value not in LANGS:
        raise LoadError(f"{where}: lang is not one of {sorted(LANGS)}")
    return value


def _text_field(value: object, where: str, name: str) -> str:
    """value as text: a non-empty str without NUL that UTF-8 can encode (a lone surrogate
    from a JSON escape cannot; review finding sec-6)."""
    if type(value) is not str or not value.strip():
        raise LoadError(f"{where}: {name} is not a non-empty string")
    if "\x00" in value:
        raise LoadError(f"{where}: {name} holds a NUL character")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise LoadError(f"{where}: {name} is not valid UTF-8 text") from None
    return value


def _texts(record: Mapping[str, Any], where: str) -> tuple[str, str]:
    title = _text_field(record["title"], where, "title")
    body = _text_field(record["body"], where, "body")
    if len(title) + len(body) > MAX_TEXT_CHARS:
        raise LoadError(f"{where}: title and body are longer than {MAX_TEXT_CHARS} characters")
    return title, body


def _origin(record: Mapping[str, Any], where: str, allowed: frozenset[str]) -> str:
    """The origin of the record's type (the generator's ORIGIN), if the record agrees."""
    doc_type = record["type"]
    if type(doc_type) is not str or doc_type not in generate.ORIGIN:
        raise LoadError(f"{where}: the type has no origin")
    origin = generate.ORIGIN[doc_type]
    if record["origin"] != origin:
        raise LoadError(f"{where}: its origin disagrees with its type {doc_type}")
    if record["trust"] != generate.trust_for(doc_type):
        raise LoadError(f"{where}: its trust disagrees with its type {doc_type}")
    if origin not in allowed:
        raise LoadError(f"{where}: a {doc_type} does not belong in this file")
    return origin


def _sheet_url(value: object, sheet_id: str, lang: str, where: str) -> str:
    match = SHEET_URL.fullmatch(value) if type(value) is str else None
    if (
        match is None
        or match.group(1) != generate.PRODUCT_PATH[lang]
        or SHEET_ID_PREFIX + match.group(2) != sheet_id
    ):
        raise LoadError(
            f"{where}: url is not https://<staff domain>/<path of its language>/<its slug>"
        )
    return value


def parse_corpus(records: Mapping[str, Sequence[Mapping[str, Any]]], digest: str) -> Corpus:
    """Every record checked; the corpus, or LoadError for the first that breaks a rule."""
    cast = [(person.id, person.name, person.email) for person in branding.ADVISORS]
    advisors = []
    for number, record in enumerate(records["advisors.jsonl"], start=1):
        _keys(record, ADVISOR_KEYS, f"advisors.jsonl:{number}")
        advisors.append((record["advisor_id"], record["display_name"], record["email"]))
    if advisors != cast:
        raise LoadError("advisors.jsonl: not exactly the cast's advisors, in order")

    advisor_ids = {advisor_id for advisor_id, _, _ in cast}
    cast_emails = {person.email for person in branding.CAST}
    mandates: dict[str, Mandate] = {}
    for number, record in enumerate(records["mandates.jsonl"], start=1):
        where = f"mandates.jsonl:{number}"
        _keys(record, MANDATE_KEYS, where)
        mandate_id = _id(record["id"], where)
        where = f"mandates.jsonl: mandate {mandate_id}"
        if record["advisor_id"] not in advisor_ids:
            raise LoadError(f"{where}: its advisor is not one of the cast's")
        if mandate_id in mandates:
            raise LoadError(f"{where}: a duplicate id")
        family, contact = _text_field(record["family"], where, "family"), record["client_email"]
        _lang(record["lang"], where)
        if type(contact) is not str or not CONTACT.fullmatch(contact):
            raise LoadError(f"{where}: client_email is not a lower-case staff-domain address")
        # One mandate's recipient, and no one of the cast: the input of M2's recipient
        # allowlist must not let a draft of one mandate go to another's client, or to
        # staff (review finding sec-3).
        if contact in cast_emails:
            raise LoadError(f"{where}: client_email is a cast member's address")
        if contact in {m.contact for m in mandates.values()}:
            raise LoadError(f"{where}: client_email repeats another mandate's")
        mandates[mandate_id] = Mandate(record["advisor_id"], mandate_id, family, contact)

    taken: set[str] = set()
    documents = []
    for number, record in enumerate(records["documents.jsonl"], start=1):
        where = f"documents.jsonl:{number}"
        _keys(record, DOCUMENT_KEYS, where)
        doc_id = _id(record["id"], where)
        where = f"documents.jsonl: document {doc_id}"
        if doc_id in taken:
            raise LoadError(f"{where}: a duplicate id")
        taken.add(doc_id)
        origin = _origin(record, where, DOCUMENT_ORIGINS)
        mandate = mandates.get(record["mandate_id"]) if type(record["mandate_id"]) is str else None
        if mandate is None:
            raise LoadError(f"{where}: its mandate is not in mandates.jsonl")
        if record["advisor_id"] != mandate.advisor_id:
            raise LoadError(f"{where}: its advisor is not its mandate's advisor")
        title, body = _texts(record, where)
        documents.append(
            Document(
                advisor_id=mandate.advisor_id,
                id=doc_id,
                mandate_id=mandate.id,
                lang=_lang(record["lang"], where),
                origin=origin,
                title=title,
                body=body,
            )
        )

    sheets = []
    for number, record in enumerate(records["product_sheets.jsonl"], start=1):
        where = f"product_sheets.jsonl:{number}"
        _keys(record, SHEET_KEYS, where)
        sheet_id = _id(record["id"], where)
        where = f"product_sheets.jsonl: sheet {sheet_id}"
        if sheet_id in taken:
            raise LoadError(f"{where}: a duplicate id")
        taken.add(sheet_id)
        origin = _origin(record, where, SHEET_ORIGINS)
        lang = _lang(record["lang"], where)
        title, body = _texts(record, where)
        url = _sheet_url(record["url"], sheet_id, lang, where)
        sheets.append(ProductSheet(sheet_id, lang, url, origin, title=title, body=body))

    return Corpus(
        advisors=tuple(Advisor(advisor_id, name) for advisor_id, name, _ in cast),
        mandates=tuple(mandates.values()),
        documents=tuple(documents),
        sheets=tuple(sheets),
        manifest_sha256=digest,
    )


def load_attack_documents(
    records: Sequence[Mapping[str, Any]], corpus: Corpus
) -> tuple[Document, ...]:
    """Attack documents for prepare(attacks=...), checked against corpus (ADR 0010).

    Each id must start with the attack-id prefix and be unique among the attacks and the
    corpus's ids; the target mandate must exist in the corpus, and the document goes to
    that mandate's advisor, as mailbox, whatever origin the record names.
    """
    mandates = {mandate.id: mandate for mandate in corpus.mandates}
    taken = {item.id.casefold() for item in (*corpus.documents, *corpus.sheets)}
    taken |= {mandate_id.casefold() for mandate_id in mandates}
    documents = []
    for number, record in enumerate(records, start=1):
        where = f"attack {number}"
        if not isinstance(record, Mapping):
            raise LoadError(f"{where}: not an object")
        _keys({key: None for key in record if key not in ATTACK_OPTIONAL}, ATTACK_KEYS, where)
        doc_id = record["id"]
        # Case-sensitive here, since an attack id must be a lower-case slug anyway; the
        # corpus side (_id) and the schema compare the prefix in any case.
        if type(doc_id) is not str or not doc_id.startswith(ATTACK_ID_PREFIX):
            raise LoadError(f"{where}: an attack id must start with the attack-id prefix")
        if len(doc_id) > MAX_ID or not ID.fullmatch(doc_id):
            raise LoadError(f"{where}: id is not a lower-case slug of at most {MAX_ID}")
        where = f"attack {doc_id}"
        if doc_id.casefold() in taken:
            raise LoadError(f"{where}: a duplicate id")
        taken.add(doc_id.casefold())
        target = record["mandate_id"]
        mandate = mandates.get(target) if type(target) is str else None
        if mandate is None:
            raise LoadError(f"{where}: its target mandate is not in the corpus")
        title, body = _texts(record, where)
        documents.append(
            Document(
                advisor_id=mandate.advisor_id,
                id=doc_id,
                mandate_id=mandate.id,
                lang=_lang(record["lang"], where),
                origin=ATTACK_ORIGIN,
                title=title,
                body=body,
            )
        )
    return tuple(documents)


# Chunking and embedding, before anything is written.


def _text(item: Document | ProductSheet) -> str:
    """What is cut and embedded: the title, a blank line, the body."""
    return f"{item.title}\n\n{item.body}"


def _check_attacks(attacks: Sequence[Document], corpus: Corpus) -> None:
    """Attack documents as load_attack_documents() makes them, and nothing else."""
    advisors = {mandate.id: mandate.advisor_id for mandate in corpus.mandates}
    for document in attacks:
        if (
            type(document) is not Document
            or not document.id.startswith(ATTACK_ID_PREFIX)
            or document.origin != ATTACK_ORIGIN
            or advisors.get(document.mandate_id) != document.advisor_id
        ):
            raise LoadError("attacks: only documents from load_attack_documents()")


def prepare(
    corpus: Corpus,
    embedder: Embedder,
    tokenizer: TokenCounter,
    *,
    attacks: Sequence[Document] = (),
) -> Prepared:
    """Every row of the load, cut into passages and embedded; LoadError for a sheet that
    is more than one passage, or an attack document load_attack_documents() did not make."""
    _check_attacks(attacks, corpus)
    documents = (*corpus.documents, *attacks)
    pieces = []  # (document, ordinal, passage)
    for document in documents:
        passages = chunk(_text(document), tokenizer)
        pieces += [(document, ordinal, passage) for ordinal, passage in enumerate(passages)]
    sheet_passages = []
    for sheet in corpus.sheets:
        passages = chunk(_text(sheet), tokenizer)
        if len(passages) != 1:
            raise LoadError(
                f"product sheet {sheet.id}: {len(passages)} passages; a sheet is one passage,"
                " refused, never cut"
            )
        sheet_passages.append(passages[0])

    vectors = embedder.embed_passages([passage for _, _, passage in pieces] + sheet_passages)
    if len(vectors) != len(pieces) + len(sheet_passages):
        raise LoadError("the embedder returned another number of vectors than passages")
    if any(len(vector) != embedder.dim for vector in vectors):
        raise LoadError(f"the embedder returned a vector without {embedder.dim} dimensions")
    chunks = [
        Chunk(
            advisor_id=document.advisor_id,
            id=f"{document.id}#{ordinal}",
            document_id=document.id,
            ordinal=ordinal,
            body=passage,
            embedding=tuple(vector),
        )
        for (document, ordinal, passage), vector in zip(pieces, vectors, strict=False)
    ]
    sheet_vectors = vectors[len(pieces) :]
    batches = []
    for advisor in corpus.advisors:
        own = advisor.advisor_id
        batches.append(
            AdvisorBatch(
                advisor=advisor,
                mandates=tuple(m for m in corpus.mandates if m.advisor_id == own),
                documents=tuple(d for d in documents if d.advisor_id == own),
                chunks=tuple(c for c in chunks if c.advisor_id == own),
            )
        )
    return Prepared(
        advisors=tuple(batches),
        sheets=tuple(
            SheetRow(sheet, tuple(vector))
            for sheet, vector in zip(corpus.sheets, sheet_vectors, strict=True)
        ),
        model_id=embedder.model_id,
        revision=embedder.revision,
    )


# Writing.


def _vector(values: Sequence[float]) -> str:
    """pgvector's text form."""
    return "[" + ",".join(repr(float(value)) for value in values) + "]"


def _replace_advisor(conn: CheckedConnection, batch: AdvisorBatch, prepared: Prepared) -> None:
    advisor_id = batch.advisor.advisor_id
    conn.execute(queries.LOAD_UPSERT_ADVISOR, (advisor_id, batch.advisor.display_name))
    conn.execute(queries.LOAD_DELETE_CHUNKS, (advisor_id,))
    conn.execute(queries.LOAD_DELETE_DOCUMENTS, (advisor_id,))
    conn.execute(queries.LOAD_DELETE_MANDATE_CONTACTS, (advisor_id,))
    conn.execute(queries.LOAD_DELETE_MANDATES, (advisor_id,))
    with conn.cursor() as cursor:
        cursor.executemany(
            queries.LOAD_INSERT_MANDATE,
            [(m.advisor_id, m.id, m.family) for m in batch.mandates],
        )
        cursor.executemany(
            queries.LOAD_INSERT_MANDATE_CONTACT,
            [(m.advisor_id, m.id, m.contact) for m in batch.mandates],
        )
        # Each row keeps its own advisor_id: a row of another advisor meets WITH CHECK.
        cursor.executemany(
            queries.LOAD_INSERT_DOCUMENT,
            [
                (d.advisor_id, d.id, d.mandate_id, d.title, d.body, d.lang, d.origin)
                for d in batch.documents
            ],
        )
        cursor.executemany(
            queries.LOAD_INSERT_CHUNK,
            [
                (
                    c.advisor_id,
                    c.id,
                    c.document_id,
                    c.ordinal,
                    c.body,
                    _vector(c.embedding),
                    prepared.model_id,
                    prepared.revision,
                )
                for c in batch.chunks
            ],
        )


def _replace_sheets(conn: CheckedConnection, prepared: Prepared) -> None:
    conn.execute(queries.LOAD_DELETE_PRODUCT_SHEETS)
    with conn.cursor() as cursor:
        cursor.executemany(
            queries.LOAD_INSERT_PRODUCT_SHEET,
            [
                (
                    row.sheet.id,
                    row.sheet.title,
                    row.sheet.body,
                    row.sheet.lang,
                    row.sheet.url,
                    row.sheet.origin,
                    _vector(row.embedding),
                    prepared.model_id,
                    prepared.revision,
                )
                for row in prepared.sheets
            ],
        )


def write(conn: CheckedConnection, prepared: Prepared) -> list[str]:
    """Replace each advisor's rows in a transaction of its own, then the product sheets;
    the lines to print. conn is the migrator acting as the owner (become_owner()).

    Every cast advisor comes first, each through owner_advisor_scope()'s checks, so the
    sheets' transaction never runs on a session those checks have not passed.
    """
    order = [batch.advisor.advisor_id for batch in prepared.advisors]
    if order != [person.id for person in branding.ADVISORS]:
        raise LoadError("a load writes exactly the cast's advisors, in order")
    lines = []
    for batch in prepared.advisors:
        with owner_advisor_scope(conn, batch.advisor.advisor_id):
            _replace_advisor(conn, batch, prepared)
        lines.append(
            f"{batch.advisor.advisor_id}: {len(batch.mandates)} mandates,"
            f" {len(batch.mandates)} contacts, {len(batch.documents)} documents,"
            f" {len(batch.chunks)} chunks"
        )
    with conn.transaction():
        _replace_sheets(conn, prepared)
    lines.append(f"product sheets: {len(prepared.sheets)}")
    return lines


def run(
    url: str,
    corpus: Corpus,
    embedder: Embedder,
    tokenizer: TokenCounter,
    *,
    attacks: Sequence[Document] = (),
) -> list[str]:
    """Log in with url, become the owner (refused for any login but the migrator), then
    prepare and write the load; the lines to print."""
    with connect(url, autocommit=True) as conn:
        become_owner(conn)
        return write(conn, prepare(corpus, embedder, tokenizer, attacks=attacks))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -P -m app.db.load",
        description="Load the synthetic corpus (make data) into the database as the owner.",
    )
    parser.add_argument("--corpus", type=Path, default=CORPUS, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    # As in app/db/migrate.py: an old, unprefixed URL is not read, and is named.
    for notice in ignored_url_notices(os.environ):
        print(f"load: {notice}", file=sys.stderr)
    try:
        corpus = read_corpus(args.corpus)
        url = Settings(_env_file=None).migration_database_url
        embedder = OnnxEmbedder()
        lines = run(url, corpus, embedder, embedder.tokenizer)
    except (ModelFileError, EmbedderError) as error:
        print(f"load: the embedding model: {error}; run `make model`", file=sys.stderr)
        return 1
    except (LoadError, MigrationError, GuardError, ValueError) as refused:
        # ValueError: a database URL the settings refuse, or a text the embedder refuses.
        print(f"load: refused: {refused}", file=sys.stderr)
        return 1
    except psycopg.Error as error:
        # The primary message only: a DETAIL line can carry a row's values.
        primary = error.diag.message_primary or ""
        print(f"load: {type(error).__name__} ({error.sqlstate}): {primary}", file=sys.stderr)
        return 1
    print(
        f"load: corpus {corpus.manifest_sha256[:12]}...: {len(corpus.mandates)} mandates,"
        f" {len(corpus.documents)} documents, {len(corpus.sheets)} product sheets"
    )
    for line in lines:
        print(f"load: {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
