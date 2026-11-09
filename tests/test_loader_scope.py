"""Request code cannot reach the loader's owner path (ADR 0010; the M1 plan's question 2).

Timur's condition: the helper that lets the owner write for an advisor must be
unreachable from app/ request code, "with the same scan as for issue()"
(tests/test_identity.py). An AST scan of app/ reports:

- any use of owner_advisor_scope (a name, an attribute, an import, or a string) outside
  its definition in app/db/session.py and its one call in app/db/load.py, which imports
  it once, by name;
- any import of app.db.load anywhere in app/ (absolute, from app.db, relative, or as an
  attribute of app.db), and the module's dotted name in a string;
- any use of migration_database_url (a name, an attribute, a keyword, or a string, any
  case) outside app/config/settings.py, app/db/migrate.py and app/db/load.py, so request
  code never holds the migrator's login (PR 1 review sec-3);
- any string that says SET ROLE outside app/db/migrate.py, which composes the one
  statement the migrator and the loader run (PR 1 review sec-3);
- the attack-id prefix in a string outside app/db/load.py (PR 1 review sec-8);
- a call of OnnxEmbedder or load_tokenizer with any argument outside app/embeddings/:
  the files= and directory= seams are for tests (PR 4 review sec-7).

Docstrings are prose and are not read as strings. Like the identity scan, it reads names
and literals: getattr() with a string built at runtime, globals(), or a module imported
by a name built at runtime (importlib is banned by tests/test_import_allowlist.py) is not
caught. At runtime owner_advisor_scope() refuses every session but the migrator acting
as the owner (tests/db/test_loader.py), so the app role fails there as well.
"""

from __future__ import annotations

import ast
import re
from collections import Counter

import pytest
from repo_files import ROOT

from app.config.settings import env_name

APP = ROOT / "app"
SESSION = "app/db/session.py"
LOAD = "app/db/load.py"
MIGRATE = "app/db/migrate.py"
SETTINGS = "app/config/settings.py"
NAMES = "app/db/names.py"
SCOPE = "owner_advisor_scope"
LOADER = "app.db.load"
MIGRATOR_URL = "migration_database_url"
MIGRATOR_URL_FILES = frozenset({"app/config/settings.py", MIGRATE, LOAD})
SET_ROLE = re.compile(r"\bSET\s+ROLE\b", re.IGNORECASE)
# The prefix as a word start: "rt-" after a letter or digit (start-, port-) is not it.
PREFIX = re.compile(r"(?<![a-z0-9])rt-", re.IGNORECASE)
EMBEDDER_FACTORIES = frozenset({"OnnxEmbedder", "load_tokenizer"})
EMBEDDINGS = "app/embeddings/"


def docstrings(tree: ast.AST) -> set[int]:
    """The ids of the docstring nodes of the module, its classes and its functions."""
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                found.add(id(body[0].value))
    return found


def package_of(rel: str) -> list[str]:
    """app/db/x.py -> ["app", "db"]; app/db/__init__.py -> ["app", "db"]."""
    parts = rel.removesuffix(".py").split("/")
    return parts[:-1]


def absolute(node: ast.ImportFrom, rel: str) -> str:
    """The module an ImportFrom names, with a relative one resolved against rel."""
    if not node.level:
        return node.module or ""
    base = package_of(rel)
    base = base[: len(base) - (node.level - 1)] if node.level > 1 else base
    return ".".join(base + ([node.module] if node.module else []))


def is_loader(module: str) -> bool:
    return module == LOADER or module.startswith(LOADER + ".")


