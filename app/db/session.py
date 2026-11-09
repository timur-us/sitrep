"""The app's connection pool and the advisor transaction (ADR 0008; threat-model path 2).

make_pool() builds the app's pool. It is lazy (min_size 0): opening it connects nobody,
so the app starts and serves /health with the database down. It opens every connection
through app/db/connect.py's CheckedConnection, so the URL check and the PG* refusal
hold for pooled connections too. Only this module builds a pool
(tests/test_import_allowlist.py).

check() runs on every new connection and at every checkout, and closes the connection
and raises unless all of these hold:
- session and current user are the app role, with no SUPERUSER, BYPASSRLS or
  REPLICATION, and no role membership either way;
- app.advisor_id is unset (NULL) or empty ('', what a transaction-local setting
  leaves behind);
- search_path is the role's own, and no other setting was changed for the session;
- of the role settings that apply to this session, there is exactly the one
  db/init/00-roles.sql wrote. A role can change its own defaults with ALTER ROLE ...
  SET, which no REVOKE prevents (PostgreSQL 17 refuses it for app.advisor_id, not for
  built-in settings such as search_path); this check turns such a change into a pool
  that connects nobody, never into a session that runs with it;
- no WITH HOLD cursor is open (other cursors end with their transaction; psycopg's
  own portal for a prepared statement shows in pg_cursors too), no channel is
  listened on, and no advisory lock is held.
Other session state, such as prepared statements, is not checked. psycopg_pool puts a
connection that fails its check back into the pool unless it is closed, so check()
closes it first.

advisor_transaction() is the only way to act for an advisor. It takes a VerifiedAdvisor,
which only app/auth/ issues, and nothing else; it checks out an idle connection, opens
a real transaction, and sets the advisor with set_config(..., true): the id is a bound
parameter, and the setting ends with the transaction. The row-level security policies
of db/migrations/0002_tables_rls.sql read it. The connection it yields is the pool's:
a reference kept past the `with` block would run inside whichever transaction next
holds that connection, so none may be kept.

Not caught here: a statement inside an advisor transaction that calls set_config itself
acts as another advisor for the rest of that transaction, and the setting is gone before
the next checkout. That is the residual risk in docs/threat-model.md;
tests/test_sql_hygiene.py keeps every statement a literal with bound values.

owner_advisor_scope() is the loader's advisor transaction (ADR 0010; the M1 plan's
question 2), and the second place that sets the advisor. The app role only reads, so the
corpus is written by the owner, which FORCE binds to the advisor of the transaction like
any other role. The helper takes a plain advisor id, since the loader has no login, and
refuses before it writes anything unless all of these hold:
- the id is one of the cast's advisors (app/config/branding.py);
- the connection is idle, so the helper opens the transaction itself: one advisor per
  transaction, and a second call inside the first is refused;
- session_user is the migrator and current_user the owner (the migrator after SET ROLE),
  neither of them a superuser or BYPASSRLS, with the role's own search_path and no
  advisor set yet;
- the login itself is the migrator: a superuser can take the migrator's session
  authorization, which changes session_user but not the login pg_stat_activity shows;
- the session holds no temporary relation: the loader's statements name tables without
  a schema, and the owner may create temporary tables, which a name finds first.
Request code cannot reach it: tests/test_loader_scope.py reports any use outside this
definition and its one call in app/db/load.py, and any import of app/db/load.py in app/.
That scan reads names and literals, so getattr() with a built string is not caught; the
checks above are the second line, and the app role fails the first of them.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from psycopg.pq import TransactionStatus
from psycopg_pool import ConnectionPool

from app.auth.identity import VerifiedAdvisor
from app.config import branding
from app.config.settings import check_database_url
from app.db import names
from app.db.connect import CheckedConnection

Pool = ConnectionPool[CheckedConnection]
ROLE_SETTINGS = [f"search_path={names.SEARCH_PATH}"]
# The advisors the loader may write for: the cast's, and no one else.
CAST_ADVISORS = frozenset(person.id for person in branding.ADVISORS)


class GuardError(Exception):
    """A connection the pool must not hand out. The message names rules, not values."""


def problems(conn: CheckedConnection) -> list[str]:
    """What is wrong with this session for the app; empty when nothing is."""
    row = conn.execute(
        "SELECT session_user, current_user, r.rolsuper, r.rolbypassrls, r.rolreplication,"
        " (SELECT count(*) FROM pg_catalog.pg_auth_members m"
        "  WHERE r.oid IN (m.member, m.roleid)),"
        " pg_catalog.current_setting('app.advisor_id', true),"
        " pg_catalog.current_setting('search_path'),"
        " (SELECT s.setconfig FROM pg_catalog.pg_db_role_setting s"
        "  WHERE s.setrole = r.oid AND s.setdatabase = d.oid),"
        " (SELECT count(*) FROM pg_catalog.pg_db_role_setting s"
        "  WHERE s.setrole IN (0, r.oid) AND s.setdatabase IN (0, d.oid)"
        "  AND (s.setrole, s.setdatabase) <> (r.oid, d.oid)),"
        " ARRAY(SELECT s.name FROM pg_catalog.pg_settings s"
        "  WHERE s.source = 'session' AND s.name <> 'search_path' ORDER BY s.name),"
        " (SELECT count(*) FROM pg_catalog.pg_cursors c WHERE c.is_holdable),"
        " (SELECT count(*) FROM pg_catalog.pg_listening_channels()),"
        " (SELECT count(*) FROM pg_catalog.pg_locks l"
        "  WHERE l.locktype = 'advisory' AND l.pid = pg_catalog.pg_backend_pid())"
        " FROM pg_catalog.pg_roles r, pg_catalog.pg_database d"
        " WHERE r.rolname = current_user AND d.datname = pg_catalog.current_database()"
    ).fetchone()
    if row is None:
        return ["the current role has no pg_roles row"]
    session_user, current_user, superuser, bypassrls, replication, memberships = row[:6]
    advisor, search_path, own_settings, other_settings = row[6:10]
    session_settings, cursors, channels, locks = row[10:]
    found = []
    if (session_user, current_user) != (names.APP_ROLE, names.APP_ROLE):
        found.append(
            f"session_user {session_user}, current_user {current_user}, expected {names.APP_ROLE}"
        )
    if superuser:
        found.append(f"{current_user} is a superuser")
    if bypassrls:
        found.append(f"{current_user} has BYPASSRLS")
    if replication:
        found.append(f"{current_user} has REPLICATION")
    if memberships:
        found.append(f"{current_user} has role memberships: {memberships}")
    if advisor not in (None, ""):
        found.append("app.advisor_id is set outside an advisor transaction")
    if search_path != names.SEARCH_PATH:
        found.append(f"search_path is not {names.SEARCH_PATH}")
    if session_settings:
        found.append(f"session-level settings: {', '.join(session_settings)}")
    if own_settings != ROLE_SETTINGS or other_settings:
        found.append(f"role settings of {current_user} differ from db/init/00-roles.sql")
    if cursors:
        found.append(f"open cursors: {cursors}")
    if channels:
        found.append(f"listening on channels: {channels}")
    if locks:
        found.append(f"advisory locks held: {locks}")
    return found


def check(conn: CheckedConnection) -> None:
    """The pool's configure and check callback: close and raise unless the session is clean."""
    try:
        if conn.info.transaction_status != TransactionStatus.IDLE:
            found = ["the connection is not idle"]
        else:
            found = problems(conn)
    except Exception:
        conn.close()
        raise
    if found:
        conn.close()
        raise GuardError("; ".join(found))


