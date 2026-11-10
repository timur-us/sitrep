"""SQL hygiene in app/ (plan condition 2; threat-model path 2, residual risk).

Custom settings such as app.advisor_id can be set by any role (PGC_USERSET), and REVOKE
SET ON PARAMETER does not stop it: SQL injection as the app role could call set_config
and act as another advisor for the rest of a transaction. So no app statement is ever
built from strings. An AST scan of app/:

- `.execute`, `.executemany`, `.copy` and `.stream` take a string literal or a constant
  that app/db/queries.py defines, never `**kwargs`; values travel as bound parameters;
  `.execute`, `.executemany` and `.stream` are only called, never passed around, and a
  constant imported from queries.py is never rebound;
- no psycopg.sql (under any import name), ClientCursor, mogrify, cursor_factory or
  pgconn (libpq's own exec and send calls);
- app/db/queries.py holds string literals only;
- set_config and app.advisor_id appear in app/db/session.py only, in any text there,
  in any case and with any spacing around the dot;
- no string anywhere in app/ says SET LOCAL, SET SESSION or SET app.;
- outside app/db/migrate.py every statement, literal or constant, is SELECT, INSERT,
  UPDATE, DELETE or WITH (COPY for .copy): DDL, privileges and session commands such
  as ALTER TABLE ... NO FORCE ROW LEVEL SECURITY stay in reviewed migration files
  (review finding sec-1 of M1 PR 7).

app/db/migrate.py is excepted from the first two rules: it composes its own DDL with
psycopg.sql from app/db/names.py, never from input.

One call is excepted from the first rule (M1 PR 8): the advisor transaction's handle,
AdvisorConnection in app/db/session.py, passes its caller's statement on in its
execute(), so that one call takes the method's own parameter. The exception holds for
that class, that method and that parameter only, and not if the method rebinds it; every
call of the handle's execute() is a statement call like any other, and is checked.

This is a lint against building SQL from strings by accident, not a proof. It reads
names, not values: a statement reached through getattr(), a method taken from a class
rather than an instance, or a module imported under the name `queries` that is not
app/db/queries.py, is not caught.
"""

from __future__ import annotations

import ast
import re

import pytest
from repo_files import ROOT

from app.db import queries

APP = ROOT / "app"
SESSION = "app/db/session.py"
QUERIES = "app/db/queries.py"
MIGRATE = "app/db/migrate.py"
LOAD = "app/db/load.py"
STATEMENT_METHODS = frozenset({"execute", "executemany", "copy", "stream"})
# Never referenced without a call; .copy is left out, it is also dict's and list's.
CALLED_ONLY = frozenset({"execute", "executemany", "stream"})
# Client-side composition or interpolation of SQL, and libpq's raw connection.
BANNED = frozenset({"ClientCursor", "AsyncClientCursor", "mogrify", "cursor_factory", "pgconn"})
# SQL names and setting names are case-insensitive.
SESSION_ONLY = {
    "set_config": re.compile(r"set_config", re.IGNORECASE),
    "app.advisor_id": re.compile(r"app\s*\.\s*advisor_id", re.IGNORECASE),
}
SET_COMMAND = re.compile(r"\bSET\s+(?:LOCAL\b|SESSION\b|app\s*\.)", re.IGNORECASE)
# The statements app/ may run outside the migration runner, by their first keyword.
DML = frozenset({"SELECT", "INSERT", "UPDATE", "DELETE", "WITH"})
LEADING = re.compile(r"(?:\s+|--[^\n]*(?:\n|$)|/\*.*?\*/)*", re.DOTALL)


def first_keyword(statement: str) -> str:
    """The statement's first word, after whitespace and comments, in upper case."""
    rest = statement[LEADING.match(statement).end() :]
    word = re.match(r"[A-Za-z]+", rest)
    return word.group(0).upper() if word else rest[:1]


HANDLERS = "app/harness/handlers.py"
# The handle's delegation (M1 PR 8): class and method in app/db/session.py.
DELEGATE = ("AdvisorConnection", "execute")


