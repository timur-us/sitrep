"""The app role, connected through settings' DATABASE_URL (plan condition 1, role half).

It is exactly the app role, with no attribute and no membership; it owns nothing in any
database; it can create nothing; it cannot reach the runner's schema or another
database; and everything in the app schema and the meta schema belongs to the owner.
Row-level security on the tables (the FORCE half) is PR 5b.
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


def test_app_role_attributes(app: Connection) -> None:
    users = app.execute("SELECT current_user, session_user").fetchone()
    assert users == (APP, APP), f"DATABASE_URL logs in as {users}, expected {APP}"
    flags = app.execute(
        "SELECT rolsuper, rolbypassrls, rolcreaterole, rolcreatedb, rolreplication"
        " FROM pg_roles WHERE rolname = current_user"
    ).fetchone()
    held = [name for name, on in zip(ATTRIBUTES, flags, strict=True) if on]
    assert not held, f"{APP} has {held}"
    memberships = app.execute(
        "SELECT g.rolname, m.rolname FROM pg_auth_members a"
        " JOIN pg_roles g ON g.oid = a.roleid JOIN pg_roles m ON m.oid = a.member"
        " WHERE current_user IN (g.rolname, m.rolname)"
    ).fetchall()
    assert not memberships, f"{APP} in pg_auth_members (role, member): {memberships}"


def test_app_role_owns_nothing(app: Connection) -> None:
    owned = column(
        app, "SELECT 'database ' || datname FROM pg_database WHERE datdba = current_user::regrole"
    )
    owned += column(
        app, "SELECT 'schema ' || nspname FROM pg_namespace WHERE nspowner = current_user::regrole"
    )
    owned += column(
        app,
        "SELECT 'relation ' || c.oid::regclass::text FROM pg_class c"
        " WHERE c.relowner = current_user::regrole",
    )
    # Every other owned object, in this database and in the shared catalogs.
    owned += column(
        app,
        "SELECT 'object ' || classid::regclass::text || ' ' || objid FROM pg_shdepend"
        " WHERE refobjid = current_user::regrole AND deptype = 'o'",
    )
    assert not owned, f"{APP} owns: {owned}"


def test_app_role_cannot_create(app: Connection) -> None:
    schemas = column(app, "SELECT nspname FROM pg_namespace ORDER BY nspname")
    assert {"public", names.SCHEMA, names.META_SCHEMA} <= set(schemas), schemas
    creatable = column(
        app, "SELECT nspname FROM pg_namespace WHERE has_schema_privilege(oid, 'CREATE')"
    )
    assert not creatable, f"{APP} has CREATE on schema {creatable}"
    on_database = [
        privilege
        for privilege in ("CREATE", "TEMPORARY")
        if app.execute(
            "SELECT has_database_privilege(current_database(), %s)", (privilege,)
        ).fetchone()[0]
    ]
    assert not on_database, f"{APP} has {on_database} on database {names.DATABASE}"


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


def test_app_role_connects_to_its_database_only(app: Connection, settings: Settings) -> None:
    others = column(
        app,
        "SELECT datname FROM pg_database WHERE datallowconn"
        " AND has_database_privilege(datname, 'CONNECT') AND datname <> current_database()",
    )
    assert not others, f"{APP} may connect to {others}"
    # The control: the catalog's answer is what the server enforces.
    elsewhere = settings.database_url.rsplit("/", 1)[0] + "/postgres"
    with pytest.raises(psycopg.OperationalError, match="permission denied for database"):
        connect(elsewhere).close()
