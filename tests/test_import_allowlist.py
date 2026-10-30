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
        "collections",
        "dataclasses",
        "enum",
        "errno",
        "fastapi",
        "html",
        "ipaddress",
        "json",
        "logging",
        "opentelemetry",
        "pathlib",
        "pydantic",
        "pydantic_settings",
        "re",
        "secrets",
        "starlette",
        "sys",
        "tomllib",
        "types",
        "unicodedata",
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
BANNED_ATTRIBUTES = frozenset(
    {
        "RedirectResponse",
        "create_connection",
        "create_datagram_endpoint",
        "create_server",
        "create_subprocess_exec",
        "create_subprocess_shell",
        "create_unix_connection",
        "import_module",
        "open_connection",
        "open_unix_connection",
        "sock_connect",
        "start_server",
        "urlopen",
    }
)
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
                if alias.name in BANNED_ATTRIBUTES or (
                    module == "os" and OS_PROGRAM.fullmatch(alias.name)
                ):
                    problems.append(f"{rel}:{node.lineno}: from {module} import {alias.name}")
        elif isinstance(node, ast.Attribute):
            on_os = isinstance(node.value, ast.Name) and node.value.id in os_names
            if node.attr in BANNED_ATTRIBUTES or (on_os and OS_PROGRAM.fullmatch(node.attr)):
                problems.append(f"{rel}:{node.lineno}: .{node.attr}")
        elif isinstance(node, ast.Name) and node.id in BANNED_NAMES:
            problems.append(f"{rel}:{node.lineno}: {node.id}")
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
    for required in ("app/main.py", "app/__main__.py", "app/observability/tracing.py"):
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
