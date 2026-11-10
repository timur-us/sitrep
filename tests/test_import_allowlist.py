"""app/ imports only reviewed modules, and nothing that connects, imports by name or runs
a program.

"No endpoint that fetches or redirects to a URL" (threat model) starts here: no HTTP
client, socket or subprocess module can enter app/ without changing this file. The scan
reads the syntax tree, so it sees static names only: getattr() with a string, or a
reviewed library that connects on its own, is not caught.
"""

from __future__ import annotations

import ast
import re

import pytest
from repo_files import ROOT

APP = ROOT / "app"

# Top-level names of every module app/ imports. Equality, so an unused entry fails too.
ALLOWED = frozenset(
    {
        "__future__",
        "app",
        "argparse",
        # app/main.py sets the loop's exception handler (M1 PR 5); the connecting and
        # serving calls of asyncio stay banned below.
        "asyncio",
        "collections",
        "contextlib",
        "dataclasses",
        # app/harness/mvp_tools.py: the follow-up's due date. No clock is read in app/.
        "datetime",
        "enum",
        "errno",
        "fastapi",
        "hashlib",
        # app/auth/dev_shim.py signs the session cookie (ADR 0009).
        "hmac",
        "html",
        "ipaddress",
        "json",
        "logging",
        # The embedder's pooling (ADR 0005), in app/embeddings/onnx.py only.
        "numpy",
        # The model runtime (ADR 0005), in app/embeddings/onnx.py only: it loads native
        # code and model graphs, so the file pins CPUExecutionProvider and the ONNX format
        # and loads bytes that model_files.py checked. register_custom_ops_library is banned.
        "onnxruntime",
        "opentelemetry",
        # app/db/connect.py reads os.environ; os.system, os.exec* and the like stay banned.
        "os",
        "pathlib",
        # The database driver (ADR 0008). It connects on its own, so this scan does not
        # see where; app/db/connect.py is the only place that opens a connection.
        "psycopg",
        # The connection pool (ADR 0008), in app/db/session.py only. It opens connections
        # itself, through the connection class session.py gives it (tests/db/test_pool.py).
        "psycopg_pool",
        "pydantic",
        "pydantic_settings",
        "re",
        "secrets",
        # app/embeddings/model_files.py: S_ISREG on an open descriptor.
        "stat",
        "starlette",
        "sys",
        # app/observability/logs.py replaces threading.excepthook (M1 PR 5); no thread is
        # started in app/.
        "threading",
        # The model's tokenizer (ADR 0005), in app/embeddings/onnx.py only. It can reach
        # the network through Tokenizer.from_pretrained(), which is banned below; the lock
        # leaves out the Hub client it would call (tests/test_lock.py).
        "tokenizers",
        "tomllib",
        "types",
        "typing",
        "unicodedata",
        # app/observability/logs.py replaces warnings.showwarning (M1 PR 5, review finding
        # sec-1): a warning's text can quote a value.
        "warnings",
        "uvicorn",
    }
)