def scan(source: str, rel: str) -> tuple[list[str], Counter[str]]:
    """The rule breaks in one file, and what was seen, for the non-vacuity checks."""
    tree = ast.parse(source)
    prose = docstrings(tree)
    found: list[str] = []
    seen: Counter[str] = Counter()
    allowed: set[int] = set()  # nodes of the one sanctioned import and call in load.py
    db_names: set[str] = set()  # local names bound to the app.db package

    if rel == LOAD:
        imports = [
            alias
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module == "app.db.session"
            for alias in node.names
            if alias.name == SCOPE and alias.asname is None
        ]
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == SCOPE
        ]
        if len(imports) == 1:
            allowed.add(id(imports[0]))
        if len(calls) == 1:
            allowed.add(id(calls[0].func))
        seen["scope calls"] += len(calls)

    for node in ast.walk(tree):
        where = f"{rel}:{getattr(node, 'lineno', '?')}"
        # owner_advisor_scope.
        if isinstance(node, ast.FunctionDef) and node.name == SCOPE:
            if rel == SESSION:
                seen["scope definitions"] += 1
            else:
                found.append(f"{where}: defines {SCOPE} outside {SESSION}")
        named = isinstance(node, ast.alias) and SCOPE in (node.name, node.asname)
        if named and id(node) not in allowed:
            found.append(f"{where}: imports {SCOPE}")
        if isinstance(node, ast.Name) and node.id == SCOPE and id(node) not in allowed:
            found.append(f"{where}: {SCOPE} used")
        if isinstance(node, ast.Attribute) and node.attr == SCOPE:
            found.append(f"{where}: .{SCOPE}")

        # Imports of app.db.load.
        if isinstance(node, ast.Import):
            for alias in node.names:
                if is_loader(alias.name):
                    found.append(f"{where}: imports {alias.name}")
                if alias.name == "app.db" and alias.asname:
                    db_names.add(alias.asname)
        if isinstance(node, ast.ImportFrom):
            module = absolute(node, rel)
            if is_loader(module):
                found.append(f"{where}: imports from {module}")
            if module == "app.db":
                for alias in node.names:
                    if alias.name == "load":
                        found.append(f"{where}: imports {LOADER}")
            if module == "app":
                db_names |= {
                    alias.asname or alias.name for alias in node.names if alias.name == "db"
                }

        # Keywords and attributes that name the migrator's URL.
        keyword = isinstance(node, ast.keyword) and (node.arg or "").lower() == MIGRATOR_URL
        if keyword and rel not in MIGRATOR_URL_FILES:
            found.append(f"{where}: {MIGRATOR_URL}=")
        if (
            isinstance(node, ast.Attribute | ast.Name)
            and (node.attr if isinstance(node, ast.Attribute) else node.id).lower() == MIGRATOR_URL
        ):
            if rel in MIGRATOR_URL_FILES:
                seen[f"{MIGRATOR_URL} in {rel}"] += 1
            else:
                found.append(f"{where}: {MIGRATOR_URL} used")

        # The calls of the embedder's factories.
        if isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name in EMBEDDER_FACTORIES and not rel.startswith(EMBEDDINGS):
                if node.args or node.keywords:
                    found.append(f"{where}: {name}() with arguments")
                else:
                    seen[f"{name}() in {rel}"] += 1

        # Strings, docstrings aside.
        if isinstance(node, ast.Constant) and isinstance(node.value, str | bytes):
            if id(node) in prose:
                continue
            text = node.value if isinstance(node.value, str) else node.value.decode("latin-1")
            if SCOPE in text:
                found.append(f"{where}: a string names {SCOPE}")
            if LOADER in text and rel != LOAD:  # load.py's own argparse prog
                found.append(f"{where}: a string names {LOADER}")
            if MIGRATOR_URL in text.lower():
                if rel in MIGRATOR_URL_FILES:
                    seen[f"{MIGRATOR_URL} in {rel}"] += 1
                else:
                    found.append(f"{where}: a string names {MIGRATOR_URL}")
            if SET_ROLE.search(text):
                if rel == MIGRATE:
                    seen["SET ROLE in migrate.py"] += 1
                else:
                    found.append(f"{where}: a string says SET ROLE")
            if PREFIX.search(text):
                if rel == LOAD:
                    seen["prefix in load.py"] += 1
                else:
                    found.append(f"{where}: a string holds the attack-id prefix")

    # app.db.load reached as an attribute of the package: app.db.load, or db.load.
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "load":
            value = node.value
            if (isinstance(value, ast.Name) and value.id in db_names) or (
                isinstance(value, ast.Attribute)
                and value.attr == "db"
                and isinstance(value.value, ast.Name)
                and value.value.id == "app"
            ):
                found.append(f"{rel}:{node.lineno}: {LOADER} as an attribute")
    return found, seen


def check(sources: dict[str, str]) -> tuple[list[str], Counter[str]]:
    found: list[str] = []
    seen: Counter[str] = Counter()
    for rel, source in sorted(sources.items()):
        problems, counts = scan(source, rel)
        found += problems
        seen += counts
    return found, seen


def app_sources() -> dict[str, str]:
    return {
        path.relative_to(ROOT).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(APP.rglob("*.py"))
    }


def test_the_owner_path_is_unreachable_from_request_code() -> None:
    sources = app_sources()
    for required in (SESSION, LOAD, MIGRATE, "app/config/settings.py", "app/main.py"):
        assert required in sources, f"{required} was not scanned"
    found, seen = check(sources)
    assert not found, "\n".join(found)
    # Non-vacuity: the scan sees the definition, the one call, and every sanctioned use.
    assert seen["scope definitions"] == 1, seen
    assert seen["scope calls"] == 1, seen
    for rel in sorted(MIGRATOR_URL_FILES):
        assert seen[f"{MIGRATOR_URL} in {rel}"] >= 1, (rel, seen)
    assert seen["SET ROLE in migrate.py"] >= 1, seen
    assert seen["prefix in load.py"] >= 1, seen
    assert seen[f"OnnxEmbedder() in {LOAD}"] == 1, seen
    # Review finding sec-1: the owner path's other names, each where it belongs.
    assert seen[f"become_owner in {MIGRATE}"] >= 2, seen  # the definition and run()'s call
    assert seen[f"become_owner in {LOAD}"] >= 2, seen  # the import and run()'s call
    assert seen[f"loopback_database_url in {SETTINGS}"] >= 3, seen
    for rel in (SESSION, MIGRATE, SETTINGS):
        assert seen[f"MIGRATOR_ROLE in {rel}"] >= 1, (rel, seen)
    assert seen[f"connect() import in {LOAD}"] == 1 and seen[f"connect() import in {MIGRATE}"] == 1
    assert seen[f"app.db.migrate import in {LOAD}"] == 1, seen


