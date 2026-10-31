"""The advisor boundary in the catalog (plan condition 1, FORCE half; threat-model path 2).

Discovery reads pg_catalog as the app role, over every schema that is not a system
schema: every relation it can read is a table with row-level security enabled and
forced, or a security_invoker view; materialized views and foreign tables are refused.
The readable set is exactly the expected tables, each with exactly one policy of the
canonical shape. The grants are exact: SELECT only on the tables, nothing on functions
or sequences, USAGE on no type but the tables' own row types. No SECURITY DEFINER
function exists outside the system schemas. Every unique index, primary keys included,
contains advisor_id, which closes the referential-integrity channel: a key that exists
for another advisor cannot be probed through a unique or foreign-key violation.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from dbtools import Connection, column
from seed_rows import TABLES, readable

from app.config.settings import Settings
from app.db import names
from app.db.connect import connect
from app.db.migrate import RECORD

pytestmark = pytest.mark.usefixtures("database")

APP = names.APP_ROLE
OWNER = names.OWNER_ROLE
# How PostgreSQL deparses the one policy every table carries (USING and WITH CHECK).
CANONICAL = "(advisor_id = NULLIF(current_setting('app.advisor_id'::text, true), ''::text))"
TABLE_PRIVILEGES = (
    "SELECT",
    "INSERT",
    "UPDATE",
    "DELETE",
    "TRUNCATE",
    "REFERENCES",
    "TRIGGER",
    "MAINTAIN",
)
# relacl of every table: the owner holds everything, the app SELECT, PUBLIC nothing.
TABLE_ACL = {f"{OWNER}=arwdDxtm/{OWNER}", f"{APP}=r/{OWNER}"}
# Schema names starting with pg_ belong to the system (pg_catalog, pg_toast, pg_temp_N).
SYSTEM = "pg\\_%"


@pytest.fixture
def app(settings: Settings) -> Iterator[Connection]:
    with connect(settings.database_url, autocommit=True) as conn:
        yield conn


def relation_problems(rows: list[tuple]) -> list[str]:
    """What breaks the rule for each readable relation, from READABLE's columns."""
    problems = []
    for schema, name, kind, enabled, forced, options in rows:
        relation = f"{schema}.{name}"
        if kind in ("r", "p"):
            if not enabled:
                problems.append(f"relrowsecurity false on {relation}")
            if not forced:
                problems.append(f"relforcerowsecurity false on {relation}")
        elif kind == "v":
            if not {"security_invoker=true", "security_invoker=on"} & set(options):
                problems.append(f"view {relation} without security_invoker")
        else:
            problems.append(f"{relation} has relkind {kind!r}, which RLS cannot cover")
    return problems


def test_readable_relations_are_row_secured(app: Connection) -> None:
    rows = readable(app)
    found = {f"{schema}.{name}" for schema, name, *_ in rows}
    assert found == TABLES, f"the app role reads {sorted(found)}, expected exactly {sorted(TABLES)}"
    problems = relation_problems(rows)
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize(
    ("row", "problem"),
    [
        (("s", "t", "r", False, True, []), "relrowsecurity false on s.t"),
        (("s", "t", "p", True, False, []), "relforcerowsecurity false on s.t"),
        (("s", "v", "v", False, False, []), "view s.v without security_invoker"),
        (("s", "m", "m", True, True, []), "s.m has relkind 'm'"),
        (("s", "f", "f", True, True, []), "s.f has relkind 'f'"),
    ],
)
def test_planted_relations_are_reported(row: tuple, problem: str) -> None:
    assert any(problem in found for found in relation_problems([row])), relation_problems([row])


def test_planted_compliant_relations_pass() -> None:
    rows = [
        ("s", "t", "r", True, True, []),
        ("s", "v", "v", False, False, ["security_invoker=true"]),
    ]
    assert relation_problems(rows) == []


def test_control_discovery_sees_a_readable_view(superuser: Connection) -> None:
    # A view the app role may read appears in discovery, and one without security_invoker
    # is reported: the discovery query is not blind to views. Rolled back.
    with superuser.transaction(force_rollback=True):
        superuser.execute(f"CREATE VIEW {names.SCHEMA}.discovery_probe AS SELECT 1 AS x")
        superuser.execute(f"GRANT SELECT ON {names.SCHEMA}.discovery_probe TO {APP}")
        superuser.execute(f"SET LOCAL ROLE {APP}")
        rows = readable(superuser)
    probe = [row for row in rows if row[1] == "discovery_probe"]
    assert len(probe) == 1, rows
    assert relation_problems(probe) == [
        f"view {names.SCHEMA}.discovery_probe without security_invoker"
    ]