# Never reviewed in: clients, sockets, programs, dynamic code and deserialisation.
BANNED_MODULES = frozenset(
    {
        "aiohttp",
        "asyncio.subprocess",
        "ctypes",
        "ftplib",
        "http",
        "httpx",
        "importlib",
        # Submodules of reviewed packages that ship data away: log handlers for HTTP,
        # sockets, syslog and mail, and OpenTelemetry's network exporters.
        "logging.handlers",
        "marshal",
        "multiprocessing",
        "opentelemetry.exporter",
        "pickle",
        "requests",
        "smtplib",
        "socket",
        "ssl",
        "subprocess",
        "urllib",
        "webbrowser",
        "xmlrpc",
    }
)
BANNED_NAMES = frozenset({"__import__", "compile", "eval", "exec"})
# Names that connect, serve, run a program, import by name or redirect, on any object
# or in a from-import. RedirectResponse: the app has no endpoint that redirects.
# from_pretrained downloads a tokenizer from the Hub, and from_file reads one past
# read_verified(); register_custom_ops_library and register_execution_provider_library
# load native code into onnxruntime; add_external_initializers* feed it tensors from
# outside the checked model; enable_profiling, profile_file_prefix and
# optimized_model_filepath make it write files (ADR 0005; review finding sec-8).
BANNED_ATTRIBUTES = frozenset(
    {
        "RedirectResponse",
        "create_connection",
        "create_datagram_endpoint",
        "create_server",
        "create_subprocess_exec",
        "create_subprocess_shell",
        "create_unix_connection",
        # asyncio's own lookup, servers, programs, pipes and raw sends (review finding
        # sec-2 of M1 PR 5): asyncio is reviewed in for the loop's exception handler only.
        "connect_read_pipe",
        "connect_write_pipe",
        "create_unix_server",
        "getaddrinfo",
        "getnameinfo",
        "sock_sendall",
        "sock_sendfile",
        "sock_sendto",
        "start_unix_server",
        "subprocess_exec",
        "subprocess_shell",
        "to_thread",
        "add_external_initializers",
        "add_external_initializers_from_files_in_memory",
        "enable_profiling",
        "from_file",
        "from_pretrained",
        "import_module",
        "open_connection",
        "open_unix_connection",
        "optimized_model_filepath",
        "profile_file_prefix",
        "register_custom_ops_library",
        "register_execution_provider_library",
        "sock_connect",
        "start_server",
        "urlopen",
    }
)
# Opening a database connection: only here, where the URL is checked and libpq's
# environment overrides are pinned (ADR 0008).
DB_CONNECT_FILE = "app/db/connect.py"
# Building a pool: only here, where its connections go through app/db/connect.py and
# the guard (review finding test-3 of PR 5b).
POOL_FILE = "app/db/session.py"
# The embedder: the one file that loads a model, from checked bytes (ADR 0005).
EMBEDDER_FILE = "app/embeddings/onnx.py"
# Modules that one file alone may import.
SINGLE_FILE = {
    # The loop's exception handler, set in the lifespan; the hooks (M1 PR 5).
    "asyncio": "app/main.py",
    "threading": "app/observability/logs.py",
    "warnings": "app/observability/logs.py",
    "psycopg_pool": POOL_FILE,
    "numpy": EMBEDDER_FILE,
    "onnxruntime": EMBEDDER_FILE,
    "tokenizers": EMBEDDER_FILE,
}
# On the os module only: running or replacing a program.
OS_PROGRAM = re.compile(r"system|popen|startfile|fork\w*|exec\w*|spawn\w*|posix_spawn\w*")


def banned_module(name: str) -> str | None:
    """The BANNED_MODULES entry that name is, or lies under."""
    return next((b for b in BANNED_MODULES if name == b or name.startswith(b + ".")), None)


def scan(source: str, rel: str) -> tuple[set[str], list[str]]:
    """The top-level modules a file imports, and its banned uses."""
    modules: set[str] = set()
    dotted: set[str] = set()  # full module names, and module.name for from-imports
    problems: list[str] = []
    os_names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.partition(".")[0]
                modules.add(top)
                dotted.add(alias.name)
                if top == "os":
                    os_names.add(alias.asname or "os")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            top = "app" if node.level else module.partition(".")[0]
            modules.add(top)
            if not node.level:
                dotted.add(module)
                dotted.update(f"{module}.{alias.name}" for alias in node.names)
            for alias in node.names:
                if (
                    alias.name == "connect"
                    and module.startswith("psycopg")
                    and rel != DB_CONNECT_FILE
                ):
                    problems.append(f"{rel}:{node.lineno}: from {module} import connect")
                if alias.name in BANNED_ATTRIBUTES or (
                    module == "os" and OS_PROGRAM.fullmatch(alias.name)
                ):
                    problems.append(f"{rel}:{node.lineno}: from {module} import {alias.name}")
        elif isinstance(node, ast.Attribute):
            on_os = isinstance(node.value, ast.Name) and node.value.id in os_names
            if node.attr in BANNED_ATTRIBUTES or (on_os and OS_PROGRAM.fullmatch(node.attr)):
                problems.append(f"{rel}:{node.lineno}: .{node.attr}")
            if node.attr == "connect" and rel != DB_CONNECT_FILE:
                problems.append(f"{rel}:{node.lineno}: .connect outside {DB_CONNECT_FILE}")
        elif isinstance(node, ast.Name) and node.id in BANNED_NAMES:
            problems.append(f"{rel}:{node.lineno}: {node.id}")
    for module, only in sorted(SINGLE_FILE.items()):
        if module in modules and rel != only:
            problems.append(f"{rel}: {module} outside {only}")
    banned = {entry for name in dotted if (entry := banned_module(name))}
    problems += [f"{rel}: banned module {entry}" for entry in sorted(banned)]
    return modules, problems