def delegations(tree: ast.Module) -> set[int]:
    """The calls in session.py's AdvisorConnection.execute() that pass the method's own
    statement parameter on as their statement, while the method never rebinds it."""
    found: set[int] = set()
    for node in tree.body:
        if not (isinstance(node, ast.ClassDef) and node.name == DELEGATE[0]):
            continue
        for method in node.body:
            if not (
                isinstance(method, ast.FunctionDef)
                and method.name == DELEGATE[1]
                and len(method.args.args) >= 2
            ):
                continue
            parameter = method.args.args[1].arg
            rebound = any(
                isinstance(inner, ast.Name)
                and inner.id == parameter
                and isinstance(inner.ctx, ast.Store | ast.Del)
                for inner in ast.walk(method)
            )
            if rebound:
                continue
            found |= {
                id(call)
                for call in ast.walk(method)
                if isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "execute"
                and call.args
                and isinstance(call.args[0], ast.Name)
                and call.args[0].id == parameter
            }
    return found


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


def scan(
    source: str, rel: str, constants: set[str], values: dict[str, str] | None = None
) -> tuple[int, list[str]]:
    """The statement calls checked in one file, and the rule breaks found there."""
    tree = ast.parse(source)
    modules: set[str] = set()  # local names for app.db.queries
    imported: set[str] = set()  # constants imported from it by name
    drivers: set[str] = set()  # local names for the psycopg package
    called: set[int] = set()  # the func node of every call
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            called.add(id(node.func))
        if isinstance(node, ast.Import):
            drivers |= {
                alias.asname or "psycopg" for alias in node.names if alias.name == "psycopg"
            }
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
    delegated = delegations(tree) if rel == SESSION else set()
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
            if node.attr == "sql" and isinstance(node.value, ast.Name) and node.value.id in drivers:
                problems.append(f"{where}: psycopg.sql")
            if node.attr in CALLED_ONLY and id(node) not in called:
                problems.append(f"{where}: .{node.attr} referenced, not called")
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
            if statement is None and any(keyword.arg is None for keyword in node.keywords):
                problems.append(f"{where}: .{node.func.attr}() takes **kwargs")
                continue
            if statement is None:
                continue  # list.copy() and the like: no statement
            checked += 1
            if id(node) in delegated:
                continue  # the handle's own delegation; its callers are checked
            if not allowed(statement):
                kind = type(statement).__name__
                problems.append(
                    f"{where}: .{node.func.attr}() takes {kind}, "
                    f"not a literal or a constant from {QUERIES}"
                )
                continue
            text = statement_text(statement, values or {})
            keyword = first_keyword(text) if text is not None else None
            permitted = DML | ({"COPY"} if node.func.attr == "copy" else set())
            if keyword is not None and keyword not in permitted:
                problems.append(f"{where}: {keyword} is not SELECT, INSERT, UPDATE, DELETE or WITH")
        if composes and isinstance(node, ast.Assign | ast.AugAssign | ast.AnnAssign):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id in imported:
                    problems.append(f"{where}: rebinds {target.id} from {QUERIES}")
                if (
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id in modules
                ):
                    problems.append(f"{where}: rebinds {target.attr} in {QUERIES}")
    if rel != SESSION:
        for number, line in enumerate(source.splitlines(), 1):
            problems += [
                f"{rel}:{number}: {word} outside {SESSION}"
                for word, pattern in SESSION_ONLY.items()
                if pattern.search(line)
            ]
    return checked, problems


def statement_text(statement: ast.expr, values: dict[str, str]) -> str | None:
    """The SQL an allowed statement argument stands for, where the scan can tell."""
    if isinstance(statement, ast.Constant) and isinstance(statement.value, str):
        return statement.value
    if isinstance(statement, ast.Name):
        return values.get(statement.id)
    if isinstance(statement, ast.Attribute):
        return values.get(statement.attr)
    return None


def query_values(source: str) -> dict[str, str]:
    """queries.py's constants and their statements."""
    found = {}
    for node in ast.parse(source).body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            found[node.targets[0].id] = node.value.value
    return found


