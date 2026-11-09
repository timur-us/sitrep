"""The loader against the database (app/db/load.py; ADR 0010; the M1 plan's PR 7).

Needs `make up`, `make migrate` and `make model`, and fails, never skips, without them.
The module loads the real corpus with the real model once and leaves it loaded, as
`make load` does; the other database tests seed beside it (tests/db/seed_rows.py).

- owner_advisor_scope() sets the advisor for one transaction for the migrator acting as
  the owner, and refuses the app role, the migrator before SET ROLE, the superuser (as
  itself and as the owner), an advisor outside the cast, a second call in one
  transaction and a connection already in one; planted rows show the SUPERUSER,
  BYPASSRLS, advisor and search_path rules.
- The loader refuses the app login and the superuser before it writes anything, through
  run() and through the command line.
- A planted document of another advisor is refused by WITH CHECK, or by the composite
  foreign key, and nothing changes.
- The load: the cast's advisors, 40 mandates (14, 13, 13), 40 contacts from the mandate
  records, 344 documents in chunks of at most CHUNK_TOKENS tokens, six sheets with their
  URLs, every vector with the model id and revision; a second load, through the command
  line, gives the same rows, byte for byte.
- Trust: generated, so neither an INSERT nor an UPDATE may name it, not even as the
  superuser; each table's CHECK allows exactly ADR 0010's origins for it, and each reads
  the block's trust; with the CHECK gone, runtime origins and unknown ones read
  untrusted (fail closed).
- Attack documents load as mailbox under the target mandate's advisor, and the next
  load without them leaves none; the schema refuses an attack id anywhere but a
  mailbox document.
- Every advisor reads the six sheets; with no advisor, none.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import psycopg
import pytest
from adr_0010 import block, trust_of
from dbtools import SUPERUSER_URL, Connection
from psycopg import sql
from psycopg.pq import TransactionStatus
from psycopg_pool import ConnectionPool
from seed_rows import vector

from app.auth.identity import issue
from app.config import branding
from app.config.embedding import CHUNK_TOKENS, MODEL_ID, REVISION
from app.config.settings import env_name, loopback_database_url
from app.db import load, names
from app.db.connect import connect
from app.db.migrate import MigrationError, become_owner
from app.db.session import (
    GuardError,
    advisor_transaction,
    owner_advisor_scope,
    owner_session_problems,
)
from app.embeddings.base import passage_input
from app.embeddings.model_files import ModelFileError
from app.embeddings.onnx import OnnxEmbedder
from data.synthetic import generate

pytestmark = pytest.mark.usefixtures("database")

MIGRATOR_URL = loopback_database_url(names.MIGRATOR_ROLE)
APP_URL = loopback_database_url(names.APP_ROLE)
SETTING = "SELECT current_setting('app.advisor_id', true)"
MINE = branding.DEMO_MANDATE.advisor_id
OTHER = next(person.id for person in branding.ADVISORS if person.id != MINE)
NAME = next(person.name for person in branding.ADVISORS if person.id == MINE)
ATTACKER = next(person for person in branding.CAST if person.role is branding.Role.ATTACKER)
IDLE = TransactionStatus.IDLE

# Every loaded row of every table the loader writes, in a stable order.
ROWS = {
    "advisors": "SELECT advisor_id, display_name FROM {} ORDER BY 1",
    "mandates": "SELECT advisor_id, id, family FROM {} ORDER BY 1, 2",
    "mandate_contacts": "SELECT advisor_id, mandate_id, email FROM {} ORDER BY 1, 2, 3",
    "documents": (
        "SELECT advisor_id, id, mandate_id, title, body, lang, origin, trust FROM {} ORDER BY 1, 2"
    ),
    "chunks": (
        "SELECT advisor_id, id, document_id, ordinal, body, embedding::text, embedding_model,"
        " embedding_revision FROM {} ORDER BY 1, 2"
    ),
    "product_sheets": (
        "SELECT id, title, body, lang, url, origin, trust, embedding::text, embedding_model,"
        " embedding_revision FROM {} ORDER BY 1"
    ),
}


def table(name: str) -> sql.Identifier:
    return sql.Identifier(names.SCHEMA, name)


def snapshot(conn: Connection) -> dict[str, tuple[int, str]]:
    """Per table: the row count and a sha256 over every row, as the superuser sees them."""
    found = {}
    for name, query in ROWS.items():
        rows = conn.execute(sql.SQL(query).format(table(name))).fetchall()
        found[name] = (len(rows), hashlib.sha256(repr(rows).encode()).hexdigest())
    return found


@contextmanager
def owner_session() -> Iterator[Connection]:
    """The migrator acting as the owner, as the loader's run() opens it."""
    with connect(MIGRATOR_URL, autocommit=True) as conn:
        become_owner(conn)
        yield conn