def make_pool(url: str, *, max_size: int = 4, timeout: float = 5.0) -> Pool:
    """A closed pool on url; open() starts it without connecting anybody."""
    check_database_url(url)
    return ConnectionPool(
        url,
        connection_class=CheckedConnection,
        kwargs={"autocommit": True},
        min_size=0,
        max_size=max_size,
        open=False,
        configure=check,
        check=check,
        timeout=timeout,
    )


@contextmanager
def advisor_transaction(pool: Pool, advisor: VerifiedAdvisor) -> Iterator[CheckedConnection]:
    """A transaction on a pooled connection in which the policies see only advisor's rows."""
    if type(advisor) is not VerifiedAdvisor:
        raise TypeError(
            f"advisor_transaction takes a VerifiedAdvisor, not {type(advisor).__name__}"
        )
    with pool.connection() as conn:
        if conn.info.transaction_status != TransactionStatus.IDLE:
            raise GuardError("the connection is not idle")
        with conn.transaction():
            conn.execute("SELECT set_config('app.advisor_id', %s, true)", (advisor.advisor_id,))
            yield conn


def owner_session_problems(row: tuple[object, ...] | None) -> list[str]:
    """What is wrong with a session for the loader, from the row owner_advisor_scope()
    reads: session_user, current_user, whether either is a superuser or BYPASSRLS, the
    advisor setting, the search_path, the login (pg_stat_activity), and how many
    temporary relations the session holds."""
    if row is None:
        return ["the session's roles have no pg_roles row"]
    session_user, current_user, session_privileged, current_privileged = row[:4]
    advisor, search_path, login, temporary = row[4:]
    found = []
    if (session_user, current_user) != (names.MIGRATOR_ROLE, names.OWNER_ROLE):
        found.append(
            f"session_user {session_user}, current_user {current_user}, "
            f"expected {names.MIGRATOR_ROLE} acting as {names.OWNER_ROLE}"
        )
    # SET SESSION AUTHORIZATION changes session_user, never the login (review sec-5).
    if login != names.MIGRATOR_ROLE:
        found.append(f"the login is not {names.MIGRATOR_ROLE}")
    if session_privileged or current_privileged:
        found.append("a role of the session is a superuser or has BYPASSRLS")
    if advisor not in (None, ""):
        found.append("an advisor is already set in this transaction")
    if search_path != names.SEARCH_PATH:
        found.append(f"search_path is not {names.SEARCH_PATH}")
    # Unqualified names find a temporary table first: it would take the writes (sec-4).
    if temporary != 0:
        found.append("the session holds temporary relations, which come first by name")
    return found


