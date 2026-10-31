"""Full-text search per language (ADR 0005; plan, PR 5b).

documents.lang is 'german' or 'english', never NULL; the generated tsv uses that
language's configuration; each search statement in app/db/queries.py is one language's
branch. German and English stemming both work, and the English branch does not return
a German document even for a word both configurations stem alike. A scan keeps the
one-argument text-search functions, which use the server's default configuration, out
of app/ and db/: every call names 'german' or 'english' as its first argument.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from dbtools import SUPERUSER_URL, Connection
from psycopg import sql
from psycopg_pool import ConnectionPool
from repo_files import ROOT
from seed_rows import MINE, SEED, insert, rows_for

from app.auth.identity import VerifiedAdvisor
from app.db import names, queries
from app.db.connect import connect
from app.db.session import advisor_transaction

DOCUMENTS = f"{names.SCHEMA}.documents"
# A German word the English configuration stems alike, and one German and one English
# word that only their own language's stemmer reduces to the query's stem.
TEXTS = {
    "german": "Die Anleihen im Portfolio wurden umgeschichtet.",
    "english": "The investments were rebalanced last quarter.",
}


def document_id(lang: str) -> str:
    return f"{SEED}-{MINE.id}-fts-{lang}"


@pytest.fixture(scope="module")
def language_documents(seeded: None) -> Iterator[None]:
    with connect(SUPERUSER_URL, autocommit=True) as conn:
        for lang, body in TEXTS.items():
            row = rows_for(MINE.id, "fts")["documents"] | {
                "id": document_id(lang),
                "body": body,
                "lang": lang,
            }
            insert(conn, DOCUMENTS, row)
        try:
            yield
        finally:
            conn.execute(
                sql.SQL("DELETE FROM {} WHERE id = ANY(%s)").format(
                    sql.Identifier(names.SCHEMA, "documents")
                ),
                ([document_id(lang) for lang in TEXTS],),
            )


def search(pool: ConnectionPool, query: str, text: str) -> list[str]:
    with advisor_transaction(pool, VerifiedAdvisor(MINE.id)) as conn:
        return [row[1] for row in conn.execute(query, (text,)).fetchall()]


@pytest.mark.usefixtures("database", "language_documents")
@pytest.mark.parametrize(
    ("query", "text", "lang"),
    [
        (queries.SEARCH_DOCUMENTS_GERMAN, "Anleihe", "german"),
        (queries.SEARCH_DOCUMENTS_ENGLISH, "investing", "english"),
    ],
    ids=["german", "english"],
)
def test_fts_language_stems(app_pool: ConnectionPool, query: str, text: str, lang: str) -> None:
    found = search(app_pool, query, text)
    assert document_id(lang) in found, f"{text!r} did not find the {lang} document: {found}"


@pytest.mark.usefixtures("database", "language_documents")
def test_fts_language_english_branch_skips_german_documents(app_pool: ConnectionPool) -> None:
    # Control: both configurations reduce "Portfolio" to the same lexeme.
    with connect(SUPERUSER_URL) as conn:
        lexemes = conn.execute(
            "SELECT to_tsvector('german', 'Portfolio')::text, to_tsvector('english', 'Portfolio')::text"
        ).fetchone()
    assert lexemes[0] == lexemes[1], lexemes
    assert document_id("german") in search(app_pool, queries.SEARCH_DOCUMENTS_GERMAN, "Portfolio")
    english = search(app_pool, queries.SEARCH_DOCUMENTS_ENGLISH, "Portfolio")
    assert document_id("german") not in english, f"the English branch found {english}"


@pytest.mark.usefixtures("database", "language_documents")
def test_fts_language_tsv_uses_the_documents_language(superuser: Connection) -> None:
    rows = dict(
        superuser.execute(
            sql.SQL("SELECT lang, tsv::text FROM {} WHERE id = ANY(%s)").format(
                sql.Identifier(names.SCHEMA, "documents")
            ),
            ([document_id(lang) for lang in TEXTS],),
        ).fetchall()
    )
    assert "'anleih'" in rows["german"], f"German tsv: {rows['german']}"
    assert "'invest'" in rows["english"], f"English tsv: {rows['english']}"


@pytest.mark.usefixtures("database")
@pytest.mark.parametrize(
    ("lang", "error"),
    [("french", psycopg.errors.CheckViolation), (None, psycopg.errors.NotNullViolation)],
    ids=["french", "NULL"],
)
def test_fts_language_rejects_other_languages(
    superuser: Connection, lang: str | None, error: type[Exception]
) -> None:
    row = rows_for(MINE.id, "fts-refused")["documents"] | {"lang": lang}
    with superuser.transaction(force_rollback=True):
        insert(
            superuser,
            f"{names.SCHEMA}.advisors",
            rows_for(MINE.id, "x")["advisors"],
            if_absent=True,
        )
        insert(
            superuser,
            f"{names.SCHEMA}.mandates",
            rows_for(MINE.id, "1")["mandates"],
            if_absent=True,
        )
        with pytest.raises(error), superuser.transaction():
            insert(superuser, DOCUMENTS, row)


# The scan.

TS_FUNCTIONS = re.compile(
    r"\b(to_tsvector|to_tsquery|plainto_tsquery|phraseto_tsquery|websearch_to_tsquery|ts_headline)"
    r"\s*\(",
    re.IGNORECASE,
)
CONFIGURATION = re.compile(r"'(?:german|english)'(?:\s*::\s*regconfig)?")


def arguments(text: str, start: int) -> list[str] | None:
    """The top-level arguments of the call whose '(' is just before start, or None if
    the call does not close in text."""
    depth, quoted, current, found = 0, False, "", []
    index = start
    while index < len(text):
        char = text[index]
        if quoted:
            current += char
            if char == "'":
                # '' inside a literal is an escaped quote.
                if text[index + 1 : index + 2] == "'":
                    current += "'"
                    index += 1
                else:
                    quoted = False
        elif char == "'":
            quoted = True
            current += char
        elif char == "(":
            depth += 1
            current += char
        elif char == ")":
            if depth == 0:
                found.append(current.strip())
                return [argument for argument in found if argument]
            depth -= 1
            current += char
        elif char == "," and depth == 0:
            found.append(current.strip())
            current = ""
        else:
            current += char
        index += 1
    return None


def unconfigured_calls(text: str) -> tuple[int, list[str]]:
    """How many text-search calls text holds, and those without a fixed configuration."""
    calls, wrong = 0, []
    for match in TS_FUNCTIONS.finditer(text):
        calls += 1
        args = arguments(text, match.end())
        line = text.count("\n", 0, match.start()) + 1
        if args is None or len(args) < 2 or not CONFIGURATION.fullmatch(args[0]):
            wrong.append(f"{line}: {match.group(1)}({', '.join(args or [])})")
    return calls, wrong


def scanned_files() -> list[Path]:
    return sorted((ROOT / "app").rglob("*.py")) + sorted((ROOT / "db").rglob("*.sql"))


def test_fts_language_no_one_argument_functions() -> None:
    files = scanned_files()
    rels = {path.relative_to(ROOT).as_posix() for path in files}
    assert {"app/db/queries.py", "db/migrations/0002_tables_rls.sql"} <= rels, sorted(rels)
    total, wrong = 0, []
    for path in files:
        calls, found = unconfigured_calls(path.read_text(encoding="utf-8"))
        total += calls
        wrong += [f"{path.relative_to(ROOT).as_posix()}:{item}" for item in found]
    assert not wrong, "\n".join(wrong)
    # Non-vacuity: the two search statements and the two branches of the generated tsv.
    assert total >= 4, f"only {total} text-search calls seen"


@pytest.mark.parametrize(
    "text",
    [
        "to_tsvector(body)",
        "SELECT TO_TSQUERY('fat & rat')",
        "plainto_tsquery(%s)",
        "phraseto_tsquery( 'a b' )",
        "websearch_to_tsquery(%s)",
        "to_tsvector(lang::regconfig, body)",
        "to_tsvector('french', body)",
        "ts_headline(body, q)",
        "to_tsvector(coalesce(title, '') || ' ' || body)",
        "to_tsvector(",
    ],
)
def test_fts_language_planted_calls_are_caught(text: str) -> None:
    calls, wrong = unconfigured_calls(text)
    assert calls == 1 and len(wrong) == 1, (calls, wrong)


@pytest.mark.parametrize(
    "text",
    [
        "to_tsvector('german', body)",
        "websearch_to_tsquery('english', %s)",
        "to_tsvector('english'::regconfig, title || ' ' || body)",
        "to_tsvector('german', coalesce(a, 'x,y'))",
        "ts_headline('german', body, q)",
    ],
)
def test_fts_language_configured_calls_pass(text: str) -> None:
    assert unconfigured_calls(text) == (1, [])
