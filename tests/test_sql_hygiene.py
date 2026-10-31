"""SQL hygiene in app/ (plan condition 2; threat-model path 2, residual risk).

Custom settings such as app.advisor_id can be set by any role (PGC_USERSET), and REVOKE
SET ON PARAMETER does not stop it: SQL injection as the app role could call set_config
and act as another advisor for the rest of a transaction. So no app statement is ever
built from strings. An AST scan of app/:

- `.execute`, `.executemany`, `.copy` and `.stream` take a string literal or a constant
  that app/db/queries.py defines; values travel as bound parameters;
- no psycopg.sql, ClientCursor, mogrify or cursor_factory;
- app/db/queries.py holds string literals only;
- set_config and app.advisor_id appear in app/db/session.py only (in any text there);
- no string anywhere in app/ says SET LOCAL or SET app.

app/db/migrate.py is excepted from the first two rules: it composes its own DDL with
psycopg.sql from app/db/names.py, never from input. The scan reads names, not values:
a statement reached through getattr(), or a module imported under the name `queries`
that is not app/db/queries.py, is not caught.
"""

from __future__ import annotations

import ast
import re

import pytest
from repo_files import ROOT

APP = ROOT / "app"
SESSION = "app/db/session.py"
QUERIES = "app/db/queries.py"
MIGRATE = "app/db/migrate.py"
STATEMENT_METHODS = frozenset({"execute", "executemany", "copy", "stream"})
# Client-side composition or interpolation of SQL.
BANNED = frozenset({"ClientCursor", "AsyncClientCursor", "mogrify", "cursor_factory"})
SESSION_ONLY = ("set_config", "app.advisor_id")
SET_COMMAND = re.compile(r"\bSET\s+(?:LOCAL\b|app\.)", re.IGNORECASE)


def query_constants(source: str) -> tuple[set[str], list[str]]:
    """The names queries.py assigns, and every statement there that is not
    `NAME = "literal"` (a docstring and `from __future__` aside)."""
    names, problems = set(), []
    for index, node in enumerate(ast.parse(source).body):
        if index == 0 and isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            continue
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            names.add(node.targets[0].id)
            continue
        problems.append(f"{QUERIES}:{node.lineno}: not a NAME = 'literal' statement")
    return names, problems


def scan(source: str, rel: str, constants: set[str]) -> tuple[int, list[str]]:
    """The statement calls checked in one file, and the rule breaks found there."""
    tree = ast.parse(source)
    modules: set[str] = set()  # local names for app.db.queries
    imported: set[str] = set()  # constants imported from it by name
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "app.db":
            modules |= {
                alias.asname or alias.name for alias in node.names if alias.name == "queries"
            }
        elif isinstance(node, ast.ImportFrom) and node.module == "app.db.queries":
            imported |= {alias.asname or alias.name for alias in node.names}
        elif isinstance(node, ast.Import):
            modules |= {alias.asname for alias in node.names if alias.name == "app.db.queries"}

    def allowed(statement: ast.expr) -> bool:
        if isinstance(statement, ast.Constant):
            return isinstance(statement.value, str)
        if isinstance(statement, ast.Name):
            return statement.id in imported and statement.id in constants
        if isinstance(statement, ast.Attribute) and isinstance(statement.value, ast.Name):
            return statement.value.id in modules and statement.attr in constants
        return False

    checked, problems = 0, []
    composes = rel != MIGRATE
    for node in ast.walk(tree):
        where = f"{rel}:{getattr(node, 'lineno', '?')}"
        if composes and isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module.startswith("psycopg.sql") or (
                module == "psycopg" and any(alias.name == "sql" for alias in node.names)
            ):
                problems.append(f"{where}: psycopg.sql")
            problems += [f"{where}: {alias.name}" for alias in node.names if alias.name in BANNED]
        elif composes and isinstance(node, ast.Import):
            problems += [
                f"{where}: psycopg.sql"
                for alias in node.names
                if alias.name.startswith("psycopg.sql")
            ]
        elif composes and isinstance(node, ast.Attribute):
            if node.attr in BANNED:
                problems.append(f"{where}: .{node.attr}")
            if (
                node.attr == "sql"
                and isinstance(node.value, ast.Name)
                and node.value.id == "psycopg"
            ):
                problems.append(f"{where}: psycopg.sql")
        elif composes and isinstance(node, ast.Name) and node.id in BANNED:
            problems.append(f"{where}: {node.id}")
        elif composes and isinstance(node, ast.keyword) and node.arg in BANNED:
            problems.append(f"{where}: {node.arg}=")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str | bytes):
            text = node.value if isinstance(node.value, str) else node.value.decode("latin-1")
            if SET_COMMAND.search(text):
                problems.append(f"{where}: a string says {SET_COMMAND.search(text).group(0)!r}")
        if (
            composes
            and isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in STATEMENT_METHODS
        ):
            statement = node.args[0] if node.args else None
            for keyword in node.keywords:
                if keyword.arg in ("query", "statement"):
                    statement = keyword.value
            if statement is None:
                continue  # list.copy() and the like: no statement
            checked += 1
            if not allowed(statement):
                kind = type(statement).__name__
                problems.append(
                    f"{where}: .{node.func.attr}() takes {kind}, "
                    f"not a literal or a constant from {QUERIES}"
                )
    if rel != SESSION:
        for number, line in enumerate(source.splitlines(), 1):
            problems += [
                f"{rel}:{number}: {word} outside {SESSION}" for word in SESSION_ONLY if word in line
            ]
    return checked, problems


