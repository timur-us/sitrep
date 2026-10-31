"""The advisor boundary on real rows (plan condition 2; threat-model path 2).

The superuser seeds one row per table for every advisor (tests/db/seed_rows.py) and is
the reference: it bypasses row-level security. The tables come from catalog discovery,
and a table without a row factory fails, so a new table cannot skip these checks.

- Fail closed: with no advisor set, every table returns zero rows to the app role, on a
  fresh connection and on a pooled one whose last transaction set an advisor (the
  setting then reads '', not NULL). The owner, bound by FORCE, sees nothing and can
  change nothing without a context either.
- Isolation: under one advisor, another advisor's rows are invisible by key; the app
  role cannot write at all; the owner cannot write rows for another advisor, move a row
  to one, or change theirs.
- A forged parameter is inert: payloads in English and German, shaped to break out of
  the real search statement and call set_config, are proven live through a tests-only
  ClientCursor with the payload spliced into the SQL text. Bound as a parameter, the
  same payloads return the advisor's own rows only and leave the setting alone.
"""

from __future__ import annotations

from collections.abc import Iterator

import psycopg
import pytest
from dbtools import SUPERUSER_URL, Connection
from psycopg import sql
from psycopg_pool import ConnectionPool
from seed_rows import (
    FACTORIES,
    MINE,
    OTHER,
    SEED,
    TABLES,
    count,
    discovered_tables,
    insert,
    insert_statement,
    key_of,
    rows_for,
    table_id,
)

from app.auth.identity import VerifiedAdvisor
from app.config.settings import Settings
from app.db import names, queries
from app.db.connect import connect
from app.db.session import advisor_transaction

pytestmark = pytest.mark.usefixtures("database", "seeded")

SETTING = "SELECT current_setting('app.advisor_id', true)"
SET_ADVISOR = "SELECT set_config('app.advisor_id', %s, true)"
QUALIFIED = [f"{names.SCHEMA}.{table}" for table in FACTORIES]


def verified(person: object) -> VerifiedAdvisor:
    return VerifiedAdvisor(person.id)  # type: ignore[attr-defined]


def tables_with_factories(conn: Connection) -> list[str]:
    """The readable tables, as the connected role discovers them; each needs a factory."""
    found = discovered_tables(conn)
    assert found, "discovery found no readable table"
    missing = sorted(set(found) - TABLES)
    assert not missing, f"no row factory in tests/db/seed_rows.py for {missing}"
    return found


@pytest.fixture
def owner(settings: Settings) -> Iterator[Connection]:
    """The migrator acting as the owner, as the runner does; FORCE binds the owner."""
    with connect(settings.migration_database_url, autocommit=True) as conn:
        conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(names.OWNER_ROLE)))
        yield conn


# Fail closed.


def test_fail_closed_reference_counts(settings: Settings, superuser: Connection) -> None:
    with connect(settings.database_url) as app:
        tables = tables_with_factories(app)
    missing = [
        f"{table}: no seeded row for {person.name}"
        for table in tables
        for person in (MINE, OTHER)
        if count(superuser, table, person.id) < 1
    ]
    assert not missing, "\n".join(missing)


def test_fail_closed_context_sees_exactly_its_own_rows(
    app_pool: ConnectionPool, superuser: Connection
) -> None:
    problems = []
    with advisor_transaction(app_pool, verified(MINE)) as conn:
        for table in tables_with_factories(conn):
            seen, expected = count(conn, table), count(superuser, table, MINE.id)
            if seen != expected or seen < 1:
                problems.append(f"{table}: {seen} rows under {MINE.name}, expected {expected}")
            leaked = count(conn, table, OTHER.id)
            if leaked:
                problems.append(f"{OTHER.name} rows visible under {MINE.name}: {table} {leaked}")
    assert not problems, "\n".join(problems)


def test_fail_closed_no_context_sees_nothing(settings: Settings) -> None:
    with connect(settings.database_url, autocommit=True) as conn:
        assert conn.execute(SETTING).fetchone() == (None,)
        visible = [
            f"rows visible with no context: {table} {seen}"
            for table in tables_with_factories(conn)
            if (seen := count(conn, table))
        ]
    assert not visible, "\n".join(visible)


