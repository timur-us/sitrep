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
        "collections",
        "dataclasses",
        "enum",
        "fastapi",
        "html",
        "logging",
        "opentelemetry",
        "pathlib",
        "pydantic",
        "pydantic_settings",
        "starlette",
        "sys",
        "types",
        "uvicorn",
    }
)

# Never reviewed in: clients, sockets, programs, dynamic code and deserialisation.
BANNED_MODULES = frozenset(
    {
        "aiohttp",
        "ctypes",
        "ftplib",
        "http",
        "httpx",
        "importlib",
        "marshal",
        "multiprocessing",
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
# Attribute names that open a connection or import by name, on any object.
BANNED_ATTRIBUTES = frozenset(
    {
        "create_connection",
        "create_datagram_endpoint",
        "create_unix_connection",
        "import_module",
        "open_connection",
        "open_unix_connection",
        "sock_connect",
    }
)
# On the os module only: running or replacing a program.
OS_PROGRAM = re.compile(r"system|popen|startfile|fork\w*|exec\w*|spawn\w*|posix_spawn\w*")


def scan(source: str, rel: str) -> tuple[set[str], list[str]]:
    """The top-level modules a file imports, and its banned uses."""
    modules: set[str] = set()
    problems: list[str] = []
    os_names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.partition(".")[0]
                modules.add(top)
                if top == "os":
                    os_names.add(alias.asname or "os")
        elif isinstance(node, ast.ImportFrom):
            top = "app" if node.level else (node.module or "").partition(".")[0]
            modules.add(top)
            for alias in node.names:
                if alias.name in BANNED_ATTRIBUTES or (
                    node.module == "os" and OS_PROGRAM.fullmatch(alias.name)
                ):
                    problems.append(f"{rel}:{node.lineno}: from {node.module} import {alias.name}")
        elif isinstance(node, ast.Attribute):
            on_os = isinstance(node.value, ast.Name) and node.value.id in os_names
            if node.attr in BANNED_ATTRIBUTES or (on_os and OS_PROGRAM.fullmatch(node.attr)):
                problems.append(f"{rel}:{node.lineno}: .{node.attr}")
        elif isinstance(node, ast.Name) and node.id in BANNED_NAMES:
            problems.append(f"{rel}:{node.lineno}: {node.id}")
    problems += [f"{rel}: banned module {module}" for module in sorted(modules & BANNED_MODULES)]
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
    for required in ("app/main.py", "app/__main__.py", "app/observability/tracing.py"):
        assert required in sources, f"{required} was not scanned"
    modules, problems = check(sources)
    assert not problems, "\n".join(problems)
    assert modules == ALLOWED, f"not imported, drop from the list: {sorted(ALLOWED - modules)}"
    assert not ALLOWED & BANNED_MODULES, sorted(ALLOWED & BANNED_MODULES)


PLANTED = [
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
    )
    modules, problems = scan(source, "app/clean.py")
    assert problems == [], problems
    assert modules == {"logging", "re", "os", "app"}
