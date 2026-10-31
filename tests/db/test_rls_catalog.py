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


def test_grants_on_tables_are_select_only(app: Connection) -> None:
    rows = app.execute(
        "SELECT n.nspname || '.' || c.relname, c.relkind, c.relacl::text[]"
        " FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
        " WHERE n.nspname = ANY(%s) AND c.relkind IN ('r', 'p', 'v', 'm', 'f', 'S')"
        " ORDER BY 1",
        ([names.SCHEMA, names.META_SCHEMA],),
    ).fetchall()
    tables = {name for name, kind, _ in rows if name.startswith(f"{names.SCHEMA}.")}
    assert tables == TABLES, f"relations in {names.SCHEMA}: {sorted(tables)}"
    wrong = []
    for name, _, acl in rows:
        if name in TABLES and set(acl or []) != TABLE_ACL:
            wrong.append(f"{name}: relacl {acl}, expected {sorted(TABLE_ACL)}")
        held = [
            privilege
            for privilege in TABLE_PRIVILEGES
            if app.execute(
                "SELECT has_table_privilege(c.oid, %s) FROM pg_class c"
                " JOIN pg_namespace n ON n.oid = c.relnamespace"
                " WHERE n.nspname || '.' || c.relname = %s",
                (privilege, name),
            ).fetchone()[0]
        ]
        expected = ["SELECT"] if name in TABLES else []
        if held != expected:
            wrong.append(f"{APP} holds {held} on {name}, expected {expected}")
    assert not wrong, "\n".join(wrong)
    columns = column(
        app,
        "SELECT c.relname || '.' || a.attname FROM pg_attribute a"
        " JOIN pg_class c ON c.oid = a.attrelid JOIN pg_namespace n ON n.oid = c.relnamespace"
        " WHERE n.nspname = ANY(%s) AND a.attacl IS NOT NULL",
        ([names.SCHEMA, names.META_SCHEMA],),
    )
    assert not columns, f"column-level grants: {columns}"


def test_no_sequence_is_usable(app: Connection) -> None:
    usable = column(
        app,
        "SELECT n.nspname || '.' || c.relname FROM pg_class c"
        " JOIN pg_namespace n ON n.oid = c.relnamespace"
        " WHERE c.relkind = 'S' AND n.nspname NOT LIKE %s AND n.nspname <> 'information_schema'"
        " AND (has_sequence_privilege(c.oid, 'USAGE') OR has_sequence_privilege(c.oid, 'SELECT')"
        " OR has_sequence_privilege(c.oid, 'UPDATE'))",
        (SYSTEM,),
    )
    assert not usable, f"{APP} may use the sequences {usable}"


def test_functions_and_types_are_not_granted(app: Connection) -> None:
    # sec-6 of the PR 5a review: 0001 revokes PUBLIC's default EXECUTE and USAGE for the
    # owner's functions and types. The default does not reach a table's row type, which
    # PUBLIC may use and REVOKE cannot change; it lends no access to rows. Every other
    # type in the schemas stays closed to the app role.
    schemas = [names.SCHEMA, names.META_SCHEMA]
    functions = column(
        app,
        "SELECT p.oid::regprocedure::text FROM pg_proc p"
        " JOIN pg_namespace n ON n.oid = p.pronamespace"
        " WHERE n.nspname = ANY(%s) AND has_function_privilege(p.oid, 'EXECUTE')",
        (schemas,),
    )
    assert not functions, f"{APP} may execute {functions}"
    types = set(
        column(
            app,
            "SELECT t.oid::regtype::text FROM pg_type t"
            " JOIN pg_namespace n ON n.oid = t.typnamespace"
            " WHERE n.nspname = ANY(%s) AND has_type_privilege(t.oid, 'USAGE')",
            (schemas,),
        )
    )
    row_types = {table.rpartition(".")[2] for table in TABLES} | {RECORD}
    row_types |= {f"{name}[]" for name in row_types}
    assert types == row_types, f"{APP} may use the types {sorted(types)}"


def test_control_a_granted_function_is_seen(superuser: Connection) -> None:
    function = f"{names.SCHEMA}.grant_probe()"
    with superuser.transaction(force_rollback=True):
        superuser.execute(f"SET LOCAL ROLE {OWNER}")
        superuser.execute(f"CREATE FUNCTION {function} RETURNS int LANGUAGE sql AS 'SELECT 1'")
        superuser.execute(f"GRANT EXECUTE ON FUNCTION {function} TO {APP}")
        superuser.execute(f"SET LOCAL ROLE {APP}")
        superuser.execute(f"SET LOCAL search_path = {names.SCHEMA}, public")
        found = column(
            superuser,
            "SELECT p.oid::regprocedure::text FROM pg_proc p"
            " JOIN pg_namespace n ON n.oid = p.pronamespace"
            " WHERE n.nspname = %s AND has_function_privilege(p.oid, 'EXECUTE')",
            (names.SCHEMA,),
        )
    assert found == ["grant_probe()"], found


def test_no_security_definer_function(app: Connection) -> None:
    found = column(
        app,
        "SELECT p.oid::regprocedure::text FROM pg_proc p"
        " JOIN pg_namespace n ON n.oid = p.pronamespace WHERE p.prosecdef"
        " AND n.nspname NOT LIKE %s AND n.nspname <> 'information_schema'",
        (SYSTEM,),
    )
    assert not found, f"SECURITY DEFINER functions: {found}"
    # Non-vacuity: the query sees functions outside the system schemas (pgvector's).
    total = app.execute(
        "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
        " WHERE n.nspname NOT LIKE %s AND n.nspname <> 'information_schema'",
        (SYSTEM,),
    ).fetchone()[0]
    assert total > 0


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


def test_every_table_has_a_required_advisor_id(app: Connection) -> None:
    found = app.execute(
        "SELECT n.nspname || '.' || c.relname, a.attnotnull, a.atttypid::regtype::text"
        " FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
        " JOIN pg_attribute a ON a.attrelid = c.oid AND a.attname = 'advisor_id'"
        " WHERE n.nspname || '.' || c.relname = ANY(%s) ORDER BY 1",
        (sorted(TABLES),),
    ).fetchall()
    assert found == [(table, True, "text") for table in sorted(TABLES)], found
