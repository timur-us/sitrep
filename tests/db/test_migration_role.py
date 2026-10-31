"""Migrations run as the migrator, acting as the owner only through SET ROLE.

The three roles carry exactly the attributes db/init/00-roles.sql writes out, and the
migrator holds the owner WITH INHERIT FALSE: without SET ROLE it can create nothing.
The runner refuses the app role (42501) and the superuser, applies nothing twice, and
stops before any DDL when a recorded file changed or went missing. The app role can
create a table nowhere and cannot switch to another role.
"""

from __future__ import annotations

import hashlib
import re
import shutil
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from dbtools import RECORD, SUPERUSER, SUPERUSER_URL, Connection, applied, column
from psycopg import sql

from app.config.settings import Settings, loopback_database_url
from app.db import migrate, names
from app.db.connect import connect

pytestmark = pytest.mark.usefixtures("database")

PROBE = f"{names.SCHEMA}.migration_probe"
PROBE_ID = sql.Identifier(names.SCHEMA, "migration_probe")
# The planted files take the numbers after the real migrations.
NEXT = f"{len(migrate.load(migrate.MIGRATIONS)) + 1:04d}"
AFTER_NEXT = f"{int(NEXT) + 1:04d}"

# rolsuper, rolinherit, rolcreaterole, rolcreatedb, rolcanlogin, rolreplication, rolbypassrls
ROLES = {
    names.OWNER_ROLE: (False, False, False, False, False, False, False),
    names.MIGRATOR_ROLE: (False, True, False, False, True, False, False),
    names.APP_ROLE: (False, True, False, False, True, False, False),
}


def test_migrations_ran_as_migrator_under_owner(superuser: Connection) -> None:
    records = applied(superuser)
    on_disk = {m.version: m for m in migrate.load(migrate.MIGRATIONS)}
    assert [row[0] for row in records] == sorted(on_disk), f"applied {records}"
    assert "0001" in on_disk, "no migration on disk"
    wrong = [
        f"{file_name}: session_user {session}, current_user {current}"
        for _, file_name, _, session, current in records
        if (session, current) != (names.MIGRATOR_ROLE, names.OWNER_ROLE)
    ]
    assert not wrong, (
        f"expected session_user {names.MIGRATOR_ROLE}, current_user {names.OWNER_ROLE}:\n"
        + "\n".join(wrong)
    )
    for version, file_name, sha256, _, _ in records:
        assert (file_name, sha256) == (on_disk[version].file_name, on_disk[version].sha256)


def test_role_layout(superuser: Connection) -> None:
    rows = superuser.execute(
        "SELECT rolname, rolsuper, rolinherit, rolcreaterole, rolcreatedb, rolcanlogin,"
        " rolreplication, rolbypassrls FROM pg_roles WHERE rolname = ANY(%s)",
        (list(ROLES),),
    ).fetchall()
    found = {name: tuple(flags) for name, *flags in rows}
    assert found == ROLES, f"role attributes {found}"
    memberships = superuser.execute(
        "SELECT g.rolname, m.rolname, a.inherit_option, a.set_option, a.admin_option"
        " FROM pg_auth_members a"
        " JOIN pg_roles g ON g.oid = a.roleid JOIN pg_roles m ON m.oid = a.member"
        " WHERE g.rolname = ANY(%(roles)s) OR m.rolname = ANY(%(roles)s)",
        {"roles": list(ROLES)},
    ).fetchall()
    expected = [(names.OWNER_ROLE, names.MIGRATOR_ROLE, False, True, False)]
    assert memberships == expected, (
        f"memberships (role, member, inherit, set, admin) {memberships}, expected {expected}"
    )


def test_migrator_without_set_role_creates_nothing(settings: Settings) -> None:
    with connect(settings.migration_database_url, autocommit=True) as conn:
        assert conn.execute("SELECT current_user").fetchone() == (names.MIGRATOR_ROLE,)
        for statement in (
            f"CREATE TABLE {PROBE} (id int)",
            "CREATE SCHEMA migration_probe",
            "CREATE TEMPORARY TABLE migration_probe (id int)",
        ):
            with (
                pytest.raises(psycopg.errors.InsufficientPrivilege),
                conn.transaction(force_rollback=True),
            ):
                conn.execute(statement)