@pytest.fixture(scope="module")
def embedder() -> OnnxEmbedder:
    try:
        return OnnxEmbedder()
    except ModelFileError as error:
        pytest.fail(f"the embedding model is not in place ({error}); run `make model`")


@pytest.fixture(scope="module")
def corpus_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("corpus")
    assert generate.main(["--out", str(out)]) == 0
    return out


@pytest.fixture(scope="module")
def corpus(corpus_dir: Path) -> load.Corpus:
    return load.read_corpus(corpus_dir)


@pytest.fixture(scope="module")
def prepared(corpus: load.Corpus, embedder: OnnxEmbedder) -> load.Prepared:
    return load.prepare(corpus, embedder, embedder.tokenizer)


@pytest.fixture(scope="module")
def loaded(database: None, prepared: load.Prepared) -> dict[str, tuple[int, str]]:
    """The corpus written once for the module; the snapshot right after."""
    with owner_session() as conn:
        load.write(conn, prepared)
    with connect(SUPERUSER_URL, autocommit=True) as conn:
        return snapshot(conn)


@pytest.fixture
def owner() -> Iterator[Connection]:
    with owner_session() as conn:
        yield conn


@pytest.fixture
def su(superuser: Connection) -> Connection:
    """The superuser with the app schema first on its search_path."""
    superuser.execute(sql.SQL("SET search_path = {}, public").format(sql.Identifier(names.SCHEMA)))
    return superuser


def batch_of(prepared: load.Prepared, advisor_id: str) -> load.AdvisorBatch:
    return next(b for b in prepared.advisors if b.advisor.advisor_id == advisor_id)


def with_batch(prepared: load.Prepared, batch: load.AdvisorBatch) -> load.Prepared:
    advisors = tuple(
        batch if b.advisor.advisor_id == batch.advisor.advisor_id else b for b in prepared.advisors
    )
    return dataclasses.replace(prepared, advisors=advisors)


# owner_advisor_scope() at runtime.


def test_owner_scope_sets_the_advisor_for_one_transaction(
    loaded: dict, owner: Connection, su: Connection
) -> None:
    with owner_advisor_scope(owner, MINE) as conn:
        assert conn is owner
        assert conn.execute(SETTING).fetchone() == (MINE,)
        seen = conn.execute("SELECT count(*) FROM mandates").fetchone()[0]
        foreign = conn.execute("SELECT count(*) FROM mandates WHERE advisor_id <> %s", (MINE,))
        assert foreign.fetchone() == (0,)
    expected = su.execute("SELECT count(*) FROM mandates WHERE advisor_id = %s", (MINE,))
    assert seen == expected.fetchone()[0] >= 14, seen
    assert owner.execute(SETTING).fetchone() == ("",), "the advisor outlived the transaction"
    # Control: without the scope the owner, bound by FORCE, sees nothing.
    assert owner.execute("SELECT count(*) FROM mandates").fetchone() == (0,)


SESSIONS = {
    "app": (APP_URL, False),
    "migrator-without-set-role": (MIGRATOR_URL, False),
    "superuser": (SUPERUSER_URL, False),
    "superuser-as-owner": (SUPERUSER_URL, True),
}