@contextmanager
def owner_advisor_scope(conn: CheckedConnection, advisor_id: str) -> Iterator[CheckedConnection]:
    """A transaction on conn in which the owner writes advisor_id's rows only (the loader's)."""
    if type(advisor_id) is not str or advisor_id not in CAST_ADVISORS:
        raise GuardError("the advisor is not one of the cast's advisors")
    if conn.info.transaction_status != TransactionStatus.IDLE:
        raise GuardError("the connection is not idle: one advisor per transaction")
    with conn.transaction():
        row = conn.execute(
            "SELECT session_user, current_user, s.rolsuper OR s.rolbypassrls,"
            " c.rolsuper OR c.rolbypassrls,"
            " pg_catalog.current_setting('app.advisor_id', true),"
            " pg_catalog.current_setting('search_path'),"
            " (SELECT a.usename FROM pg_catalog.pg_stat_activity a"
            "  WHERE a.pid = pg_catalog.pg_backend_pid()),"
            " (SELECT count(*) FROM pg_catalog.pg_class t"
            "  WHERE t.relnamespace = pg_catalog.pg_my_temp_schema())"
            " FROM pg_catalog.pg_roles s, pg_catalog.pg_roles c"
            " WHERE s.rolname = session_user AND c.rolname = current_user"
        ).fetchone()
        found = owner_session_problems(row)
        if found:
            raise GuardError("; ".join(found))
        conn.execute("SELECT set_config('app.advisor_id', %s, true)", (advisor_id,))
        yield conn