@pytest.fixture
def probe_cleanup(superuser: Connection) -> Iterator[None]:
    # Only a broken runner leaves these behind; the tests then fail, and the next run is clean.
    yield
    superuser.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(PROBE_ID))
    real = [m.version for m in migrate.load(migrate.MIGRATIONS)]
    superuser.execute(sql.SQL("DELETE FROM {} WHERE NOT version = ANY(%s)").format(RECORD), (real,))


def _copy_with(tmp_path: Path, **files: str) -> Path:
    """The real migrations plus the given files, in a fresh directory."""
    directory = tmp_path / "migrations"
    shutil.copytree(migrate.MIGRATIONS, directory)
    for name, text in files.items():
        (directory / name).write_text(text)
    return directory


def _copy_with_probe(tmp_path: Path) -> Path:
    return _copy_with(tmp_path, **{f"{NEXT}_probe.sql": f"CREATE TABLE {PROBE} (id int);\n"})


def _probe_absent(superuser: Connection) -> bool:
    return column(superuser, "SELECT to_regclass(%s)", (PROBE,)) == [None]


@pytest.mark.usefixtures("probe_cleanup")
def test_migrate_refuses_the_app_role(
    tmp_path: Path, settings: Settings, superuser: Connection
) -> None:
    # A migration is pending, so a runner that did not refuse would leave a trace.
    before = applied(superuser)
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="permission denied to set role"):
        migrate.run(settings.database_url, _copy_with_probe(tmp_path))
    assert _probe_absent(superuser), f"{NEXT} ran as the app role"
    assert applied(superuser) == before


@pytest.mark.usefixtures("probe_cleanup")
def test_migrate_refuses_the_superuser(tmp_path: Path, superuser: Connection) -> None:
    before = applied(superuser)
    with pytest.raises(migrate.MigrationError, match=f"are \\('{SUPERUSER}', "):
        migrate.run(SUPERUSER_URL, _copy_with_probe(tmp_path))
    assert _probe_absent(superuser), f"{NEXT} ran as the superuser"
    assert applied(superuser) == before


@pytest.mark.parametrize(
    ("url", "rule"),
    [
        (loopback_database_url(names.APP_ROLE), "must log in as"),
        (loopback_database_url(names.MIGRATOR_ROLE) + "?options=-cx", "['options']"),
        (
            loopback_database_url(names.MIGRATOR_ROLE).replace("@", ":pw0rdCanary@", 1),
            "['password']",
        ),
    ],
)
def test_entry_point_refuses_a_bad_migration_url(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], url: str, rule: str
) -> None:
    monkeypatch.setenv("MIGRATION_DATABASE_URL", url)
    assert migrate.main([]) == 1
    err = capsys.readouterr().err
    assert err.startswith("migrate: refused:") and rule in err, err
    assert "pw0rdCanary" not in err


def test_second_run_applies_nothing(settings: Settings, superuser: Connection) -> None:
    before = applied(superuser)
    assert migrate.run(settings.migration_database_url) == []
    assert applied(superuser) == before