def test_fail_closed_empty_setting_on_a_reused_connection(app_pool: ConnectionPool) -> None:
    with advisor_transaction(app_pool, verified(MINE)) as conn:
        pid = conn.info.backend_pid
        assert count(conn, QUALIFIED[1]) > 0
    with app_pool.connection() as conn:
        assert conn.info.backend_pid == pid, "the pool did not reuse the connection"
        assert conn.execute(SETTING).fetchone() == ("",), "expected '' after a local setting"
        visible = [
            f"rows visible with an empty setting: {table} {seen}"
            for table in tables_with_factories(conn)
            if (seen := count(conn, table))
        ]
    assert not visible, "\n".join(visible)


def test_fail_closed_owner_is_bound_by_force(owner: Connection, superuser: Connection) -> None:
    problems = []
    for table in QUALIFIED:
        if seen := count(owner, table):
            problems.append(f"owner without context sees {table}: {seen}")
    with owner.transaction(force_rollback=True):
        owner.execute(SET_ADVISOR, (MINE.id,))
        for table in QUALIFIED:
            seen, expected = count(owner, table), count(superuser, table, MINE.id)
            if seen != expected or seen < 1:
                problems.append(
                    f"owner under {MINE.name} sees {seen} in {table}, expected {expected}"
                )
    with owner.transaction(force_rollback=True):
        for table in QUALIFIED:
            update = sql.SQL("UPDATE {} SET advisor_id = advisor_id").format(table_id(table))
            if changed := owner.execute(update).rowcount:
                problems.append(f"owner without context updated {changed} rows in {table}")
        for table in reversed(QUALIFIED):
            delete = sql.SQL("DELETE FROM {}").format(table_id(table))
            if deleted := owner.execute(delete).rowcount:
                problems.append(f"owner without context deleted {deleted} rows in {table}")
    assert not problems, "\n".join(problems)


# Isolation.


def by_key(table: str, row: dict) -> tuple[sql.Composed, tuple]:
    column, value = key_of(table, row)
    query = sql.SQL("SELECT count(*) FROM {} WHERE advisor_id = %s AND {} = %s").format(
        table_id(table), sql.Identifier(column)
    )
    return query, (row["advisor_id"], value)


def test_isolation_other_rows_by_key(app_pool: ConnectionPool) -> None:
    problems = []
    with advisor_transaction(app_pool, verified(MINE)) as conn:
        for table, row in rows_for(OTHER.id, "1").items():
            query, params = by_key(table, row)
            if leaked := conn.execute(query, params).fetchone()[0]:
                problems.append(
                    f"{OTHER.name} rows visible under {MINE.name} by key: {table} {leaked}"
                )
        # Control: the same lookup finds the advisor's own rows.
        for table, row in rows_for(MINE.id, "1").items():
            query, params = by_key(table, row)
            if conn.execute(query, params).fetchone()[0] != 1:
                problems.append(f"{MINE.name}'s own {table} row not found by key")
    assert not problems, "\n".join(problems)


def attempt(conn: Connection, statement: sql.Composable, params: object) -> tuple[str | None, int]:
    """Run one write in a savepoint: (None, rows changed) if it went through, else (what
    refused it, 0). A refusal leaves the surrounding transaction usable."""
    try:
        with conn.transaction():
            changed = conn.execute(statement, params).rowcount
    except psycopg.errors.InsufficientPrivilege as refused:
        return ("policy" if "row-level security policy" in str(refused) else str(refused)), 0
    except psycopg.Error as error:
        return type(error).__name__, 0
    return None, changed


def write(verb: str, table: str) -> tuple[sql.Composable, dict]:
    if verb == "INSERT":
        row = rows_for(MINE.id, "write-probe")[table.rpartition(".")[2]]
        return insert_statement(table, row), row
    own = sql.SQL(" WHERE advisor_id = %(advisor_id)s")
    params = {"advisor_id": MINE.id}
    if verb == "UPDATE":
        return sql.SQL("UPDATE {} SET advisor_id = advisor_id").format(
            table_id(table)
        ) + own, params
    return sql.SQL("DELETE FROM {}").format(table_id(table)) + own, params