def check(sources: dict[str, str]) -> tuple[int, list[str]]:
    constants, problems = query_constants(sources.get(QUERIES, ""))
    values = query_values(sources.get(QUERIES, ""))
    total = 0
    for rel, source in sorted(sources.items()):
        checked, found = scan(source, rel, constants, values)
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
    for required in (SESSION, QUERIES, MIGRATE, LOAD, "app/db/connect.py"):
        assert required in sources, f"{required} was not scanned"
    checked, problems = check(sources)
    assert not problems, "\n".join(problems)
    # Non-vacuity: the advisor setting and the guard in session.py are among the calls,
    # and so are the loader's statements.
    assert checked >= 2, f"only {checked} statement calls checked"
    assert "set_config" in sources[SESSION]
    constants, _ = query_constants(sources[QUERIES])
    assert {
        "SEARCH_DOCUMENTS_ENGLISH",
        "SEARCH_DOCUMENTS_GERMAN",
        "SEARCH_SHEETS_ENGLISH",
        "NEAREST_CHUNKS",
        "MANDATE_VISIBLE",
        "PORTFOLIO_REPORT",
    } <= constants, constants
    loader_calls, _ = scan(sources[LOAD], LOAD, constants)
    assert loader_calls >= 11, f"only {loader_calls} statement calls checked in {LOAD}"
    # M1 PR 8: the READ handlers run their statements through the scan, and the handle's
    # delegation is the one exception, used once.
    handler_calls, _ = scan(sources[HANDLERS], HANDLERS, constants)
    assert handler_calls >= 10, f"only {handler_calls} statement calls checked in {HANDLERS}"
    assert len(delegations(ast.parse(sources[SESSION]))) == 1


def loader_statement_problems(statements: dict[str, str]) -> list[str]:
    """The loader's rules (ADR 0010): no statement names trust, which the database
    generates; none truncates; every advisor table's delete is bound to the advisor as
    well as scoped by row-level security."""
    found = []
    for name, value in sorted(statements.items()):
        if re.search(r"\btrust\b", value, re.IGNORECASE):
            found.append(f"{name}: names trust")
        if re.search(r"\btruncate\b", value, re.IGNORECASE):
            found.append(f"{name}: truncates")
        delete = name.startswith("LOAD_DELETE_") and name != "LOAD_DELETE_PRODUCT_SHEETS"
        if delete and not value.endswith(" WHERE advisor_id = %s"):
            found.append(f"{name}: not bound to the advisor")
    return found


def test_the_loaders_statements() -> None:
    loader = {name: value for name, value in vars(queries).items() if name.startswith("LOAD_")}
    # One upsert, five deletes, five inserts (review finding test-16: exact, both ways).
    assert len(loader) == 11, sorted(loader)
    assert loader_statement_problems(loader) == []
    loader_calls, _ = scan(app_sources()[LOAD], LOAD, set(loader))
    assert loader_calls == 11, loader_calls


def test_planted_loader_statements_are_caught() -> None:
    assert loader_statement_problems(
        {
            "LOAD_INSERT_X": "INSERT INTO documents (id, trust) VALUES (%s, %s)",
            "LOAD_UPDATE_X": "UPDATE documents SET Trust = 'internal'",
            "LOAD_WIPE": "TRUNCATE chunks",
            "LOAD_DELETE_CHUNKS": "DELETE FROM chunks",
            "LOAD_DELETE_DOCUMENTS": "DELETE FROM documents WHERE id = %s",
            "LOAD_DELETE_PRODUCT_SHEETS": "DELETE FROM product_sheets",
        }
    ) == [
        "LOAD_DELETE_CHUNKS: not bound to the advisor",
        "LOAD_DELETE_DOCUMENTS: not bound to the advisor",
        "LOAD_INSERT_X: names trust",
        "LOAD_UPDATE_X: names trust",
        "LOAD_WIPE: truncates",
    ]
    assert loader_statement_problems({"LOAD_DELETE_X": "DELETE FROM x WHERE advisor_id = %s"}) == []


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
    # Review finding sec-1 of PR 7: outside the migration runner a statement reads or
    # writes rows; DDL, privileges and session commands stay in reviewed migration files.
    (
        "app/db/x.py",
        "conn.execute('ALTER TABLE documents NO FORCE ROW LEVEL SECURITY')\n",
        "ALTER is not SELECT, INSERT, UPDATE, DELETE or WITH",
    ),
    ("app/db/x.py", "conn.execute('  drop table chunks')\n", "DROP is not SELECT"),
    ("app/db/x.py", "conn.execute('TRUNCATE chunks')\n", "TRUNCATE is not SELECT"),
    ("app/db/x.py", "conn.execute('GRANT ALL ON chunks TO PUBLIC')\n", "GRANT is not SELECT"),
    ("app/db/x.py", "conn.execute('RESET ROLE')\n", "RESET is not SELECT"),
    ("app/db/x.py", "conn.execute('-- a note\\nDO $$ BEGIN END $$')\n", "DO is not SELECT"),
    ("app/db/x.py", "conn.execute('/* x */ CREATE TEMP TABLE c (x int)')\n", "CREATE is not"),
    ("app/db/x.py", "cur.executemany('COPY x FROM STDIN', rows)\n", "COPY is not SELECT"),
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
    ("app/db/x.py", "conn.execute(**statement)\n", ".execute() takes **kwargs"),
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


