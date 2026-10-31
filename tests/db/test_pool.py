"""The app's pool and its guard (ADR 0008; PR 5a review handovers sec-8 and sec-12b).

- The pool opens every connection through app/db/connect.py: with a PG* variable set it
  connects nobody, where a plain psycopg_pool pool takes the variable (control).
- Every new connection, and every connection at checkout, passes the guard: the app
  role as session and current user, no superuser, no BYPASSRLS, no membership either
  way, no advisor setting, the role's own search_path, and the role settings exactly as
  db/init/00-roles.sql wrote them. The app role can change its own defaults with
  ALTER ROLE (not for app.advisor_id, which PostgreSQL refuses it), so the guard reads
  pg_db_role_setting too. A connection that fails is closed, and the pool opens another.
- Pools on the superuser and migrator URLs connect nobody.
- The advisor transaction is a real transaction, takes an idle connection only, and its
  setting does not outlive it.

The planted states run inside a superuser transaction that is rolled back: ALTER ROLE
and GRANT are transactional, and SET LOCAL SESSION AUTHORIZATION makes the session the
app role's for the guard to inspect.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
import pytest
from dbtools import SUPERUSER, Connection
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.pq import TransactionStatus
from psycopg_pool import ConnectionPool, PoolTimeout
from seed_rows import MINE, OTHER

from app.auth.identity import issue
from app.config.settings import Settings, loopback_database_url
from app.db import names, session
from app.db.connect import CheckedConnection, connect
from app.main import create_app

pytestmark = pytest.mark.usefixtures("database")

APP = names.APP_ROLE
SETTING = "SELECT current_setting('app.advisor_id', true)"
STATE = "SELECT current_setting('app.advisor_id', true), current_setting('search_path')"
# Short: these pools are expected to connect nobody.
REFUSED_TIMEOUT = 1.5


def logged(caplog: pytest.LogCaptureFixture) -> str:
    return "\n".join(record.getMessage() for record in caplog.records)


def identifier(name: str) -> sql.Identifier:
    return sql.Identifier(name)


def test_pool_connects_as_the_app_role(app_pool: ConnectionPool) -> None:
    with app_pool.connection() as conn:
        assert conn.execute("SELECT session_user, current_user").fetchone() == (APP, APP)
        assert conn.autocommit
        assert session.problems(conn) == []


def test_controls_a_plain_pool_reads_the_libpq_environment(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    # psycopg_pool opens connections itself: without app/db/connect.py the environment
    # steers them, so the refusal below is not vacuous.
    monkeypatch.setenv("PGOPTIONS", "-c search_path=pg_catalog")
    with (
        ConnectionPool(settings.database_url, min_size=1, open=True, timeout=5.0) as plain,
        plain.connection() as conn,
    ):
        assert conn.execute("SHOW search_path").fetchone() == ("pg_catalog",)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("PGHOSTADDR", "127.0.0.1"),
        ("PGOPTIONS", "-c search_path=pg_catalog"),
        ("PGTZ", "Pacific/Chatham"),
    ],
)
def test_pool_refuses_the_libpq_environment(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    name: str,
    value: str,
) -> None:
    monkeypatch.setenv(name, value)
    with (
        caplog.at_level(logging.INFO, logger="psycopg.pool"),
        session.make_pool(settings.database_url, max_size=1, timeout=REFUSED_TIMEOUT) as pool,
        pytest.raises(PoolTimeout),
        pool.connection() as conn,
    ):
        found = conn.execute("SHOW search_path").fetchone()
        pytest.fail(f"the pool connected with {name} set (search_path {found})")
    assert f"libpq environment variables are set: ['{name}']" in logged(caplog)


@pytest.mark.parametrize("role", [SUPERUSER, names.MIGRATOR_ROLE])
def test_pool_rejects_wrong_role(role: str, caplog: pytest.LogCaptureFixture) -> None:
    url = loopback_database_url(role)
    with (
        caplog.at_level(logging.INFO, logger="psycopg.pool"),
        session.make_pool(url, max_size=1, timeout=REFUSED_TIMEOUT) as pool,
        pytest.raises(PoolTimeout),
        session.advisor_transaction(pool, issue(MINE.id)) as conn,
    ):
        who = conn.execute("SELECT current_user").fetchone()[0]
        pytest.fail(f"pool accepted a {who} connection")
    assert f"session_user {role}, current_user {role}, expected {APP}" in logged(caplog)


@contextmanager
def as_app(superuser: Connection, *statements: sql.Composable) -> Iterator[Connection]:
    """The superuser's session, after statements, as the app role would see it: rolled back."""
    with superuser.transaction(force_rollback=True):
        for statement in statements:
            superuser.execute(statement)
        superuser.execute(sql.SQL("SET LOCAL SESSION AUTHORIZATION {}").format(identifier(APP)))
        superuser.execute(
            sql.SQL("SET LOCAL search_path = {}, public").format(identifier(names.SCHEMA))
        )
        yield superuser