def check(sources: dict[str, str]) -> tuple[set[str], list[str]]:
    """Scan every file; a module outside ALLOWED is a problem too."""
    modules: set[str] = set()
    problems: list[str] = []
    for rel, source in sorted(sources.items()):
        found, banned = scan(source, rel)
        modules |= found
        problems += banned
        problems += [f"{rel}: unreviewed import {module}" for module in sorted(found - ALLOWED)]
    return modules, problems


def test_import_allowlist() -> None:
    sources = {
        path.relative_to(ROOT).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(APP.rglob("*.py"))
    }
    assert len(sources) >= 10, f"only {len(sources)} files scanned"
    for required in (
        "app/main.py",
        "app/__main__.py",
        "app/observability/tracing.py",
        "app/db/connect.py",
        EMBEDDER_FILE,
    ):
        assert required in sources, f"{required} was not scanned"
    modules, problems = check(sources)
    assert not problems, "\n".join(problems)
    assert modules == ALLOWED, f"not imported, drop from the list: {sorted(ALLOWED - modules)}"
    # A banned name may sit under a reviewed package (logging.handlers), not be one.
    assert not ALLOWED & BANNED_MODULES, sorted(ALLOWED & BANNED_MODULES)


PLANTED = [
    ("import socket\n", "banned module socket"),
    ("import logging.handlers\n", "banned module logging.handlers"),
    ("from logging.handlers import HTTPHandler\n", "banned module logging.handlers"),
    ("from logging import handlers\n", "banned module logging.handlers"),
    ("from opentelemetry.exporter.otlp import x\n", "banned module opentelemetry.exporter"),
    ("from fastapi.responses import RedirectResponse\n", "import RedirectResponse"),
    ("import starlette.responses as r\nr.RedirectResponse('/')\n", ".RedirectResponse"),
    ("from os import system\n", "from os import system"),
    ("import builtins\nbuiltins.__import__('socket')\n", "unreviewed import builtins"),
    ("import httpx\n", "unreviewed import httpx"),
    ("import httpx\n", "banned module httpx"),
    ("import urllib.request as r\n", "banned module urllib"),
    ("from http import client\n", "banned module http"),
    ('import importlib\nimportlib.import_module("requests")\n', ".import_module"),
    ('import importlib\nimportlib.import_module("requests")\n', "banned module importlib"),
    ('__import__("socket")\n', ":1: __import__"),
    ("import asyncio\nasyncio.open_connection('h', 1)\n", ".open_connection"),
    ("from asyncio import open_connection\n", "import open_connection"),
    ("import subprocess\n", "banned module subprocess"),
    ("import os\nos.system('x')\n", ".system"),
    ("import os as o\no.execv('x', ['x'])\n", ".execv"),
    ("from os import popen\n", "from os import popen"),
    ("eval('1')\n", ":1: eval"),
    ("import psycopg\npsycopg.connect('x')\n", ".connect outside app/db/connect.py"),
    ("from psycopg import connect\n", "from psycopg import connect"),
    ("import psycopg\npsycopg.Connection.connect('x')\n", ".connect outside"),
    # Review finding test-3 / sec-2: a pool built elsewhere skips CheckedConnection.
    ("from psycopg_pool import ConnectionPool\n", "psycopg_pool outside app/db/session.py"),
    ("import psycopg_pool\n", "psycopg_pool outside app/db/session.py"),
    ("from psycopg_pool import NullConnectionPool as N\n", "psycopg_pool outside"),
    # The embedder's libraries, outside app/embeddings/onnx.py (ADR 0005).
    ("import onnxruntime as ort\n", "onnxruntime outside app/embeddings/onnx.py"),
    ("from onnxruntime import InferenceSession\n", "onnxruntime outside"),
    ("import tokenizers\n", "tokenizers outside app/embeddings/onnx.py"),
    ("from tokenizers import Tokenizer\n", "tokenizers outside"),
    ("import numpy as np\n", "numpy outside app/embeddings/onnx.py"),
    # Their network and native-code paths, anywhere.
    ("Tokenizer.from_pretrained('org/model')\n", ".from_pretrained"),
    ("from transformers import from_pretrained\n", "import from_pretrained"),
    ("options.register_custom_ops_library('libx.so')\n", ".register_custom_ops_library"),
    # Review finding sec-8: native code, outside tensors, written files, unchecked reads.
    ("ort.register_execution_provider_library('x', 'libx.so')\n", ".register_execution_provider"),
    ("options.add_external_initializers(names, values)\n", ".add_external_initializers"),
    ("o.add_external_initializers_from_files_in_memory(a, b, c)\n", ".add_external_initializers_"),
    ("options.enable_profiling = True\n", ".enable_profiling"),
    ("options.profile_file_prefix = 'p'\n", ".profile_file_prefix"),
    ("options.optimized_model_filepath = 'm.onnx'\n", ".optimized_model_filepath"),
    ("tokenizers.Tokenizer.from_file('tokenizer.json')\n", ".from_file"),
    # Review finding sec-2 of M1 PR 5: asyncio's other ways out, and one file each.
    ("import asyncio\nasyncio.get_running_loop().getaddrinfo('x', 1)\n", ".getaddrinfo"),
    ("loop.subprocess_exec(p, 'x')\n", ".subprocess_exec"),
    ("loop.subprocess_shell(p, 'x')\n", ".subprocess_shell"),
    ("asyncio.start_unix_server(h, 'p')\n", ".start_unix_server"),
    ("loop.create_unix_server(f, 'p')\n", ".create_unix_server"),
    ("loop.connect_write_pipe(p, f)\n", ".connect_write_pipe"),
    ("loop.sock_sendto(s, b'x', a)\n", ".sock_sendto"),
    ("loop.sock_sendall(s, b'x')\n", ".sock_sendall"),
    ("asyncio.to_thread(f)\n", ".to_thread"),
    ("import asyncio.subprocess\n", "banned module asyncio.subprocess"),
    ("from asyncio import subprocess\n", "banned module asyncio.subprocess"),
    ("import asyncio\n", "asyncio outside app/main.py"),
    ("import threading\n", "threading outside app/observability/logs.py"),
]


