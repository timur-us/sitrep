"""Migrations run as the migrator, acting as the owner only through SET ROLE.

The three roles carry exactly the attributes db/init/00-roles.sql writes out, and the
migrator holds the owner WITH INHERIT FALSE: without SET ROLE it can create nothing.
The runner refuses the app role (42501) and the superuser, applies nothing twice, and
stops before any DDL when a recorded file changed or went missing. The app role can
create a table nowhere and cannot switch to another role.
"""

from __future__ import annotations

import hashlib
import shutil
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from dbtools import RECORD, SUPERUSER, SUPERUSER_URL, Connection, applied, column
from psycopg import sql

from app.config.settings import Settings
from app.db import migrate, names
from app.db.connect import connect

pytestmark = pytest.mark.usefixtures("database")

PROBE = f"{names.SCHEMA}.migration_probe"
PROBE_ID = sql.Identifier(names.SCHEMA, "migration_probe")

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


def test_migrate_refuses_the_app_role(settings: Settings, superuser: Connection) -> None:
    before = applied(superuser)
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="permission denied to set role"):
        migrate.run(settings.database_url)
    assert applied(superuser) == before


def test_migrate_refuses_the_superuser(superuser: Connection) -> None:
    before = applied(superuser)
    with pytest.raises(migrate.MigrationError, match=f"are \\('{SUPERUSER}', "):
        migrate.run(SUPERUSER_URL)
    assert applied(superuser) == before


def test_entry_point_exits_non_zero_for_the_app_role(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("MIGRATION_DATABASE_URL", settings.database_url)
    assert migrate.main([]) == 1
    assert "InsufficientPrivilege (42501)" in capsys.readouterr().err


def test_second_run_applies_nothing(settings: Settings, superuser: Connection) -> None:
    before = applied(superuser)
    assert migrate.run(settings.migration_database_url) == []
    assert applied(superuser) == before


@pytest.fixture
def probe_cleanup(superuser: Connection) -> Iterator[None]:
    # Only a broken runner leaves these behind; the tests then fail, and the next run is clean.
    yield
    superuser.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(PROBE_ID))
    superuser.execute(sql.SQL("DELETE FROM {} WHERE file_name = '0002_probe.sql'").format(RECORD))


def _copy_with_probe(tmp_path: Path) -> Path:
    directory = tmp_path / "migrations"
    shutil.copytree(migrate.MIGRATIONS, directory)
    (directory / "0002_probe.sql").write_text(f"CREATE TABLE {PROBE} (id int);\n")
    return directory


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
    assert column(superuser, "SELECT to_regclass(%s)", (PROBE,)) == [None], "0002 ran"
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
    assert column(superuser, "SELECT to_regclass(%s)", (PROBE,)) == [None], "0002 ran"
    assert applied(superuser) == before


def test_app_role_cannot_create_a_table_or_switch_role(settings: Settings) -> None:
    with connect(settings.database_url, autocommit=True) as app:
        schemas = column(app, "SELECT nspname FROM pg_namespace ORDER BY nspname")
        assert {"public", names.SCHEMA, names.META_SCHEMA, "pg_catalog"} <= set(schemas)
        statements = [
            sql.SQL("CREATE TABLE {} (id int)").format(sql.Identifier(schema, "probe"))
            for schema in schemas
        ]
        statements += [
            sql.SQL("CREATE TABLE probe (id int)"),  # unqualified: the search_path
            sql.SQL("CREATE TEMPORARY TABLE probe (id int)"),
        ]
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