def alter(template: str, *roles: str) -> sql.Composed:
    return sql.SQL(template).format(*(identifier(role) for role in roles))


PLANTED = [
    ("superuser", [alter("ALTER ROLE {} SUPERUSER", APP)], f"{APP} is a superuser"),
    ("bypassrls", [alter("ALTER ROLE {} BYPASSRLS", APP)], f"{APP} has BYPASSRLS"),
    (
        "member of the owner",
        [alter("GRANT {} TO {}", names.OWNER_ROLE, APP)],
        f"{APP} has role memberships: 1",
    ),
    (
        "granted to the migrator",
        [alter("GRANT {} TO {}", APP, names.MIGRATOR_ROLE)],
        f"{APP} has role memberships: 1",
    ),
    (
        "a default for every database",
        [alter("ALTER ROLE {} SET row_security = off", APP)],
        f"role settings of {APP} differ from db/init/00-roles.sql",
    ),
    (
        "this database's default replaced",
        [
            sql.SQL("ALTER ROLE {} IN DATABASE {} SET search_path = public").format(
                identifier(APP), identifier(names.DATABASE)
            )
        ],
        f"role settings of {APP} differ from db/init/00-roles.sql",
    ),
    (
        "a session-level advisor setting",
        [sql.SQL("SELECT set_config('app.advisor_id', {}, false)").format(sql.Literal(OTHER.id))],
        "app.advisor_id is set outside an advisor transaction",
    ),
    # Review findings test-7 and sec-3.
    (
        "a default for all roles",
        [sql.SQL("ALTER ROLE ALL SET statement_timeout = 0")],
        f"role settings of {APP} differ from db/init/00-roles.sql",
    ),
    (
        "a default for this database",
        [alter("ALTER DATABASE {} SET statement_timeout = 0", names.DATABASE)],
        f"role settings of {APP} differ from db/init/00-roles.sql",
    ),
    ("replication", [alter("ALTER ROLE {} REPLICATION", APP)], f"{APP} has REPLICATION"),
    (
        "another session-level setting",
        [sql.SQL("SELECT set_config('row_security', 'off', false)")],
        "session-level settings: row_security",
    ),
    (
        "a held cursor",
        [sql.SQL("DECLARE guard_probe CURSOR WITH HOLD FOR SELECT 1")],
        "open cursors: 1",
    ),
    ("an advisory lock", [sql.SQL("SELECT pg_advisory_lock(4242)")], "advisory locks held: 1"),
]


def test_guard_control_the_planted_session_is_clean(superuser: Connection) -> None:
    with as_app(superuser) as conn:
        assert conn.execute("SELECT session_user, current_user").fetchone() == (APP, APP)
        assert session.problems(conn) == []


@pytest.mark.parametrize(
    ("statements", "problem"),
    [(statements, problem) for _, statements, problem in PLANTED],
    ids=[name for name, _, _ in PLANTED],
)
def test_guard_reports_a_planted_state(
    superuser: Connection, statements: list[sql.Composable], problem: str
) -> None:
    with as_app(superuser, *statements) as conn:
        found = session.problems(conn)
    assert found == [problem], found


def test_guard_reports_a_foreign_search_path(superuser: Connection) -> None:
    with as_app(superuser) as conn:
        conn.execute("SET LOCAL search_path = public")
        found = session.problems(conn)
    assert found == [f"search_path is not {names.SCHEMA}, public"], found


def test_guard_reports_a_session_that_switched_role(superuser: Connection) -> None:
    # session_user and current_user are compared both (review finding test-7).
    with superuser.transaction(force_rollback=True):
        superuser.execute(sql.SQL("SET LOCAL ROLE {}").format(identifier(APP)))
        superuser.execute(
            sql.SQL("SET LOCAL search_path = {}, public").format(identifier(names.SCHEMA))
        )
        found = session.problems(superuser)
    assert found == [f"session_user {SUPERUSER}, current_user {APP}, expected {APP}"], found


@pytest.mark.parametrize("role", [SUPERUSER, names.MIGRATOR_ROLE])
def test_guard_reports_another_login(role: str) -> None:
    with connect(loopback_database_url(role), autocommit=True) as conn:
        found = session.problems(conn)
    assert f"session_user {role}, current_user {role}, expected {APP}" in found, found


