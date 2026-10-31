"""The app role, connected through settings' DATABASE_URL (plan condition 1, role half).

It is exactly the app role, with no attribute and no membership; it owns nothing in any
database; it can create nothing; it cannot reach the runner's schema or another
database; and everything in the app schema and the meta schema belongs to the owner.
Each catalog check is a function of the role, so a control runs the same query for a
role that does hold what is checked. Row-level security (the FORCE half) is PR 5b.
"""

from __future__ import annotations

from collections.abc import Iterator

import psycopg
import pytest
from dbtools import RECORD as RECORD_ID
from dbtools import Connection, column
from psycopg import sql

from app.config.settings import Settings
from app.db import names
from app.db.connect import connect
from app.db.migrate import RECORD

pytestmark = pytest.mark.usefixtures("database")

APP = names.APP_ROLE
ATTRIBUTES = ("rolsuper", "rolbypassrls", "rolcreaterole", "rolcreatedb", "rolreplication")
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


@pytest.fixture
def app(settings: Settings) -> Iterator[Connection]:
    with connect(settings.database_url, autocommit=True) as conn:
        yield conn


def role_problems(conn: Connection, role: str) -> list[str]:
    flags = conn.execute(
        "SELECT rolsuper, rolbypassrls, rolcreaterole, rolcreatedb, rolreplication"
        " FROM pg_roles WHERE rolname = %s",
        (role,),
    ).fetchone()
    problems = [f"{role} has {name}" for name, on in zip(ATTRIBUTES, flags, strict=True) if on]
    memberships = conn.execute(
        "SELECT g.rolname, m.rolname FROM pg_auth_members a"
        " JOIN pg_roles g ON g.oid = a.roleid JOIN pg_roles m ON m.oid = a.member"
        " WHERE %s IN (g.rolname, m.rolname)",
        (role,),
    ).fetchall()
    problems += [f"{role} in pg_auth_members (role, member): {row}" for row in memberships]
    return problems


def owned_by(conn: Connection, role: str) -> list[str]:
    params = {"role": role}
    owned = column(
        conn,
        "SELECT 'database ' || datname FROM pg_database WHERE datdba = %(role)s::regrole",
        params,
    )
    owned += column(
        conn,
        "SELECT 'schema ' || nspname FROM pg_namespace WHERE nspowner = %(role)s::regrole",
        params,
    )
    owned += column(
        conn,
        "SELECT 'relation ' || c.oid::regclass::text FROM pg_class c"
        " WHERE c.relowner = %(role)s::regrole",
        params,
    )
    # Every other owned object, in this database and in the shared catalogs.
    owned += column(
        conn,
        "SELECT 'object ' || classid::regclass::text || ' ' || objid FROM pg_shdepend"
        " WHERE refobjid = %(role)s::regrole AND deptype = 'o'",
        params,
    )
    return owned


def creatable_by(conn: Connection, role: str) -> list[str]:
    found = column(
        conn,
        "SELECT 'schema ' || nspname FROM pg_namespace"
        " WHERE has_schema_privilege(%s, oid, 'CREATE')",
        (role,),
    )
    for privilege in ("CREATE", "TEMPORARY"):
        if conn.execute(
            "SELECT has_database_privilege(%s, current_database(), %s)", (role, privilege)
        ).fetchone()[0]:
            found.append(f"database {privilege}")
    return found


def test_app_role_attributes(app: Connection) -> None:
    users = app.execute("SELECT current_user, session_user").fetchone()
    assert users == (APP, APP), f"DATABASE_URL logs in as {users}, expected {APP}"
    problems = role_problems(app, APP)
    assert not problems, "\n".join(problems)


def test_control_attributes_and_memberships_are_seen(superuser: Connection) -> None:
    with superuser.transaction(force_rollback=True):
        superuser.execute("CREATE ROLE attribute_probe CREATEDB BYPASSRLS")
        superuser.execute(
            sql.SQL("GRANT {} TO attribute_probe").format(sql.Identifier(names.OWNER_ROLE))
        )
        problems = role_problems(superuser, "attribute_probe")
    assert problems == [
        "attribute_probe has rolbypassrls",
        "attribute_probe has rolcreatedb",
        f"attribute_probe in pg_auth_members (role, member): {(names.OWNER_ROLE, 'attribute_probe')}",
    ], problems


def test_app_role_owns_nothing(app: Connection) -> None:
    owned = owned_by(app, APP)
    assert not owned, f"{APP} owns: {owned}"


def test_control_ownership_is_seen(app: Connection) -> None:
    # The same queries, for the owner: a wrong join would come back empty here too.
    owned = set(owned_by(app, names.OWNER_ROLE))
    expected = {
        f"database {names.DATABASE}",
        f"schema {names.SCHEMA}",
        f"schema {names.META_SCHEMA}",
        f"relation {RECORD}",
    }
    assert expected <= owned, sorted(owned)
    assert any(item.startswith("object pg_database ") for item in owned), sorted(owned)


def test_app_role_cannot_create(app: Connection) -> None:
    schemas = column(app, "SELECT nspname FROM pg_namespace ORDER BY nspname")
    assert {"public", names.SCHEMA, names.META_SCHEMA} <= set(schemas), schemas
    creatable = creatable_by(app, APP)
    assert not creatable, f"{APP} may create in: {creatable}"