@pytest.mark.parametrize(("source", "needle"), PLANTED)
def test_planted_imports_are_caught(source: str, needle: str) -> None:
    _, problems = check({"app/planted.py": source})
    assert any(needle in problem for problem in problems), problems


def test_clean_code_is_not_reported() -> None:
    # Near misses: a method named execute, re.compile, an os attribute that runs nothing.
    source = (
        "import logging\nimport re\nimport os\nfrom app.config import branding\n"
        "cursor.execute('select 1')\nre.compile('x')\nos.environ.get('X')\nos.path.join('a')\n"
        "from logging import Filter\nimport logging.config\nfrom fastapi.responses import Response\n"
        "from opentelemetry.sdk.trace import TracerProvider\n"
    )
    modules, problems = scan(source, "app/clean.py")
    assert problems == [], problems
    assert modules == {"logging", "re", "os", "app", "fastapi", "opentelemetry"}


def test_the_pool_is_allowed_in_its_own_file() -> None:
    assert scan("from psycopg_pool import ConnectionPool\n", "app/db/session.py")[1] == []


def test_the_embedder_libraries_are_allowed_in_their_own_file() -> None:
    source = "import numpy as np\nimport onnxruntime as ort\nimport tokenizers\n"
    assert scan(source, "app/embeddings/onnx.py")[1] == []
    assert scan(source, "app/embeddings/chunking.py")[1] != []
    # The ban on from_pretrained holds there too.
    assert scan("tokenizers.Tokenizer.from_pretrained('x')\n", "app/embeddings/onnx.py")[1] != []


def test_connect_is_allowed_in_its_own_file() -> None:
    source = "import psycopg\npsycopg.connect('x')\n"
    assert scan(source, "app/db/connect.py")[1] == []
    assert scan(source, "app/db/session.py")[1] != []