@pytest.mark.parametrize("scope", ["", " IN DATABASE {db}"], ids=["all databases", "this database"])
@pytest.mark.parametrize(
    "setting", ["search_path = public", "row_security = off", "statement_timeout = 1000"]
)
def test_guard_sees_the_app_roles_own_alter_role(
    settings: Settings, scope: str, setting: str
) -> None:
    # What SQL injection as the app role could do to every later connection: measured,
    # the role may change these defaults of its own.
    statement = sql.SQL("ALTER ROLE {app}" + scope + " SET " + setting).format(
        app=identifier(APP), db=identifier(names.DATABASE)
    )
    with (
        connect(settings.database_url, autocommit=True) as app,
        app.transaction(force_rollback=True),
    ):
        app.execute(statement)
        found = session.problems(app)
    assert found == [f"role settings of {APP} differ from db/init/00-roles.sql"], found


@pytest.mark.parametrize(
    ("statements", "problem"),
    [
        (["LISTEN guard_probe"], "listening on channels: 1"),
        (["SELECT pg_advisory_lock(4243)"], "advisory locks held: 1"),
        (
            ["BEGIN", "DECLARE guard_probe CURSOR WITH HOLD FOR SELECT 1", "COMMIT"],
            "open cursors: 1",
        ),
        (
            ["SELECT set_config('statement_timeout', '1000', false)"],
            "session-level settings: statement_timeout",
        ),
    ],
    ids=["listen", "advisory lock", "held cursor", "session-level setting"],
)
def test_guard_sees_session_state_of_the_app_role(
    settings: Settings, statements: list[str], problem: str
) -> None:
    # State that outlives a transaction (review finding sec-3), left by the app role.
    with connect(settings.database_url, autocommit=True) as app:
        for statement in statements:
            app.execute(statement)
        found = session.problems(app)
    assert found == [problem], found


@pytest.mark.parametrize("scope", ["", " IN DATABASE {db}"])
def test_app_role_cannot_put_the_advisor_setting_in_its_defaults(
    settings: Settings, scope: str
) -> None:
    # Measured on PostgreSQL 17: placeholder settings in role defaults need the superuser.
    statement = sql.SQL("ALTER ROLE {app}" + scope + " SET app.advisor_id = {value}").format(
        app=identifier(APP), db=identifier(names.DATABASE), value=sql.Literal(OTHER.id)
    )
    with (
        connect(settings.database_url, autocommit=True) as app,
        pytest.raises(psycopg.errors.InsufficientPrivilege, match="permission denied to set"),
        app.transaction(force_rollback=True),
    ):
        app.execute(statement)


def test_revoke_set_on_parameter_does_not_stop_set_config(superuser: Connection) -> None:
    # Measured on PostgreSQL 17: the residual risk in docs/threat-model.md. REVOKE leaves
    # no ACL entry for a placeholder setting, and the app role still sets it.
    with as_app(
        superuser,
        sql.SQL("REVOKE SET ON PARAMETER app.advisor_id FROM PUBLIC"),
        sql.SQL("REVOKE ALL ON PARAMETER app.advisor_id FROM {}").format(identifier(APP)),
    ) as conn:
        acl = conn.execute(
            "SELECT count(*) FROM pg_parameter_acl WHERE parname = 'app.advisor_id'"
        ).fetchone()
        forged = conn.execute(
            "SELECT current_user, set_config('app.advisor_id', %s, true)", (OTHER.id,)
        ).fetchone()
    assert acl == (0,), acl
    assert forged == (APP, OTHER.id), forged


@pytest.mark.parametrize(
    "statements",
    [
        [sql.SQL("SELECT set_config('app.advisor_id', {}, false)").format(sql.Literal(OTHER.id))],
        [sql.SQL("SET search_path = public")],
        [sql.SQL("SELECT set_config('row_security', 'off', false)")],
        [sql.SQL("LISTEN guard_probe")],
        [sql.SQL("SELECT pg_advisory_lock(4244)")],
        [
            sql.SQL("BEGIN"),
            sql.SQL("DECLARE guard_probe CURSOR WITH HOLD FOR SELECT 1"),
            sql.SQL("COMMIT"),
        ],
    ],
    ids=[
        "session-level advisor setting",
        "session-level search_path",
        "session-level row_security",
        "listen",
        "advisory lock",
        "held cursor",
    ],
)
def test_session_level_change_is_rejected_at_checkout(
    app_pool: ConnectionPool, statements: list[sql.Composable]
) -> None:
    with app_pool.connection() as conn:
        for statement in statements:
            conn.execute(statement)
        first, pid = conn, conn.info.backend_pid
    with app_pool.connection() as conn:
        again = conn.info.backend_pid
        state = conn.execute(STATE).fetchone()
    assert again != pid, f"the pooled connection was handed out again with {state}"
    assert first.closed, "the rejected connection was not closed"
    assert state == (None, f"{names.SCHEMA}, public"), state


