"""The advisor boundary in the catalog (plan condition 1, FORCE half; threat-model path 2).

Discovery reads pg_catalog as the app role, over every schema that is not a system
schema: every relation it can read is a table with row-level security enabled and
forced, or a security_invoker view; materialized views and foreign tables are refused.
The readable set is exactly the expected tables. Each advisor-scoped table has exactly
one policy of the canonical shape; product_sheets, the one global table (ADR 0010),
has exactly its two, pinned as PostgreSQL deparses them. No policy anywhere admits
advisor_id IS NULL. The grants are exact: SELECT only on the tables, nothing on
functions or sequences, USAGE on no type but the tables' own row types. No SECURITY
DEFINER function exists outside the system schemas. Every unique index, primary keys
included, contains advisor_id, which closes the referential-integrity channel: a key
that exists for another advisor cannot be probed through a unique or foreign-key
violation. product_sheets is exempt from the advisor_id rules by its name alone, holds
no advisor_id, and no foreign key leads into or out of it.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from dbtools import Connection, column
from seed_rows import ADVISOR_TABLES, GLOBAL_TABLES, SHEETS, TABLES, readable

from app.config.settings import Settings
from app.db import names
from app.db.connect import connect
from app.db.migrate import RECORD

pytestmark = pytest.mark.usefixtures("database")

APP = names.APP_ROLE
OWNER = names.OWNER_ROLE
# How PostgreSQL deparses the one policy every advisor-scoped table carries (USING and
# WITH CHECK).
CANONICAL = "(advisor_id = NULLIF(current_setting('app.advisor_id'::text, true), ''::text))"
# product_sheets' two policies (ADR 0010), as (name, command, permissive, roles, USING,
# WITH CHECK): the app role reads every sheet inside an advisor transaction, and the
# owner, which FORCE binds, writes them.
SHEET_POLICIES = [
    (
        "advisor_read",
        "r",
        True,
        [APP],
        "(NULLIF(current_setting('app.advisor_id'::text, true), ''::text) IS NOT NULL)",
        None,
    ),
    ("owner_write", "*", True, [OWNER], "true", "true"),
]
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
        (sorted(ADVISOR_TABLES),),
    ).fetchall()
    policies: dict[str, list[tuple]] = {}
    for table, name, *shape in rows:
        policies.setdefault(table, [])
        if name is not None:
            policies[table].append(tuple(shape))
    assert set(policies) == ADVISOR_TABLES, sorted(policies)
    canonical = ("*", True, True, CANONICAL, CANONICAL)
    wrong = [
        f"{table}: {len(found)} policies {found}, expected one {canonical}"
        for table, found in sorted(policies.items())
        if found != [canonical]
    ]
    assert not wrong, "\n".join(wrong)


POLICIES_OF = (
    "SELECT p.polname, p.polcmd, p.polpermissive,"
    " ARRAY(SELECT r.rolname::text FROM pg_roles r WHERE r.oid = ANY(p.polroles)"
    "  ORDER BY r.rolname),"
    " pg_get_expr(p.polqual, p.polrelid), pg_get_expr(p.polwithcheck, p.polrelid)"
    " FROM pg_policy p WHERE p.polrelid = %s::regclass ORDER BY 1"
)


def test_product_sheets_have_exactly_their_policies(app: Connection) -> None:
    assert GLOBAL_TABLES == {f"{names.SCHEMA}.product_sheets"}, GLOBAL_TABLES
    found = [tuple(row) for row in app.execute(POLICIES_OF, (SHEETS,)).fetchall()]
    assert found == [tuple(policy) for policy in SHEET_POLICIES], found


# Every policy outside the system schemas, both expressions: none may let a row without
# an advisor through (ADR 0010; the bypass PR 8 sec-8 forbids).
ALL_POLICIES = (
    "SELECT n.nspname || '.' || c.relname || ' ' || p.polname,"
    " coalesce(pg_get_expr(p.polqual, p.polrelid), '') || ' '"
    " || coalesce(pg_get_expr(p.polwithcheck, p.polrelid), '')"
    " FROM pg_policy p JOIN pg_class c ON c.oid = p.polrelid"
    " JOIN pg_namespace n ON n.oid = c.relnamespace"
    " WHERE n.nspname NOT LIKE %s AND n.nspname <> 'information_schema' ORDER BY 1"
)
NULL_ADVISOR = re.compile(r"advisor_id\)?\s+IS\s+NULL", re.IGNORECASE)


def null_advisor_policies(rows: list[tuple]) -> list[str]:
    return [name for name, text in rows if NULL_ADVISOR.search(text)]


def test_no_policy_admits_a_null_advisor(app: Connection) -> None:
    rows = app.execute(ALL_POLICIES, (SYSTEM,)).fetchall()
    # Non-vacuity: every table's policies are read, the sheets' two among them.
    assert len(rows) == len(ADVISOR_TABLES) + len(SHEET_POLICIES), rows
    found = null_advisor_policies(rows)
    assert not found, f"policies that admit advisor_id IS NULL: {found}"


def test_control_a_null_advisor_policy_is_seen(superuser: Connection) -> None:
    mandates = f"{names.SCHEMA}.mandates"
    with planted(
        superuser,
        f"CREATE POLICY null_probe ON {mandates} FOR SELECT USING (advisor_id IS NULL)",
        f"CREATE POLICY null_probe_2 ON {mandates} FOR SELECT USING ((advisor_id) is  null)",
    ) as conn:
        found = null_advisor_policies(conn.execute(ALL_POLICIES, (SYSTEM,)).fetchall())
    assert found == [f"{mandates} null_probe", f"{mandates} null_probe_2"], found


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
    "SELECT n.nspname || '.' || c.relname, i.indexrelid::regclass::text, i.indisprimary,"
    " ARRAY(SELECT a.attname FROM unnest(i.indkey) AS k(attnum)"
    "  JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum)"
    " FROM pg_index i JOIN pg_class c ON c.oid = i.indrelid"
    " JOIN pg_namespace n ON n.oid = c.relnamespace"
    " WHERE n.nspname = %s AND i.indisunique ORDER BY 2"
)


def unique_index_problems(rows: list[tuple]) -> list[str]:
    """Unique indexes without advisor_id, outside the global table, which is exempt by name."""
    return [
        f"unique index without advisor_id: {name} {columns}"
        for table, name, _, columns in rows
        if table not in GLOBAL_TABLES and "advisor_id" not in columns
    ]


def test_every_unique_index_contains_advisor_id(app: Connection) -> None:
    indexes = app.execute(UNIQUE_INDEXES, (names.SCHEMA,)).fetchall()
    primaries = [name for _, name, primary, _ in indexes if primary]
    assert len(primaries) == len(TABLES), f"primary keys: {primaries}"
    assert len(indexes) > len(primaries), f"no unique index besides the keys: {indexes}"
    wrong = unique_index_problems(indexes)
    assert not wrong, "\n".join(wrong)
    # The exemption is used: the sheets' keys, id and url, hold no advisor_id.
    sheets = sorted((primary, columns) for table, _, primary, columns in indexes if table == SHEETS)
    assert sheets == [(False, ["url"]), (True, ["id"])], sheets


def test_planted_unique_indexes_are_checked() -> None:
    # The exemption is the global table's name, and no other: a table that merely looks
    # global is reported.
    other = f"{names.SCHEMA}.lookalike"
    rows = [(SHEETS, "s_pkey", True, ["id"]), (other, "l_pkey", True, ["id"])]
    assert unique_index_problems(rows) == ["unique index without advisor_id: l_pkey ['id']"]
    assert unique_index_problems([(other, "l_pkey", True, ["advisor_id", "id"])]) == []


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
    # Every advisor-scoped table but advisors has one; the global table has none.
    assert len(foreign) == len(ADVISOR_TABLES) - 1, f"foreign keys: {rows}"
    wrong = constraint_problems(rows)
    assert not wrong, "\n".join(wrong)


def test_no_foreign_key_touches_the_global_table(app: Connection) -> None:
    # ADR 0010 (PR 1 review sec-2): no key leads into or out of product_sheets, so no
    # advisor's row hangs off a global one, nor the other way round.
    found = column(
        app,
        "SELECT conname FROM pg_constraint WHERE contype = 'f'"
        " AND %s::regclass IN (conrelid, confrelid)",
        (SHEETS,),
    )
    assert found == [], found


def test_mandate_contacts_keys(app: Connection) -> None:
    # Recipients per mandate (PR 8 sec-2): advisor_id leads every key.
    contacts = f"{names.SCHEMA}.mandate_contacts"
    rows = app.execute(
        "SELECT c.contype,"
        " ARRAY(SELECT a.attname FROM unnest(c.conkey) WITH ORDINALITY AS k(attnum, place)"
        "  JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum"
        "  ORDER BY k.place),"
        " c.confrelid::regclass::text"
        " FROM pg_constraint c WHERE c.conrelid = %s::regclass AND c.contype IN ('p', 'u', 'f')"
        " ORDER BY 1",
        (contacts,),
    ).fetchall()
    assert [(kind, columns) for kind, columns, _ in rows] == [
        ("f", ["advisor_id", "mandate_id"]),
        ("p", ["advisor_id", "mandate_id", "email"]),
    ], rows
    assert rows[0][2].rpartition(".")[2] == "mandates", rows


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
        " AND NOT a.attisdropped"
        " WHERE n.nspname || '.' || c.relname = ANY(%s) ORDER BY 1",
        (sorted(TABLES),),
    ).fetchall()
    # Every table but the global one, which has no advisor_id at all (ADR 0010).
    assert found == [(table, True, "text") for table in sorted(ADVISOR_TABLES)], found