def test_a_queries_constant_that_is_not_dml_is_caught() -> None:
    # Review finding sec-1 of PR 7: the rule reads the constant's value too.
    sources = {
        QUERIES: 'Y = "ALTER TABLE documents DISABLE ROW LEVEL SECURITY"\nZ = "SELECT 1"\n',
        "app/db/x.py": (
            "from app.db import queries\nfrom app.db.queries import Y, Z\n"
            "conn.execute(Y)\nconn.execute(queries.Y)\nconn.execute(Z)\n"
        ),
    }
    _, problems = check(sources)
    assert [p for p in problems if "ALTER is not" in p] == [
        "app/db/x.py:3: ALTER is not SELECT, INSERT, UPDATE, DELETE or WITH",
        "app/db/x.py:4: ALTER is not SELECT, INSERT, UPDATE, DELETE or WITH",
    ], problems
    # Control: the migration runner composes DDL, and may.
    _, problems = check({QUERIES: CLEAN_QUERIES, MIGRATE: "conn.execute('ALTER TABLE x')\n"})
    assert problems == [], problems


HANDLE = (
    "class AdvisorConnection:\n"
    "    def execute(self, query, params=None):\n"
    "        return self._live().execute(query, params)\n"
)


def test_the_handles_delegation_is_the_one_exception() -> None:
    _, problems = check({QUERIES: CLEAN_QUERIES, SESSION: HANDLE})
    assert problems == [], problems
    assert len(delegations(ast.parse(HANDLE))) == 1


@pytest.mark.parametrize(
    ("rel", "source"),
    [
        ("app/db/x.py", HANDLE),
        (SESSION, HANDLE.replace("AdvisorConnection", "OtherHandle")),
        (SESSION, HANDLE.replace("def execute", "def run")),
        (
            SESSION,
            HANDLE.replace("        return", "        query = f'SELECT {query}'\n        return"),
        ),
        (SESSION, HANDLE.replace("execute(query, params)", "execute(params, query)")),
        # Security review sec-3: rebindings that are not a plain assignment.
        (
            SESSION,
            HANDLE.replace(
                "        return self._live().execute(query, params)\n",
                "        run = lambda query: self._live().execute(query, params)\n"
                "        return run(f'SELECT {params}')\n",
            ),
        ),
        (
            SESSION,
            HANDLE.replace(
                "        return self._live().execute(query, params)\n",
                "        def run(query):\n"
                "            return self._live().execute(query, params)\n"
                "        return run(f'SELECT {params}')\n",
            ),
        ),
        (
            SESSION,
            HANDLE.replace(
                "        return self._live().execute(query, params)\n",
                "        try:\n"
                "            raise ValueError(f'SELECT {params}')\n"
                "        except ValueError as query:\n"
                "            return self._live().execute(query, params)\n",
            ),
        ),
        (
            SESSION,
            HANDLE.replace(
                "        return self._live().execute(query, params)\n",
                "        match f'SELECT {params}':\n"
                "            case str() as query:\n"
                "                return self._live().execute(query, params)\n",
            ),
        ),
        (
            SESSION,
            HANDLE.replace(
                "        return self._live().execute(query, params)\n",
                "        from os import sep as query\n"
                "        return self._live().execute(query, params)\n",
            ),
        ),
        # A caller of the handle: a name, not a literal or a queries.py constant.
        (
            "app/harness/x.py",
            "with advisor_transaction(pool, a) as conn:\n    conn.execute(statement)\n",
        ),
    ],
    ids=[
        "another-file",
        "another-class",
        "another-method",
        "rebound",
        "another-argument",
        "lambda",
        "nested-def",
        "except-as",
        "case-as",
        "import-as",
        "caller",
    ],
)
def test_the_handles_exception_reaches_nothing_else(rel: str, source: str) -> None:
    _, problems = check({QUERIES: CLEAN_QUERIES, rel: source})
    assert any("takes Name" in problem for problem in problems), problems


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