def test_one_canonical_policy_per_table(app: Connection) -> None:
    rows = app.execute(
        "SELECT n.nspname || '.' || c.relname, p.polname, p.polcmd, p.polpermissive,"
        " p.polroles = '{0}', pg_get_expr(p.polqual, p.polrelid),"
        " pg_get_expr(p.polwithcheck, p.polrelid)"
        " FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
        " LEFT JOIN pg_policy p ON p.polrelid = c.oid"
        " WHERE n.nspname || '.' || c.relname = ANY(%s) ORDER BY 1, 2",
        (sorted(TABLES),),
    ).fetchall()
    policies: dict[str, list[tuple]] = {}
    for table, name, *shape in rows:
        policies.setdefault(table, [])
        if name is not None:
            policies[table].append(tuple(shape))
    assert set(policies) == TABLES, sorted(policies)
    canonical = ("*", True, True, CANONICAL, CANONICAL)
    wrong = [
        f"{table}: {len(found)} policies {found}, expected one {canonical}"
        for table, found in sorted(policies.items())
        if found != [canonical]
    ]
    assert not wrong, "\n".join(wrong)


SCHEMAS = [names.SCHEMA, names.META_SCHEMA]
RELATION_ACLS = (
    "SELECT n.nspname || '.' || c.relname, c.relacl::text[]"
    " FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
    " WHERE n.nspname = ANY(%s) AND c.relkind IN ('r', 'p', 'v', 'm', 'f', 'S') ORDER BY 1"
)
HELD = (
    "SELECT has_table_privilege(c.oid, %s) FROM pg_class c"
    " JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname || '.' || c.relname = %s"
)
COLUMN_GRANTS = (
    "SELECT c.relname || '.' || a.attname FROM pg_attribute a"
    " JOIN pg_class c ON c.oid = a.attrelid JOIN pg_namespace n ON n.oid = c.relnamespace"
    " WHERE n.nspname = ANY(%s) AND a.attacl IS NOT NULL"
)
USABLE_SEQUENCES = (
    "SELECT n.nspname || '.' || c.relname FROM pg_class c"
    " JOIN pg_namespace n ON n.oid = c.relnamespace"
    " WHERE c.relkind = 'S' AND n.nspname NOT LIKE %s AND n.nspname <> 'information_schema'"
    " AND (has_sequence_privilege(c.oid, 'USAGE') OR has_sequence_privilege(c.oid, 'SELECT')"
    " OR has_sequence_privilege(c.oid, 'UPDATE'))"
)
EXECUTABLE = (
    "SELECT p.oid::regprocedure::text FROM pg_proc p"
    " JOIN pg_namespace n ON n.oid = p.pronamespace"
    " WHERE n.nspname = ANY(%s) AND has_function_privilege(p.oid, 'EXECUTE')"
)
USABLE_TYPES = (
    "SELECT t.oid::regtype::text FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace"
    " WHERE n.nspname = ANY(%s) AND has_type_privilege(t.oid, 'USAGE')"
)
SECURITY_DEFINER = (
    "SELECT p.oid::regprocedure::text FROM pg_proc p"
    " JOIN pg_namespace n ON n.oid = p.pronamespace WHERE p.prosecdef"
    " AND n.nspname NOT LIKE %s AND n.nspname <> 'information_schema'"
)
# A table's row type, and its array type, cannot be revoked (see below).
ROW_TYPES = {table.rpartition(".")[2] for table in TABLES} | {RECORD}
ROW_TYPES |= {f"{name}[]" for name in ROW_TYPES}


def grant_problems(conn: Connection) -> list[str]:
    """Grants on the schemas' relations and columns beyond SELECT on the tables, as the
    connected role holds them."""
    rows = conn.execute(RELATION_ACLS, (SCHEMAS,)).fetchall()
    wrong = []
    for name, acl in rows:
        if name in TABLES and set(acl or []) != TABLE_ACL:
            wrong.append(f"{name}: relacl {acl}, expected {sorted(TABLE_ACL)}")
        held = [p for p in TABLE_PRIVILEGES if conn.execute(HELD, (p, name)).fetchone()[0]]
        expected = ["SELECT"] if name in TABLES else []
        if held != expected:
            wrong.append(f"{APP} holds {held} on {name}, expected {expected}")
    wrong += [f"column-level grant: {item}" for item in column(conn, COLUMN_GRANTS, (SCHEMAS,))]
    return wrong