@pytest.mark.parametrize("who", sorted(SESSIONS))
def test_owner_scope_refuses_other_sessions(who: str) -> None:
    url, set_role = SESSIONS[who]
    with connect(url, autocommit=True) as conn:
        if set_role:
            conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(names.OWNER_ROLE)))
        expected = f"expected {names.MIGRATOR_ROLE} acting as"
        with pytest.raises(GuardError, match=expected), owner_advisor_scope(conn, MINE):
            pytest.fail(f"{who} entered the owner's scope")
        assert conn.info.transaction_status == IDLE
        assert conn.execute(SETTING).fetchone()[0] in (None, ""), "the advisor was set"


@pytest.mark.parametrize(
    "advisor_id",
    [ATTACKER.id, NAME, "", MINE + " ", MINE.upper(), "nobody", None, 1, issue(MINE)],
    ids=[
        "attacker",
        "display-name",
        "empty",
        "space",
        "upper",
        "nobody",
        "none",
        "int",
        "verified",
    ],
)
def test_owner_scope_refuses_an_advisor_outside_the_cast(
    owner: Connection, advisor_id: Any
) -> None:
    refused = "not one of the cast's advisors"
    with pytest.raises(GuardError, match=refused), owner_advisor_scope(owner, advisor_id):
        pytest.fail("entered")
    assert owner.info.transaction_status == IDLE


def test_owner_scope_takes_one_advisor_per_transaction(owner: Connection) -> None:
    with owner_advisor_scope(owner, MINE):
        with pytest.raises(GuardError, match="not idle"), owner_advisor_scope(owner, OTHER):
            pytest.fail("a second advisor in one transaction")
        assert owner.execute(SETTING).fetchone() == (MINE,)
    with (
        owner.transaction(force_rollback=True),
        pytest.raises(GuardError, match="not idle"),
        owner_advisor_scope(owner, MINE),
    ):
        pytest.fail("entered inside an open transaction")


CLEAN = (names.MIGRATOR_ROLE, names.OWNER_ROLE, False, False, None, names.SEARCH_PATH)


@pytest.mark.parametrize(
    ("row", "needle"),
    [
        ((*CLEAN[:2], True, False, *CLEAN[4:]), "superuser or has BYPASSRLS"),
        ((*CLEAN[:3], True, *CLEAN[4:]), "superuser or has BYPASSRLS"),
        ((*CLEAN[:4], MINE, CLEAN[5]), "already set"),
        ((*CLEAN[:5], "public"), "search_path"),
        ((names.APP_ROLE, names.APP_ROLE, *CLEAN[2:]), "expected"),
        ((names.MIGRATOR_ROLE, names.MIGRATOR_ROLE, *CLEAN[2:]), "expected"),
        (None, "no pg_roles row"),
    ],
)
def test_planted_owner_sessions_are_refused(row: tuple | None, needle: str) -> None:
    found = owner_session_problems(row)
    assert any(needle in item for item in found), found


def test_a_clean_owner_session_passes() -> None:
    assert owner_session_problems(CLEAN) == []
    assert owner_session_problems((*CLEAN[:4], "", CLEAN[5])) == []


# The loader refuses every login but the migrator, before any write.


def test_the_loader_refuses_the_app_login(
    loaded: dict, corpus: load.Corpus, embedder: OnnxEmbedder, su: Connection
) -> None:
    before = snapshot(su)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        load.run(APP_URL, corpus, embedder, embedder.tokenizer)
    assert snapshot(su) == before


def test_the_loader_refuses_the_superuser(
    loaded: dict, corpus: load.Corpus, embedder: OnnxEmbedder, su: Connection
) -> None:
    before = snapshot(su)
    with pytest.raises(MigrationError, match="expected"):
        load.run(SUPERUSER_URL, corpus, embedder, embedder.tokenizer)
    assert snapshot(su) == before