def test_advisor_transaction_is_a_real_transaction(app_pool: ConnectionPool) -> None:
    with session.advisor_transaction(app_pool, issue(MINE.id)) as conn:
        status = conn.info.transaction_status
        inside = conn.execute(SETTING).fetchone()[0]
        pid = conn.info.backend_pid
    with app_pool.connection() as conn:
        after = conn.execute(SETTING).fetchone()[0]
        assert conn.info.backend_pid == pid
    assert status == TransactionStatus.INTRANS, status
    assert inside == MINE.id
    assert after == "", f"the advisor setting outlived its transaction: {after!r}"


class OneConnection:
    """A pool stand-in that hands out one connection, already inside a transaction."""

    def __init__(self, conn: Connection) -> None:
        self.conn = conn

    @contextmanager
    def connection(self) -> Iterator[Connection]:
        yield self.conn


def test_advisor_transaction_takes_an_idle_connection_only(settings: Settings) -> None:
    with connect(settings.database_url, autocommit=True) as conn:
        conn.execute("BEGIN")
        with (
            pytest.raises(session.GuardError, match="not idle"),
            session.advisor_transaction(OneConnection(conn), issue(MINE.id)),
        ):
            pytest.fail("the advisor transaction ran inside another transaction")
        conn.execute("ROLLBACK")


# check() itself (review finding test-7).


def test_check_closes_a_connection_that_is_not_idle(settings: Settings) -> None:
    conn = connect(settings.database_url, autocommit=True)
    conn.execute("BEGIN")
    with pytest.raises(session.GuardError, match="not idle"):
        session.check(conn)
    assert conn.closed


def test_check_closes_a_rejected_connection() -> None:
    conn = connect(loopback_database_url(SUPERUSER), autocommit=True)
    with pytest.raises(session.GuardError, match=f"current_user {SUPERUSER}"):
        session.check(conn)
    assert conn.closed


def test_check_closes_the_connection_when_its_query_fails(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(conn: Connection) -> list[str]:
        raise RuntimeError("guard query failed")

    monkeypatch.setattr(session, "problems", broken)
    conn = connect(settings.database_url, autocommit=True)
    with pytest.raises(RuntimeError, match="guard query failed"):
        session.check(conn)
    assert conn.closed


def test_check_passes_a_clean_connection(settings: Settings) -> None:
    with connect(settings.database_url, autocommit=True) as conn:
        session.check(conn)
        assert not conn.closed


def test_an_error_inside_the_advisor_transaction_leaves_nothing(app_pool: ConnectionPool) -> None:
    # Review finding test-8: the transaction rolls back, and the connection is reused clean.
    with (
        pytest.raises(psycopg.errors.DivisionByZero),
        session.advisor_transaction(app_pool, issue(MINE.id)) as conn,
    ):
        pid = conn.info.backend_pid
        conn.execute("SELECT 1 / 0")
    with app_pool.connection() as conn:
        assert conn.info.backend_pid == pid
        assert conn.info.transaction_status == TransactionStatus.IDLE
        assert conn.execute(SETTING).fetchone()[0] in (None, "")


def test_the_app_opens_and_closes_its_pool(settings: Settings) -> None:
    # Review finding test-9: the lifespan's pool works with the database up.
    app = create_app(settings)
    with TestClient(app):
        pool = app.state.pool
        with pool.connection() as conn:
            assert conn.execute("SELECT current_user").fetchone() == (APP,)
        assert isinstance(conn, CheckedConnection)
    assert pool.closed


def test_a_connection_stays_clean_through_many_checkouts(app_pool: ConnectionPool) -> None:
    # psycopg prepares a statement after five runs (prepare_threshold), and the portal of a
    # prepared statement shows in pg_cursors while it runs: the guard counts only WITH HOLD
    # cursors, or it would reject every connection from the sixth checkout on.
    pids = set()
    for _ in range(12):
        with session.advisor_transaction(app_pool, issue(MINE.id)) as conn:
            conn.execute("SELECT 1")
        with app_pool.connection() as conn:
            pids.add(conn.info.backend_pid)
    assert len(pids) == 1, f"{len(pids)} connections for 12 checkouts"