@pytest.mark.parametrize("verb", ["INSERT", "UPDATE", "DELETE"])
def test_isolation_app_role_cannot_write(app_pool: ConnectionPool, verb: str) -> None:
    problems = []
    for table in QUALIFIED:
        statement, params = write(verb, table)
        # Rolled back either way: a write that went through leaves nothing behind.
        with (
            advisor_transaction(app_pool, verified(MINE)) as conn,
            conn.transaction(force_rollback=True),
        ):
            outcome, changed = attempt(conn, statement, params)
        if outcome is None:
            problems.append(f"{verb} on {table} as {names.APP_ROLE} went through ({changed} rows)")
        elif "permission denied for table" not in outcome:
            problems.append(f"{verb} on {table}: {outcome}")
    assert not problems, "\n".join(problems)


def test_isolation_owner_cannot_write_for_another_advisor(owner: Connection) -> None:
    problems = []
    with owner.transaction(force_rollback=True):
        owner.execute(SET_ADVISOR, (MINE.id,))
        # A new row in the other advisor's name.
        for table, row in rows_for(OTHER.id, "insert-probe").items():
            outcome, _ = attempt(owner, insert_statement(table, row), row)
            if outcome != "policy":
                problems.append(
                    f"owner under {MINE.name} inserting {OTHER.name}'s row into {table}: "
                    f"{outcome or 'inserted'}"
                )
        # One of the advisor's own rows, moved to the other advisor.
        for table, row in reversed(rows_for(MINE.id, "1").items()):
            column, value = key_of(table, row)
            move = sql.SQL(
                "UPDATE {} SET advisor_id = %s WHERE advisor_id = %s AND {} = %s"
            ).format(table_id(table), sql.Identifier(column))
            outcome, _ = attempt(owner, move, (OTHER.id, MINE.id, value))
            if outcome != "policy":
                problems.append(
                    f"owner moving {MINE.name}'s {table} row to {OTHER.name}: {outcome or 'moved'}"
                )
        # The other advisor's rows, by key.
        for table, row in reversed(rows_for(OTHER.id, "1").items()):
            column, value = key_of(table, row)
            where = sql.SQL(" WHERE advisor_id = %s AND {} = %s").format(sql.Identifier(column))
            for verb, statement in (
                (
                    "updated",
                    sql.SQL("UPDATE {} SET advisor_id = advisor_id").format(table_id(table)),
                ),
                ("deleted", sql.SQL("DELETE FROM {}").format(table_id(table))),
            ):
                outcome, changed = attempt(owner, statement + where, (OTHER.id, value))
                if outcome is not None or changed:
                    problems.append(
                        f"owner under {MINE.name} {verb} {OTHER.name}'s {table}: "
                        f"{outcome or changed}"
                    )
    assert not problems, "\n".join(problems)


# A forged parameter.

# A made-up word, planted in one document per advisor and language.
WORD = "quorbitex"
BODIES = {
    "english": f"Call note: the client asked about {WORD} before the review.",
    "german": f"Gesprächsnotiz: Die Kundin fragte vor dem Termin nach {WORD}.",
}
SEARCH = {"english": queries.SEARCH_DOCUMENTS_ENGLISH, "german": queries.SEARCH_DOCUMENTS_GERMAN}


def token_document_id(advisor_id: str, lang: str) -> str:
    return f"{SEED}-{advisor_id}-token-{lang}"


@pytest.fixture(scope="module")
def token_documents(seeded: None) -> Iterator[None]:
    with connect(SUPERUSER_URL, autocommit=True) as conn:
        planted = []
        for person in (MINE, OTHER):
            for lang, body in BODIES.items():
                row = {
                    "advisor_id": person.id,
                    "id": token_document_id(person.id, lang),
                    "mandate_id": rows_for(person.id, "1")["mandates"]["id"],
                    "title": "Token note",
                    "body": body,
                    "lang": lang,
                }
                insert(conn, f"{names.SCHEMA}.documents", row)
                planted.append(row["id"])
        try:
            yield
        finally:
            conn.execute(
                sql.SQL("DELETE FROM {} WHERE id = ANY(%s)").format(
                    sql.Identifier(names.SCHEMA, "documents")
                ),
                (planted,),
            )