def test_grants_on_tables_are_select_only(app: Connection) -> None:
    tables = {
        name
        for name, _ in app.execute(RELATION_ACLS, (SCHEMAS,)).fetchall()
        if name.startswith(f"{names.SCHEMA}.")
    }
    assert tables == TABLES, f"relations in {names.SCHEMA}: {sorted(tables)}"
    wrong = grant_problems(app)
    assert not wrong, "\n".join(wrong)


@contextmanager
def planted(superuser: Connection, *statements: str) -> Iterator[Connection]:
    """statements run as the owner, then the session as the app role: rolled back."""
    with superuser.transaction(force_rollback=True):
        superuser.execute(f"SET LOCAL ROLE {OWNER}")
        for statement in statements:
            superuser.execute(statement)
        superuser.execute(f"SET LOCAL ROLE {APP}")
        superuser.execute(f"SET LOCAL search_path = {names.SCHEMA}, public")
        yield superuser


def test_control_extra_grants_are_seen(superuser: Connection) -> None:
    # Review finding test-2: the grant check reports a table and a column grant.
    mandates = f"{names.SCHEMA}.mandates"
    with planted(
        superuser,
        f"GRANT UPDATE ON {mandates} TO {APP}",
        f"GRANT INSERT (family) ON {mandates} TO {APP}",
    ) as conn:
        wrong = grant_problems(conn)
    assert f"{APP} holds ['SELECT', 'UPDATE'] on {mandates}, expected ['SELECT']" in wrong, wrong
    assert any("relacl" in item and mandates in item for item in wrong), wrong
    assert "column-level grant: mandates.family" in wrong, wrong


def test_no_sequence_is_usable(app: Connection) -> None:
    usable = column(app, USABLE_SEQUENCES, (SYSTEM,))
    assert not usable, f"{APP} may use the sequences {usable}"


def test_control_a_usable_sequence_is_seen(superuser: Connection) -> None:
    sequence = f"{names.SCHEMA}.sequence_probe"
    with planted(
        superuser, f"CREATE SEQUENCE {sequence}", f"GRANT USAGE ON SEQUENCE {sequence} TO {APP}"
    ) as conn:
        found = column(conn, USABLE_SEQUENCES, (SYSTEM,))
    assert found == [sequence], found


def test_functions_and_types_are_not_granted(app: Connection) -> None:
    # sec-6 of the PR 5a review: 0001 revokes PUBLIC's default EXECUTE and USAGE for the
    # owner's functions and types. The default does not reach a table's row type, which
    # PUBLIC may use and REVOKE cannot change; it lends no access to rows. Every other
    # type in the schemas stays closed to the app role.
    functions = column(app, EXECUTABLE, (SCHEMAS,))
    assert not functions, f"{APP} may execute {functions}"
    types = set(column(app, USABLE_TYPES, (SCHEMAS,)))
    assert types == ROW_TYPES, f"{APP} may use the types {sorted(types)}"


def test_control_a_granted_function_and_type_are_seen(superuser: Connection) -> None:
    function = f"{names.SCHEMA}.grant_probe()"
    kind = f"{names.SCHEMA}.grant_probe_kind"
    with planted(
        superuser,
        f"CREATE FUNCTION {function} RETURNS int LANGUAGE sql AS 'SELECT 1'",
        f"GRANT EXECUTE ON FUNCTION {function} TO {APP}",
        f"CREATE TYPE {kind} AS ENUM ('a')",
        f"GRANT USAGE ON TYPE {kind} TO {APP}",
    ) as conn:
        functions = column(conn, EXECUTABLE, (SCHEMAS,))
        types = set(column(conn, USABLE_TYPES, (SCHEMAS,)))
    assert functions == ["grant_probe()"], functions
    assert types - ROW_TYPES == {"grant_probe_kind", "grant_probe_kind[]"}, sorted(types)


def test_no_security_definer_function(app: Connection) -> None:
    found = column(app, SECURITY_DEFINER, (SYSTEM,))
    assert not found, f"SECURITY DEFINER functions: {found}"