def test_control_create_rights_are_seen(app: Connection) -> None:
    found = set(creatable_by(app, names.OWNER_ROLE))
    expected = {f"schema {names.SCHEMA}", f"schema {names.META_SCHEMA}", "database CREATE"}
    assert expected <= found, sorted(found)


def test_meta_schema_is_closed_to_the_app(app: Connection) -> None:
    usage = app.execute("SELECT has_schema_privilege(%s, 'USAGE')", (names.META_SCHEMA,))
    assert usage.fetchone() == (False,), f"{APP} has USAGE on {names.META_SCHEMA}"
    held = [
        privilege
        for privilege in TABLE_PRIVILEGES
        # By oid: resolving the name would itself need USAGE on the schema.
        if app.execute(
            "SELECT has_table_privilege(c.oid, %s) FROM pg_class c"
            " JOIN pg_namespace n ON n.oid = c.relnamespace"
            " WHERE n.nspname = %s AND c.relname = 'schema_migrations'",
            (privilege, names.META_SCHEMA),
        ).fetchone()[0]
    ]
    assert not held, f"{APP} has {held} on {RECORD}"
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        app.execute(sql.SQL("SELECT count(*) FROM {}").format(RECORD_ID))


def test_everything_in_the_schemas_belongs_to_the_owner(app: Connection) -> None:
    owners = app.execute(
        "SELECT 'database ' || datname, pg_get_userbyid(datdba) FROM pg_database"
        " WHERE datname = %(database)s"
        " UNION ALL SELECT 'schema ' || nspname, pg_get_userbyid(nspowner) FROM pg_namespace"
        " WHERE nspname = ANY(%(schemas)s)"
        " UNION ALL SELECT 'relation ' || c.oid::regclass::text, pg_get_userbyid(c.relowner)"
        " FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
        " WHERE n.nspname = ANY(%(schemas)s)"
        " UNION ALL SELECT 'function ' || p.oid::regprocedure::text, pg_get_userbyid(p.proowner)"
        " FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
        " WHERE n.nspname = ANY(%(schemas)s)"
        " UNION ALL SELECT 'type ' || t.oid::regtype::text, pg_get_userbyid(t.typowner)"
        " FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace"
        " WHERE n.nspname = ANY(%(schemas)s)",
        {"database": names.DATABASE, "schemas": [names.SCHEMA, names.META_SCHEMA]},
    ).fetchall()
    found = dict(owners)
    # Non-vacuity: the database, both schemas and the runner's table were looked at.
    for expected in (
        f"database {names.DATABASE}",
        f"schema {names.SCHEMA}",
        f"schema {names.META_SCHEMA}",
        f"relation {RECORD}",
    ):
        assert expected in found, f"{expected} was not found"
    wrong = sorted(
        f"{name} is owned by {owner}" for name, owner in owners if owner != names.OWNER_ROLE
    )
    assert not wrong, "\n".join(wrong)


# (grantee, privilege) pairs from an ACL, with PUBLIC for grantee 0; NULL means the default.
DATABASE_ACL = (
    "SELECT coalesce(r.rolname, 'PUBLIC'), a.privilege_type FROM pg_database d,"
    " aclexplode(coalesce(d.datacl, acldefault('d', d.datdba))) a"
    " LEFT JOIN pg_roles r ON r.oid = a.grantee WHERE d.datname = %s"
)
SCHEMA_ACL = (
    "SELECT coalesce(r.rolname, 'PUBLIC'), a.privilege_type FROM pg_namespace n,"
    " aclexplode(coalesce(n.nspacl, acldefault('n', n.nspowner))) a"
    " LEFT JOIN pg_roles r ON r.oid = a.grantee WHERE n.nspname = %s"
)


def test_grants_are_exact(app: Connection) -> None:
    owner, migrator = names.OWNER_ROLE, names.MIGRATOR_ROLE
    expected = [
        (
            DATABASE_ACL,
            names.DATABASE,
            {
                (owner, "CREATE"),
                (owner, "TEMPORARY"),
                (owner, "CONNECT"),
                (APP, "CONNECT"),
                (migrator, "CONNECT"),
            },
        ),
        (SCHEMA_ACL, names.SCHEMA, {(owner, "USAGE"), (owner, "CREATE"), (APP, "USAGE")}),
        (SCHEMA_ACL, names.META_SCHEMA, {(owner, "USAGE"), (owner, "CREATE")}),
        (
            SCHEMA_ACL,
            "public",
            {("postgres", "USAGE"), ("postgres", "CREATE"), ("PUBLIC", "USAGE")},
        ),
    ]
    wrong = []
    for query, name, want in expected:
        got = {tuple(row) for row in app.execute(query, (name,)).fetchall()}
        if got != want:
            wrong.append(f"{name}: {sorted(got)}, expected {sorted(want)}")
    assert not wrong, "\n".join(wrong)
    public_owner = app.execute(
        "SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = 'public'"
    ).fetchone()
    assert public_owner == ("postgres",), f"public is owned by {public_owner}"