# Review finding sec-1: the migrator's login and the owner, reached without one name the
# first version of the scan looked for.
OWNER_ROUTE = (
    "from app.config.settings import loopback_database_url\n"
    "from app.db import names\n"
    "from app.db.connect import connect\n"
    "from app.db.migrate import become_owner\n"
    "with connect(loopback_database_url(names.MIGRATOR_ROLE), autocommit=True) as conn:\n"
    "    become_owner(conn)\n"
)

# The plan's planted shapes first, then the rest of the rules.
PLANTED = [
    # Review finding sec-1, line by line, and as a whole.
    ("app/harness/x.py", OWNER_ROUTE, "imports become_owner"),
    ("app/api/x.py", "from app.db.migrate import become_owner\n", "imports become_owner"),
    ("app/api/x.py", "from app.db.migrate import run\n", "imports app.db.migrate"),
    ("app/api/x.py", "import app.db.migrate\n", "imports app.db.migrate"),
    ("app/api/x.py", "from app.db import migrate\n", "imports app.db.migrate"),
    ("app/db/x.py", "from . import migrate\n", "imports app.db.migrate"),
    ("app/api/x.py", "migrate.become_owner(conn)\n", ".become_owner"),
    ("app/api/x.py", "become_owner(conn)\n", "become_owner used"),
    ("app/api/x.py", "from app.db.connect import connect\n", "imports connect from app.db.connect"),
    ("app/db/session.py", "from app.db.connect import connect as c\n", "imports connect from"),
    (
        "app/api/x.py",
        "from app.config.settings import loopback_database_url\n",
        "imports loopback_database_url",
    ),
    ("app/api/x.py", "settings.loopback_database_url(role)\n", ".loopback_database_url"),
    ("app/api/x.py", "names.MIGRATOR_ROLE\n", ".MIGRATOR_ROLE"),
    ("app/api/x.py", "from app.db.names import OWNER_ROLE\n", "imports OWNER_ROLE"),
    ("app/db/load.py", "names.OWNER_ROLE\n", ".OWNER_ROLE"),
    ("app/api/x.py", "from app.db.session import owner_advisor_scope\n", "imports owner"),
    (
        "app/api/x.py",
        "from app.db import session\nsession.owner_advisor_scope(c, 'a')\n",
        ".owner_advisor_scope",
    ),
    ("app/api/x.py", "import app.db.session as s\ns.owner_advisor_scope\n", ".owner_advisor_scope"),
    ("app/api/x.py", "from app.db import load\n", "imports app.db.load"),
    ("app/api/x.py", "import app.db.load\n", "imports app.db.load"),
    ("app/api/x.py", "import app.db.load as l\n", "imports app.db.load"),
    ("app/api/x.py", "from app.db.load import write\n", "imports from app.db.load"),
    ("app/db/x.py", "from . import load\n", "imports app.db.load"),
    ("app/db/x.py", "from .load import run\n", "imports from app.db.load"),
    ("app/api/x.py", "from ..db import load\n", "imports app.db.load"),
    ("app/api/x.py", "from app import db\ndb.load.main()\n", "app.db.load as an attribute"),
    ("app/api/x.py", "import app.db\napp.db.load.main()\n", "app.db.load as an attribute"),
    ("app/api/x.py", "import app.db as d\nd.load\n", "app.db.load as an attribute"),
    ("app/api/x.py", "x = 'app.db.load'\n", "a string names app.db.load"),
    ("app/api/x.py", "from app.db.session import owner_advisor_scope as o\n", "imports owner"),
    ("app/api/x.py", "getattr(session, 'owner_advisor_scope')\n", "a string names owner"),
    ("app/api/x.py", "def owner_advisor_scope():\n    pass\n", "defines owner_advisor_scope"),
    # In load.py: a second call, a second import under another name, a reference.
    (
        LOAD,
        (
            "from app.db.session import owner_advisor_scope\n"
            "owner_advisor_scope(c, 'a')\nowner_advisor_scope(c, 'b')\n"
        ),
        "owner_advisor_scope used",
    ),
    (LOAD, "from app.db.session import owner_advisor_scope as scope\n", "imports owner"),
    (LOAD, "from app.db import session\nf = session.owner_advisor_scope\n", ".owner_advisor"),
    (
        LOAD,
        "from app.db.session import owner_advisor_scope\nf = owner_advisor_scope\nf(c, 'a')\n",
        "owner_advisor_scope used",
    ),
    # In session.py: anything but the definition.
    (SESSION, "def owner_advisor_scope():\n    pass\nalias = owner_advisor_scope\n", "used"),
    # The migrator's URL.
    ("app/api/x.py", "Settings().migration_database_url\n", "migration_database_url used"),
    ("app/api/x.py", f"settings.{MIGRATOR_URL.upper()}\n", "migration_database_url used"),
    ("app/api/x.py", "getattr(s, 'migration_database_url')\n", "a string names migration"),
    (
        "app/api/x.py",
        f"os.environ[{env_name('migration_database_url')!r}]\n",
        "a string names migration",
    ),
    ("app/api/x.py", "Settings(migration_database_url=u)\n", "migration_database_url="),
    ("app/db/session.py", "url = settings.migration_database_url\n", "migration_database_url"),
    # SET ROLE.
    ("app/db/x.py", "conn.execute('SET ROLE owner')\n", "a string says SET ROLE"),
    ("app/db/x.py", "conn.execute('set   role x')\n", "a string says SET ROLE"),
    (LOAD, "conn.execute(f'SET ROLE {owner}')\n", "a string says SET ROLE"),
    (SESSION, "x = b'SET ROLE x'\n", "a string says SET ROLE"),
    # The attack-id prefix.
    ("app/harness/x.py", "PREFIX = 'rt-'\n", "attack-id prefix"),
    ("app/harness/x.py", "x = f'RT-{n}'\n", "attack-id prefix"),
    ("app/harness/x.py", "re.compile('^rt-')\n", "attack-id prefix"),
    # The embedder's seams.
    ("app/harness/x.py", "OnnxEmbedder(directory=d)\n", "OnnxEmbedder() with arguments"),
    ("app/harness/x.py", "onnx.OnnxEmbedder(files=f)\n", "OnnxEmbedder() with arguments"),
    ("app/harness/x.py", "OnnxEmbedder(path)\n", "OnnxEmbedder() with arguments"),
    ("app/harness/x.py", "OnnxEmbedder(**options)\n", "OnnxEmbedder() with arguments"),
    ("app/harness/x.py", "load_tokenizer(files=f)\n", "load_tokenizer() with arguments"),
]