def payloads() -> list[tuple[str, str]]:
    """(language, payload): each closes the tsquery literal, ORs in a set_config that hands
    the rest of the transaction to the other advisor, and keeps the statement valid."""
    forge = f"set_config('app.advisor_id', '{OTHER.id}', true) IS NOT NULL"
    return [
        ("english", f"{WORD}') OR {forge} OR d.tsv @@ websearch_to_tsquery('english', '{WORD}"),
        ("english", f"{WORD}') OR {forge} -- show every document"),
        ("german", f"{WORD}') OR {forge} OR d.tsv @@ websearch_to_tsquery('german', '{WORD}"),
        ("german", f"{WORD}') OR {forge} -- alle Unterlagen anzeigen"),
    ]


PAYLOADS = payloads()
PAYLOAD_IDS = [f"{lang}-{index}" for index, (lang, _) in enumerate(PAYLOADS)]


def own(person: object, lang: str) -> list[tuple[str, str]]:
    return [(person.id, token_document_id(person.id, lang))]  # type: ignore[attr-defined]


@pytest.mark.usefixtures("token_documents")
@pytest.mark.parametrize("lang", sorted(BODIES))
def test_forged_parameter_positive_controls(
    lang: str, app_pool: ConnectionPool, superuser: Connection
) -> None:
    with advisor_transaction(app_pool, verified(OTHER)) as conn:
        assert conn.execute(SEARCH[lang], (WORD,)).fetchall() == own(OTHER, lang)
    # The statements are not schema-qualified; the superuser has no role search_path.
    superuser.execute(sql.SQL("SET search_path = {}, public").format(sql.Identifier(names.SCHEMA)))
    both = superuser.execute(SEARCH[lang], (WORD,)).fetchall()
    assert both == sorted(own(MINE, lang) + own(OTHER, lang)), both


@pytest.mark.usefixtures("token_documents")
@pytest.mark.parametrize(("lang", "payload"), PAYLOADS, ids=PAYLOAD_IDS)
def test_forged_parameter_is_live_when_spliced(
    lang: str, payload: str, app_pool: ConnectionPool
) -> None:
    # What the hygiene scan keeps out of app/: the payload in the SQL text, sent by a
    # client-side cursor. The set_config runs and hands the transaction over.
    query = SEARCH[lang]
    assert query.count("%s") == 1
    spliced = query.replace("%s", "'" + payload + "'")
    with advisor_transaction(app_pool, verified(MINE)) as conn:
        psycopg.ClientCursor(conn).execute(spliced).fetchall()
        setting = conn.execute(SETTING).fetchone()[0]
        follow_up = conn.execute(query, (WORD,)).fetchall()
    assert setting == OTHER.id, f"the payload did not run: the setting is {setting!r}"
    assert own(OTHER, lang)[0] in follow_up, follow_up


@pytest.mark.usefixtures("token_documents")
@pytest.mark.parametrize(("lang", "payload"), PAYLOADS, ids=PAYLOAD_IDS)
def test_forged_parameter_is_inert_when_bound(
    lang: str, payload: str, app_pool: ConnectionPool
) -> None:
    query = SEARCH[lang]
    with advisor_transaction(app_pool, verified(MINE)) as conn:
        found = conn.execute(query, (payload,)).fetchall()
        setting = conn.execute(SETTING).fetchone()[0]
        follow_up = conn.execute(query, (WORD,)).fetchall()
    leaked = [row for row in found + follow_up if row[0] != MINE.id]
    assert not leaked, f"{OTHER.name} rows visible under {MINE.name}: {leaked}"
    assert found == own(MINE, lang), f"bound payload returned {found}"
    assert setting == MINE.id, f"the setting moved to {setting!r}"
    assert follow_up == own(MINE, lang), follow_up