@pytest.mark.usefixtures("probe_cleanup")
def test_changed_applied_file_stops_the_run(
    tmp_path: Path, superuser: Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = _copy_with_probe(tmp_path)
    first = directory / "0001_schema.sql"
    first.write_text(first.read_text() + "-- changed\n")
    assert hashlib.sha256(first.read_bytes()).hexdigest() != applied(superuser)[0][2]
    before = applied(superuser)
    assert migrate.main(["--migrations", str(directory)]) == 1
    assert "0001_schema.sql changed after it was applied" in capsys.readouterr().err
    assert _probe_absent(superuser), f"{NEXT} ran"
    assert applied(superuser) == before


@pytest.mark.usefixtures("probe_cleanup")
def test_missing_applied_file_stops_the_run(
    tmp_path: Path, superuser: Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = _copy_with_probe(tmp_path)
    (directory / "0001_schema.sql").rename(directory / "0001_renamed.sql")
    before = applied(superuser)
    assert migrate.main(["--migrations", str(directory)]) == 1
    assert "0001_schema.sql was applied and is no longer on disk" in capsys.readouterr().err
    assert _probe_absent(superuser), f"{NEXT} ran"
    assert applied(superuser) == before


@pytest.mark.usefixtures("probe_cleanup")
def test_a_failing_file_rolls_back_the_whole_run(
    tmp_path: Path, superuser: Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = _copy_with(
        tmp_path,
        **{
            f"{NEXT}_probe.sql": f"CREATE TABLE {PROBE} (id int);\n",
            f"{AFTER_NEXT}_fails.sql": "SELECT 1/0;\n",
        },
    )
    before = applied(superuser)
    assert migrate.main(["--migrations", str(directory)]) == 1
    assert "DivisionByZero" in capsys.readouterr().err
    assert _probe_absent(superuser), f"{NEXT} stayed although {AFTER_NEXT} failed"
    assert applied(superuser) == before


# A file that leaves the runner's transaction, its role or its search_path is caught after
# it ran. What it committed on its own stays (detected, not prevented); it is not recorded.
ESCAPES = [
    (f"CREATE TABLE {PROBE} (id int); COMMIT;", "ended the runner's transaction"),
    (f"CREATE TABLE {PROBE} (id int); ROLLBACK; SELECT 1;", "ended the runner's transaction"),
    ("RESET ROLE;", "changed session_user, current_user"),
    ("SET search_path = public;", "changed the search_path"),
]


@pytest.mark.usefixtures("probe_cleanup")
@pytest.mark.parametrize(("text", "message"), ESCAPES)
def test_a_file_that_escapes_the_run_is_refused(
    tmp_path: Path,
    superuser: Connection,
    capsys: pytest.CaptureFixture[str],
    text: str,
    message: str,
) -> None:
    directory = _copy_with(tmp_path, **{f"{NEXT}_escape.sql": text + "\n"})
    before = applied(superuser)
    assert migrate.main(["--migrations", str(directory)]) == 1
    err = capsys.readouterr().err
    assert f"{NEXT}_escape.sql {message}" in err, err
    assert applied(superuser) == before, "the escaping file was recorded"


@pytest.mark.parametrize(
    ("names_", "rule"),
    [
        (["0001_a.sql", "0003_c.sql"], "expected ['0001', '0002']"),
        (["0001_a.sql", "0002_B.sql"], "0002_B.sql: not a NNNN_name.sql file"),
        (["0001_a.sql", "0002_b.txt"], "0002_b.txt: not a NNNN_name.sql file"),
        (["0001_a.sql", "0001_b.sql"], "expected ['0001', '0002']"),
    ],
)
def test_load_refuses_a_bad_directory(tmp_path: Path, names_: list[str], rule: str) -> None:
    for name in names_:
        (tmp_path / name).write_text("SELECT 1;\n")
    with pytest.raises(migrate.MigrationError, match=re.escape(rule)):
        migrate.load(tmp_path)


def test_load_refuses_a_symlink(tmp_path: Path) -> None:
    (tmp_path / "elsewhere.sql").write_text("SELECT 1;\n")
    directory = tmp_path / "migrations"
    directory.mkdir()
    (directory / "0001_a.sql").symlink_to(tmp_path / "elsewhere.sql")
    with pytest.raises(migrate.MigrationError, match="0001_a.sql: not a NNNN_name.sql file"):
        migrate.load(directory)


PROBES = [
    "CREATE TABLE probe (id int)",  # unqualified: the search_path
    "CREATE TEMPORARY TABLE probe (id int)",
    "CREATE SCHEMA probe",
    f"CREATE FUNCTION {names.SCHEMA}.probe() RETURNS int LANGUAGE sql AS 'SELECT 1'",
    "CREATE ROLE probe",
    f"ALTER ROLE {names.APP_ROLE} SUPERUSER",
    f"ALTER ROLE {names.APP_ROLE} BYPASSRLS",
    f"GRANT {names.OWNER_ROLE} TO {names.APP_ROLE}",
    "SET SESSION AUTHORIZATION postgres",
    "SELECT lo_create(0)",
    "SELECT lo_creat(-1)",
    "SELECT lo_from_bytea(0, 'x')",
]


def test_app_role_cannot_create_a_table_or_switch_role(settings: Settings) -> None:
    with connect(settings.database_url, autocommit=True) as app:
        schemas = column(app, "SELECT nspname FROM pg_namespace ORDER BY nspname")
        assert {"public", names.SCHEMA, names.META_SCHEMA, "pg_catalog"} <= set(schemas)
        statements = [
            sql.SQL("CREATE TABLE {} (id int)").format(sql.Identifier(schema, "probe"))
            for schema in schemas
        ]
        statements += [sql.SQL(probe) for probe in PROBES]
        statements += [
            sql.SQL("SET ROLE {}").format(sql.Identifier(role))
            for role in (names.OWNER_ROLE, names.MIGRATOR_ROLE, SUPERUSER)
        ]
        allowed = []
        for statement in statements:
            # Rolled back either way: a probe that succeeds leaves nothing behind.
            try:
                with app.transaction(force_rollback=True):
                    app.execute(statement)
            except psycopg.errors.InsufficientPrivilege:
                continue
            allowed.append(statement.as_string(app))
        assert not allowed, f"{names.APP_ROLE} may run: {allowed}"
        # CREATE DATABASE cannot run inside a transaction.
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            app.execute("CREATE DATABASE probe")


@pytest.mark.parametrize("database", ["postgres", "template1"])
@pytest.mark.parametrize("role", [names.APP_ROLE, names.MIGRATOR_ROLE])
def test_login_roles_connect_to_their_database_only(role: str, database: str) -> None:
    url = loopback_database_url(role).rsplit("/", 1)[0] + "/" + database
    with pytest.raises(psycopg.OperationalError, match="permission denied for database"):
        connect(url).close()


def test_the_owner_cannot_log_in() -> None:
    with pytest.raises(psycopg.OperationalError, match="not permitted to log in"):
        connect(loopback_database_url(names.OWNER_ROLE)).close()


def test_role_settings_are_exactly_the_search_path(superuser: Connection) -> None:
    # A role may change its own defaults (ALTER ROLE ... SET), which no REVOKE stops;
    # app/db/session.py's guard checks them at every checkout (tests/db/test_pool.py).
    # Here: what db/init set, and nothing else.
    rows = superuser.execute(
        "SELECT r.rolname, d.datname, s.setconfig FROM pg_db_role_setting s"
        " JOIN pg_roles r ON r.oid = s.setrole LEFT JOIN pg_database d ON d.oid = s.setdatabase"
        " WHERE r.rolname = ANY(%s) ORDER BY r.rolname",
        ([names.APP_ROLE, names.MIGRATOR_ROLE, names.OWNER_ROLE],),
    ).fetchall()
    path = [f"search_path={names.SCHEMA}, public"]
    assert rows == [
        (names.APP_ROLE, names.DATABASE, path),
        (names.MIGRATOR_ROLE, names.DATABASE, path),
    ], rows


def test_objects_the_owner_creates_are_not_public(superuser: Connection) -> None:
    # PostgreSQL grants EXECUTE on new functions and USAGE on new types to PUBLIC by
    # default; 0001 revokes both for the owner's future objects. Rolled back.
    function = f"{names.SCHEMA}.default_acl_probe()"
    with superuser.transaction(force_rollback=True):
        superuser.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(names.OWNER_ROLE)))
        superuser.execute(f"CREATE FUNCTION {function} RETURNS int LANGUAGE sql AS 'SELECT 1'")
        superuser.execute(f"CREATE TYPE {names.SCHEMA}.default_acl_probe AS (a int)")
        granted = superuser.execute(
            "SELECT has_function_privilege(%s, %s, 'EXECUTE'), has_type_privilege(%s, %s, 'USAGE')",
            (names.APP_ROLE, function, names.APP_ROLE, f"{names.SCHEMA}.default_acl_probe"),
        ).fetchone()
    assert granted == (False, False), f"(EXECUTE, USAGE) for {names.APP_ROLE}: {granted}"