def test_control_a_security_definer_function_is_seen(superuser: Connection) -> None:
    function = f"{names.SCHEMA}.definer_probe()"
    with planted(
        superuser,
        f"CREATE FUNCTION {function} RETURNS int LANGUAGE sql SECURITY DEFINER AS 'SELECT 1'",
    ) as conn:
        found = column(conn, SECURITY_DEFINER, (SYSTEM,))
    assert found == ["definer_probe()"], found


UNIQUE_INDEXES = (
    "SELECT i.indexrelid::regclass::text, i.indisprimary,"
    " ARRAY(SELECT a.attname FROM unnest(i.indkey) AS k(attnum)"
    "  JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum)"
    " FROM pg_index i JOIN pg_class c ON c.oid = i.indrelid"
    " JOIN pg_namespace n ON n.oid = c.relnamespace"
    " WHERE n.nspname = %s AND i.indisunique ORDER BY 1"
)


def test_every_unique_index_contains_advisor_id(app: Connection) -> None:
    indexes = app.execute(UNIQUE_INDEXES, (names.SCHEMA,)).fetchall()
    primaries = [name for name, primary, _ in indexes if primary]
    assert len(primaries) == len(TABLES), f"primary keys: {primaries}"
    assert len(indexes) > len(primaries), f"no unique index besides the keys: {indexes}"
    wrong = [
        f"unique index without advisor_id: {name} {columns}"
        for name, _, columns in indexes
        if "advisor_id" not in columns
    ]
    assert not wrong, "\n".join(wrong)


# Foreign keys and exclusion constraints (review finding sec-6): a key check bypasses
# row-level security, so each one names advisor_id, in a foreign key on both sides at the
# same position.
CONSTRAINTS = (
    "SELECT c.conname, c.contype,"
    " ARRAY(SELECT a.attname FROM unnest(c.conkey) WITH ORDINALITY AS k(attnum, place)"
    "  JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum ORDER BY k.place),"
    " ARRAY(SELECT a.attname FROM unnest(c.confkey) WITH ORDINALITY AS k(attnum, place)"
    "  JOIN pg_attribute a ON a.attrelid = c.confrelid AND a.attnum = k.attnum ORDER BY k.place)"
    " FROM pg_constraint c JOIN pg_namespace n ON n.oid = c.connamespace"
    " WHERE n.nspname = %s AND c.contype IN ('f', 'x') ORDER BY 1"
)


def constraint_problems(rows: list[tuple]) -> list[str]:
    wrong = []
    for name, kind, columns, referenced in rows:
        if "advisor_id" not in columns:
            wrong.append(f"{name}: no advisor_id in {columns}")
        elif kind == "f" and (
            "advisor_id" not in referenced
            or columns.index("advisor_id") != referenced.index("advisor_id")
        ):
            wrong.append(f"{name}: {columns} references {referenced}")
    return wrong


def test_every_key_check_contains_advisor_id(app: Connection) -> None:
    rows = app.execute(CONSTRAINTS, (names.SCHEMA,)).fetchall()
    foreign = [row for row in rows if row[1] == "f"]
    assert len(foreign) == len(TABLES) - 1, f"foreign keys: {rows}"
    wrong = constraint_problems(rows)
    assert not wrong, "\n".join(wrong)


def test_control_key_checks_without_advisor_id_are_seen(superuser: Connection) -> None:
    probe = f"{names.SCHEMA}.constraint_probe"
    with planted(
        superuser,
        f"CREATE TABLE {probe} (advisor_id text, other text, slot int,"
        f" FOREIGN KEY (other) REFERENCES {names.SCHEMA}.advisors (advisor_id),"
        " EXCLUDE USING btree (slot WITH =))",
    ) as conn:
        wrong = constraint_problems(conn.execute(CONSTRAINTS, (names.SCHEMA,)).fetchall())
    assert any("constraint_probe_other_fkey: no advisor_id" in item for item in wrong), wrong
    assert any("constraint_probe_slot_excl: no advisor_id" in item for item in wrong), wrong


def test_every_table_has_a_required_advisor_id(app: Connection) -> None:
    found = app.execute(
        "SELECT n.nspname || '.' || c.relname, a.attnotnull, a.atttypid::regtype::text"
        " FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
        " JOIN pg_attribute a ON a.attrelid = c.oid AND a.attname = 'advisor_id'"
        " WHERE n.nspname || '.' || c.relname = ANY(%s) ORDER BY 1",
        (sorted(TABLES),),
    ).fetchall()
    assert found == [(table, True, "text") for table in sorted(TABLES)], found
