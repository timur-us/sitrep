"""The loader (ADR 0010; the M1 plan's PR 7): scaffolding for the tests that come first.

The names the tests import, and nothing that runs: every function raises
NotImplementedError. The next commit builds them.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from data.synthetic import generate

CORPUS = generate.DEFAULT_OUT
MANIFEST_SHA256 = "7053bbbc4bd2b52f4c1ddacd4647726dc9a556422661d8bb1bbadd269b29bf5d"
MAX_TEXT_CHARS = 32_000
DOCUMENT_ORIGINS = frozenset({"kyc_register", "portfolio_system", "call_notes", "mailbox"})
SHEET_ORIGINS = frozenset({"product_catalogue"})
ATTACK_ID_PREFIX = "rt-"
ATTACK_ORIGIN = "mailbox"


class LoadError(Exception):
    """A corpus, a record or a row the loader refuses."""


@dataclass(frozen=True)
class Advisor:
    advisor_id: str
    display_name: str


@dataclass(frozen=True)
class Mandate:
    advisor_id: str
    id: str
    family: str
    contact: str


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
    advisors: tuple[AdvisorBatch, ...]
    sheets: tuple[SheetRow, ...]
    model_id: str
    revision: str


def read_corpus(directory: Path = CORPUS, *, manifest_sha256: str = MANIFEST_SHA256) -> Corpus:
    raise NotImplementedError("M1 PR 7: built in the next commit")


def load_attack_documents(
    records: Sequence[Mapping[str, Any]], corpus: Corpus
) -> tuple[Document, ...]:
    raise NotImplementedError("M1 PR 7: built in the next commit")


def prepare(
    corpus: Corpus, embedder: Any, tokenizer: Any, *, attacks: Sequence[Document] = ()
) -> Prepared:
    raise NotImplementedError("M1 PR 7: built in the next commit")


def write(conn: Any, prepared: Prepared) -> list[str]:
    raise NotImplementedError("M1 PR 7: built in the next commit")


def run(
    url: str, corpus: Corpus, embedder: Any, tokenizer: Any, *, attacks: Sequence[Document] = ()
) -> list[str]:
    raise NotImplementedError("M1 PR 7: built in the next commit")


def main(argv: list[str] | None = None) -> int:
    raise NotImplementedError("M1 PR 7: built in the next commit")