def check(sources: dict[str, str]) -> tuple[int, list[str]]:
    constants, problems = query_constants(sources.get(QUERIES, ""))
    total = 0
    for rel, source in sorted(sources.items()):
        checked, found = scan(source, rel, constants)
        total += checked
        problems += found
    return total, problems


def app_sources() -> dict[str, str]:
    return {
        path.relative_to(ROOT).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(APP.rglob("*.py"))
    }


def test_sql_hygiene() -> None:
    sources = app_sources()
    for required in (SESSION, QUERIES, MIGRATE, "app/db/connect.py"):
        assert required in sources, f"{required} was not scanned"
    checked, problems = check(sources)
    assert not problems, "\n".join(problems)
    # Non-vacuity: the advisor setting and the guard in session.py are among the calls.
    assert checked >= 2, f"only {checked} statement calls checked"
    assert "set_config" in sources[SESSION]
    constants, _ = query_constants(sources[QUERIES])
    assert {"SEARCH_DOCUMENTS_ENGLISH", "SEARCH_DOCUMENTS_GERMAN"} <= constants, constants


CLEAN_QUERIES = 'X = "SELECT 1"\n'

PLANTED = [
    ("app/db/x.py", "conn.execute(f'SELECT {x}')\n", ".execute() takes JoinedStr"),
    ("app/db/x.py", "conn.execute('SELECT ' + x)\n", ".execute() takes BinOp"),
    ("app/db/x.py", "conn.execute('SELECT %s' % x)\n", ".execute() takes BinOp"),
    ("app/db/x.py", "conn.execute('SELECT {}'.format(x))\n", ".execute() takes Call"),
    ("app/db/x.py", "q = 'SELECT 1'\nconn.execute(q)\n", ".execute() takes Name"),
    ("app/db/x.py", "cur.executemany(q, rows)\n", ".executemany() takes Name"),
    ("app/db/x.py", "cur.copy(f'COPY {t} TO STDOUT')\n", ".copy() takes JoinedStr"),
    ("app/db/x.py", "cur.stream(query=q)\n", ".stream() takes Name"),
    ("app/db/x.py", "from app.db import queries\nconn.execute(queries.NOPE)\n", "takes Attribute"),
    ("app/db/x.py", "from app.db import queries\nconn.execute(queries.X + y)\n", "takes BinOp"),
    ("app/db/x.py", "from psycopg import sql\n", "psycopg.sql"),
    ("app/db/x.py", "import psycopg.sql\n", "psycopg.sql"),
    ("app/db/x.py", "from psycopg.sql import SQL\n", "psycopg.sql"),
    ("app/db/x.py", "import psycopg\npsycopg.sql.SQL('x')\n", "psycopg.sql"),
    ("app/db/x.py", "import psycopg\npsycopg.ClientCursor(conn)\n", ".ClientCursor"),
    ("app/db/x.py", "from psycopg import ClientCursor\n", "ClientCursor"),
    ("app/db/x.py", "cur.mogrify('SELECT %s', (x,))\n", ".mogrify"),
    ("app/db/x.py", "connect(url, cursor_factory=C)\n", "cursor_factory="),
    (
        "app/db/x.py",
        "conn.execute(\"SELECT set_config('a', %s, true)\", (x,))\n",
        "set_config outside",
    ),
    ("app/db/x.py", "# app.advisor_id in a comment\n", "app.advisor_id outside"),
    ("app/db/session.py", "conn.execute(\"SET LOCAL app.advisor_id = 'x'\")\n", "SET LOCAL"),
    ("app/db/session.py", "conn.execute('set app.advisor_id = 1')\n", "set app."),
    ("app/db/migrate.py", "x = 'SET LOCAL x = 1'\n", "SET LOCAL"),
    ("app/db/migrate.py", "x = 'set_config'\n", "set_config outside"),
    # Review findings sec-4 / test-10: bypasses the first version missed.
    ("app/db/x.py", "import psycopg as pg\npg.sql.SQL('x')\n", "psycopg.sql"),
    (
        "app/db/x.py",
        "conn.execute(\"SELECT SET_CONFIG('a', %s, true)\", (x,))\n",
        "set_config outside",
    ),
    ("app/db/x.py", "x = 'APP.ADVISOR_ID'\n", "app.advisor_id outside"),
    ("app/db/x.py", "x = 'app . advisor_id'\n", "app.advisor_id outside"),
    ("app/db/session.py", "conn.execute('SET SESSION app.advisor_id = 1')\n", "SET SESSION"),
    ("app/db/session.py", "conn.execute('set   local  x = 1')\n", "set   local"),
    ("app/db/x.py", "conn.execute(**statement)\n", ".execute() takes **"),
    ("app/db/x.py", "run = conn.execute\nrun(f'SELECT {x}')\n", ".execute referenced"),
    ("app/db/x.py", "conn.pgconn.exec_(b'SELECT 1')\n", ".pgconn"),
    (
        "app/db/x.py",
        "from app.db.queries import X\nX = f'SELECT {y}'\nconn.execute(X)\n",
        "rebinds X",
    ),
    (QUERIES, "X = f'SELECT {y}'\n", "not a NAME = 'literal' statement"),
    (QUERIES, "X = 'SELECT ' + Y\n", "not a NAME = 'literal' statement"),
    (QUERIES, "def build(): return 'x'\n", "not a NAME = 'literal' statement"),
]


@pytest.mark.parametrize(("rel", "source", "needle"), PLANTED)
def test_sql_hygiene_planted_violations_are_caught(rel: str, source: str, needle: str) -> None:
    sources = {QUERIES: CLEAN_QUERIES} | {rel: source}
    _, problems = check(sources)
    assert any(needle in problem for problem in problems), problems


def test_sql_hygiene_clean_code_passes() -> None:
    sources = {
        QUERIES: CLEAN_QUERIES,
        "app/db/x.py": (
            "from app.db import queries\nfrom app.db.queries import X\n"
            "conn.execute(queries.X, (a,))\nconn.execute(X)\nconn.execute('SELECT 1')\n"
            "conn.execute(query='SELECT 1')\nrows.copy()\n'a b'.replace('a', 'b')\n"
        ),
        SESSION: "conn.execute(\"SELECT set_config('app.advisor_id', %s, true)\", (a,))\n",
        MIGRATE: "from psycopg import sql\nconn.execute(sql.SQL('SET ROLE {}'))\n",
    }
    checked, problems = check(sources)
    assert problems == [], problems
    assert checked == 5