@pytest.mark.parametrize(("rel", "source", "needle"), PLANTED)
def test_planted_uses_are_caught(rel: str, source: str, needle: str) -> None:
    found, _ = scan(source, rel)
    assert any(needle in item for item in found), found


def test_the_sanctioned_uses_pass() -> None:
    load = (
        '"""The loader. It calls owner_advisor_scope() and says SET ROLE in prose."""\n'
        "from app.db.session import owner_advisor_scope\n"
        "from app.db.connect import CheckedConnection, connect\n"
        "from app.db.migrate import MigrationError, become_owner\n"
        "from app.embeddings.onnx import OnnxEmbedder\n"
        "PREFIX = 'rt-'\n"
        "def run(url):\n"
        "    with connect(url, autocommit=True) as conn:\n"
        "        become_owner(conn)\n"
        "def write(conn, advisor):\n"
        "    with owner_advisor_scope(conn, advisor):\n"
        "        pass\n"
        "url = Settings().migration_database_url\n"
        "embedder = OnnxEmbedder()\n"
    )
    found, seen = scan(load, LOAD)
    assert found == [], found
    assert seen["scope calls"] == 1 and seen["prefix in load.py"] == 1, seen
    session = (
        '"""The pool; owner_advisor_scope() for the loader, after SET ROLE."""\n'
        "def owner_advisor_scope(conn, advisor_id):\n"
        '    """Refuses unless the session is the migrator after SET ROLE."""\n'
    )
    assert scan(session, SESSION) == ([], Counter({"scope definitions": 1}))
    # Near misses: words with rt- inside, another .load, the embedder module's own calls.
    near = (
        "x = 'start-up, export-ready, port-forward'\n"
        "json.load(f)\nyaml.load(s)\n"
        "from app import config\nconfig.load\n"
    )
    assert scan(near, "app/api/x.py")[0] == []
    assert scan("ModelTokenizer(read_verified(d, t, files))\n", "app/embeddings/onnx.py")[0] == []
    assert scan("OnnxEmbedder(files=f)\n", "app/embeddings/onnx.py")[0] == []
    migrate = "SET_OWNER = sql.SQL('SET ROLE {}')\nurl = s.migration_database_url\n"
    assert scan(migrate, MIGRATE)[0] == []