@pytest.mark.parametrize("role", [names.APP_ROLE, "postgres"])
def test_make_load_refuses_another_login(
    loaded: dict,
    corpus_dir: Path,
    su: Connection,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    role: str,
) -> None:
    url = loopback_database_url(role)
    monkeypatch.setenv(env_name("migration_database_url"), url)
    before = snapshot(su)
    assert load.main(["--corpus", str(corpus_dir)]) == 1
    err = capsys.readouterr().err
    assert "load: refused:" in err and f"must log in as {names.MIGRATOR_ROLE}" in err, err
    assert url not in err, "the refused URL was echoed"
    assert snapshot(su) == before


# A row of another advisor, planted into an advisor's batch.


@pytest.mark.parametrize(
    ("variant", "refusal"),
    [
        ("another advisor's document under this advisor's mandate", "InsufficientPrivilege"),
        ("this advisor's document under another advisor's mandate", "ForeignKeyViolation"),
    ],
)
def test_a_planted_document_of_another_advisor_is_refused(
    loaded: dict,
    prepared: load.Prepared,
    owner: Connection,
    su: Connection,
    variant: str,
    refusal: str,
) -> None:
    mine = batch_of(prepared, MINE)
    theirs = batch_of(prepared, OTHER)
    base = mine.documents[0]
    if variant.startswith("another"):
        # The other advisor's id on a document of this advisor's mandate, in this
        # advisor's transaction.
        planted = dataclasses.replace(base, id="planted-1", advisor_id=OTHER)
    else:
        # This advisor's id on a document of the other advisor's mandate.
        planted = dataclasses.replace(base, id="planted-1", mandate_id=theirs.mandates[0].id)
    batch = dataclasses.replace(mine, documents=(*mine.documents, planted))
    before = snapshot(su)
    with pytest.raises(psycopg.Error) as refused:
        load.write(owner, with_batch(prepared, batch))
    assert type(refused.value).__name__ == refusal, refused.value
    if refusal == "InsufficientPrivilege":
        assert "row-level security policy" in str(refused.value)
    assert owner.info.transaction_status == IDLE
    # Nothing else was committed: not this advisor's rows, nor any later advisor's or sheet.
    assert snapshot(su) == before


def test_a_load_writes_exactly_the_cast(prepared: load.Prepared, owner: Connection) -> None:
    for advisors in (prepared.advisors[:1], prepared.advisors[::-1], ()):
        with pytest.raises(load.LoadError, match="exactly the cast's advisors"):
            load.write(owner, dataclasses.replace(prepared, advisors=advisors))


# The load.


def test_the_corpus_is_loaded(
    loaded: dict,
    corpus: load.Corpus,
    prepared: load.Prepared,
    embedder: OnnxEmbedder,
    su: Connection,
) -> None:
    advisors = su.execute("SELECT advisor_id, display_name FROM advisors ORDER BY 1").fetchall()
    assert advisors == sorted((p.id, p.name) for p in branding.ADVISORS)
    per_advisor = dict(
        su.execute("SELECT advisor_id, count(*) FROM mandates GROUP BY 1").fetchall()
    )
    assert per_advisor == {p.id: 13 for p in branding.ADVISORS} | {MINE: 14}, per_advisor
    counts = {name: rows for name, (rows, _) in loaded.items()}
    chunks = sum(len(batch.chunks) for batch in prepared.advisors)
    assert counts == {
        "advisors": 3,
        "mandates": 40,
        "mandate_contacts": 40,
        "documents": 344,
        "chunks": chunks,
        "product_sheets": 6,
    }, counts
    assert chunks >= 344
    without = su.execute(
        "SELECT count(*) FROM documents d WHERE NOT EXISTS (SELECT 1 FROM chunks c"
        " WHERE c.advisor_id = d.advisor_id AND c.document_id = d.id)"
    ).fetchone()
    assert without == (0,), "a document without a chunk"
    # Every stored passage fits the model with its prefix (ADR 0005's chunk cap).
    over = [
        chunk_id
        for chunk_id, body in su.execute("SELECT id, body FROM chunks").fetchall()
        if embedder.tokenizer.count(passage_input(body)) > CHUNK_TOKENS
    ]
    assert not over, over


def test_a_second_load_gives_the_same_rows(
    loaded: dict,
    corpus: load.Corpus,
    corpus_dir: Path,
    su: Connection,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert snapshot(su) == loaded
    assert load.main(["--corpus", str(corpus_dir)]) == 0
    out = capsys.readouterr().out
    assert snapshot(su) == loaded, "a second load changed rows"
    lines = out.splitlines()
    assert lines[0] == (
        f"load: corpus {load.MANIFEST_SHA256[:12]}...: 40 mandates, 344 documents, 6 product sheets"
    ), lines
    assert lines[-1] == "load: product sheets: 6", lines
    assert [line.split(":")[1].strip() for line in lines[1:-1]] == [
        p.id for p in branding.ADVISORS
    ], lines
    for item in (*corpus.documents[:20], *corpus.sheets):
        assert item.body[:30] not in out and item.title not in out, "text in the output"


def test_every_vector_carries_the_model(loaded: dict, su: Connection) -> None:
    for name in ("chunks", "product_sheets"):
        found = su.execute(
            sql.SQL(
                "SELECT DISTINCT embedding_model, embedding_revision, vector_dims(embedding)"
                " FROM {}"
            ).format(table(name))
        ).fetchall()
        assert found == [(MODEL_ID, REVISION, 384)], (name, found)


def test_contacts_are_the_mandate_records_client_email(
    loaded: dict, corpus_dir: Path, su: Connection
) -> None:
    records = [
        json.loads(line)
        for line in (corpus_dir / "mandates.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    expected = sorted((r["advisor_id"], r["id"], r["client_email"]) for r in records)
    found = su.execute("SELECT advisor_id, mandate_id, email FROM mandate_contacts ORDER BY 1, 2")
    assert found.fetchall() == expected
    # Never an address from an e-mail: the advisors' own, which every e-mail carries, are absent.
    emails = {email for *_, email in expected}
    assert not emails & {p.email for p in branding.ADVISORS}


def test_sheets_store_their_exact_url(loaded: dict, corpus: load.Corpus, su: Connection) -> None:
    found = su.execute("SELECT id, url, lang FROM product_sheets ORDER BY 1").fetchall()
    assert found == sorted((s.id, s.url, s.lang) for s in corpus.sheets)
    staff = branding.DOMAINS.staff
    for sheet_id, url, lang in found:
        slug = sheet_id.removeprefix("product-")
        assert url == f"https://{staff}/{generate.PRODUCT_PATH[lang]}/{slug}", url


# Origin and trust (ADR 0010).


def test_documents_carry_origin_and_trust(
    loaded: dict, corpus: load.Corpus, su: Connection
) -> None:
    trust = trust_of()
    found = {
        (origin, level): rows
        for origin, level, rows in su.execute(
            "SELECT origin, trust, count(*) FROM documents GROUP BY 1, 2"
        ).fetchall()
    }
    expected = Counter(d.origin for d in corpus.documents)
    assert found == {(origin, trust[origin]): rows for origin, rows in expected.items()}
    assert found[("mailbox", "untrusted")] > 100, found
    sheets = su.execute("SELECT DISTINCT origin, trust FROM product_sheets").fetchall()
    assert sheets == [("product_catalogue", "internal")], sheets


def probe_row(name: str, origin: str, n: int) -> dict[str, object]:
    """A row of the text table name with origin, for a rolled-back probe."""
    text = {"title": "Probe", "body": "Probe text.", "lang": "english", "origin": origin}
    if name == "documents":
        mandate = branding.DEMO_MANDATE
        return {
            "advisor_id": mandate.advisor_id,
            "id": f"probe-{n}",
            "mandate_id": mandate.id,
        } | text
    if name == "product_sheets":
        url = f"https://{branding.DOMAINS.staff}/products/probe-{n}"
        model = {"embedding": vector(n), "embedding_model": "probe", "embedding_revision": "probe"}
        return {"id": f"probe-{n}", "url": url} | text | model
    raise AssertionError(f"no probe row for {name}: PR 14 adds drafts and followups here")


def insert(conn: Connection, name: str, row: dict[str, object]) -> Any:
    statement = sql.SQL("INSERT INTO {} ({}) VALUES ({}) RETURNING trust").format(
        table(name),
        sql.SQL(", ").join(sql.Identifier(column) for column in row),
        sql.SQL(", ").join(sql.Placeholder(column) for column in row),
    )
    return conn.execute(statement, row).fetchone()[0]


def existing_text_tables(conn: Connection) -> list[str]:
    tables = block()["tables"]
    found = [
        name
        for name in sorted(tables)
        if conn.execute("SELECT to_regclass(%s)", (f"{names.SCHEMA}.{name}",)).fetchone()[0]
    ]
    assert {"documents", "product_sheets"} <= set(found), found
    return found


def test_each_table_allows_exactly_its_origins(loaded: dict, su: Connection) -> None:
    tables, trust = block()["tables"], trust_of()
    candidates = [*sorted(trust), "a_new_origin"]
    for name in existing_text_tables(su):
        allowed, levels = [], {}
        with su.transaction(force_rollback=True):
            for n, origin in enumerate(candidates):
                try:
                    with su.transaction():
                        levels[origin] = insert(su, name, probe_row(name, origin, n))
                    allowed.append(origin)
                except psycopg.errors.CheckViolation as error:
                    assert error.diag.constraint_name == f"{name}_origin_check", error
        assert allowed == sorted(tables[name]), (name, allowed)
        assert levels == {origin: trust[origin] for origin in allowed}, (name, levels)


@pytest.mark.parametrize("name", ["documents", "product_sheets"])
def test_trust_fails_closed_for_other_origins(loaded: dict, su: Connection, name: str) -> None:
    runtime = sorted(block()["runtime_origins"])
    others = [*runtime, "a_new_origin", "Mailbox", "KYC_REGISTER", "call_notes "]
    with su.transaction(force_rollback=True):
        su.execute(
            sql.SQL("ALTER TABLE {} DROP CONSTRAINT {}").format(
                table(name), sql.Identifier(f"{name}_origin_check")
            )
        )
        levels = {
            origin: insert(su, name, probe_row(name, origin, 100 + n))
            for n, origin in enumerate(others)
        }
    assert levels == dict.fromkeys(others, "untrusted"), levels
    assert runtime == ["agent_draft", "agent_followup"], runtime


@pytest.mark.parametrize("name", ["documents", "product_sheets"])
def test_trust_cannot_be_written(loaded: dict, su: Connection, name: str) -> None:
    # Not by the loader, not by the owner, not even by the superuser (PostgreSQL's rule).
    row = probe_row(name, "mailbox" if name == "documents" else "product_catalogue", 200)
    with su.transaction(force_rollback=True):
        with pytest.raises(psycopg.errors.GeneratedAlways), su.transaction():
            insert(su, name, row | {"trust": "internal"})
        insert(su, name, row)
        with pytest.raises(psycopg.errors.GeneratedAlways), su.transaction():
            su.execute(
                sql.SQL("UPDATE {} SET trust = 'internal' WHERE id = %s").format(table(name)),
                (row["id"],),
            )
        trust = su.execute(
            sql.SQL("SELECT trust FROM {} WHERE id = %s").format(table(name)), (row["id"],)
        ).fetchone()
    assert trust == ("untrusted" if name == "documents" else "internal",)


# Attack documents (M1 PR 17 loads them; the rules are here).


def test_attack_documents_load_as_mailbox_and_leave_with_the_next_load(
    loaded: dict,
    corpus: load.Corpus,
    prepared: load.Prepared,
    embedder: OnnxEmbedder,
    owner: Connection,
    su: Connection,
) -> None:
    record = {
        "id": "rt-probe-1",
        "mandate_id": branding.DEMO_MANDATE.id,
        "lang": "german",
        "title": "Rückfrage zum Bericht",
        "body": "Eine Probe für den Loader, ohne Anweisung.",
        "origin": "call_notes",  # ignored: every attack document is mailbox
    }
    attacks = load.load_attack_documents([record], corpus)
    # Cut and embed the attack document alone, then add it to the full load.
    bare = dataclasses.replace(corpus, documents=(), sheets=())
    extra = batch_of(load.prepare(bare, embedder, embedder.tokenizer, attacks=attacks), MINE)
    mine = batch_of(prepared, MINE)
    batch = dataclasses.replace(
        mine, documents=mine.documents + extra.documents, chunks=mine.chunks + extra.chunks
    )
    try:
        load.write(owner, with_batch(prepared, batch))
        found = su.execute(
            "SELECT advisor_id, mandate_id, origin, trust FROM documents WHERE id = %s",
            ("rt-probe-1",),
        ).fetchall()
        assert found == [(MINE, branding.DEMO_MANDATE.id, "mailbox", "untrusted")], found
        chunks = su.execute(
            "SELECT count(*), min(embedding_model) FROM chunks WHERE document_id = %s",
            ("rt-probe-1",),
        ).fetchone()
        assert chunks[0] >= 1 and chunks[1] == MODEL_ID, chunks
    finally:
        load.write(owner, prepared)
    left = su.execute(
        "SELECT (SELECT count(*) FROM documents WHERE id ILIKE 'rt-%'),"
        " (SELECT count(*) FROM chunks WHERE document_id ILIKE 'rt-%')"
    ).fetchone()
    assert left == (0, 0), "a load without attack documents left one"
    assert snapshot(su) == loaded


ATTACK_IDS = [
    ("documents", {"id": "rt-x", "origin": "call_notes"}, "documents_attack_id_check"),
    ("documents", {"id": "RT-x", "origin": "kyc_register"}, "documents_attack_id_check"),
    ("documents", {"id": "Rt-x", "origin": "mailbox"}, None),
    ("mandates", {"advisor_id": MINE, "id": "rt-m", "family": "X"}, "mandates_attack_id_check"),
    ("advisors", {"advisor_id": "rt-a", "display_name": "X"}, "advisors_attack_id_check"),
    ("product_sheets", {"id": "rt-s"}, "product_sheets_attack_id_check"),
    (
        "mandate_contacts",
        {"advisor_id": MINE, "mandate_id": "RT-m", "email": f"x@{branding.DOMAINS.staff}"},
        "mandate_contacts_attack_id_check",
    ),
]


@pytest.mark.parametrize(("name", "fields", "constraint"), ATTACK_IDS)
def test_the_schema_keeps_attack_ids_to_mailbox_documents(
    loaded: dict, su: Connection, name: str, fields: dict, constraint: str | None
) -> None:
    if name in ("documents", "product_sheets"):
        row = probe_row(name, "product_catalogue", 300) | fields
    else:
        row = fields
    with su.transaction(force_rollback=True):
        statement = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
            table(name),
            sql.SQL(", ").join(sql.Identifier(column) for column in row),
            sql.SQL(", ").join(sql.Placeholder(column) for column in row),
        )
        if constraint is None:
            su.execute(statement, row)  # control: an attack id on a mailbox document
            return
        with pytest.raises(psycopg.errors.CheckViolation) as refused:
            su.execute(statement, row)
    assert refused.value.diag.constraint_name == constraint, refused.value


# Product sheets, as the app role sees them after a load.


def test_every_advisor_reads_the_sheets_and_no_one_else(
    loaded: dict, app_pool: ConnectionPool
) -> None:
    for person in branding.ADVISORS:
        with advisor_transaction(app_pool, issue(person.id)) as conn:
            assert conn.execute("SELECT count(*) FROM product_sheets").fetchone() == (6,)
    with app_pool.connection() as conn:
        assert conn.execute("SELECT count(*) FROM product_sheets").fetchone() == (0,)
