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
        # The model client (ADR 0012), in app/harness/model_client.py only. It connects on
        # its own, to the one base URL that file passes; anthropic.lib (Bedrock, Vertex,
        # Foundry, the tool and environment runners) is banned below.
        "anthropic",
        "app",
        "argparse",
        # app/main.py sets the loop's exception handler (M1 PR 5), and the live recorder
        # drives the app over ASGI in process (app/runners/live.py, M1 PR 16); the
        # connecting and serving calls of asyncio stay banned below.
        "asyncio",
        "collections",
        "contextlib",
        # app/db/load.py: the generator's ORIGIN, trust_for, LANGS and paths
        # (data/synthetic/generate.py, standard library and branding only; ADR 0010).
        # app/runners/scoring.py: which advisor owns each record, for the eval scorer
        # (ADR 0014). The image carries no data/, and no request path imports either.
        "data",
        "dataclasses",
        # app/harness/model_client.py: the cost, exact to the table's cents; the number
        # guard's figures (app/harness/grounding.py, app/harness/context.py).
        "decimal",
        # app/harness/mvp_tools.py: the follow-up's due date; the number guard's dates
        # (app/harness/grounding.py); the prompt's date (app/harness/prompt.py). The date
        # is read from the clock in two files only (CLOCK_FILES, M1 PR 16).
        "datetime",
        "enum",
        "errno",
        "fastapi",
        "hashlib",
        # app/auth/dev_shim.py signs the session cookie (ADR 0009).
        "hmac",
        "html",
        # The model client's HTTP library (ADR 0012), in app/harness/model_client.py only:
        # the file builds the client itself, with trust_env off, because the SDK's
        # DefaultHttpxClient mounts proxies from the environment whatever trust_env says.
        "httpx2",
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
        # app/api/ask.py reads time.monotonic for the route's deadline and the rate limit
        # per advisor (M1 PR 12; Timur's answer 1 on #33: the loop stays without a clock).
        # Nothing else in app/ reads a clock, and nothing sleeps (`sleep`, banned below).
        "time",
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
        # The SDK's other clients and runners: Bedrock, Vertex, Foundry, AWS, the tool
        # runner's toolset and the environments worker (ADR 0012).
        "anthropic.lib",
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
        # Threads and executors anywhere, and the sends that need a socket (review
        # finding sec-r6 of M1 PR 5): logs.py replaces the thread hook, it starts none.
        "Thread",
        "Timer",
        # No request waits on a timer (M1 PR 12): a backoff is not built, and the route's
        # deadline only compares the clock.
        "sleep",
        "connect_accepted_socket",
        "run_in_executor",
        "sendfile",
        "start_new_thread",
        "start_tls",
        "add_external_initializers",
        # The SDK's HTTP clients read proxy variables from the environment even with
        # trust_env off (ADR 0012).
        "DefaultAioHttpClient",
        "DefaultAsyncHttpxClient",
        "DefaultHttpxClient",
        # The SDK's other clients and helpers, which anthropic re-exports at its top level,
        # outside anthropic.lib: Bedrock, Vertex, Foundry, AWS, Google Cloud, the tool
        # runner's decorator and the refusal-fallback middleware. And the paths that leave
        # the factory's configuration: the beta namespace and a client copied with other
        # options (ADR 0012; review finding sec-5).
        "AnthropicAWS",
        "AnthropicBedrock",
        "AnthropicFoundry",
        "AnthropicGoogleCloud",
        "AnthropicVertex",
        "BetaRefusalFallbackMiddleware",
        "beta",
        "beta_tool",
        "with_options",
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
# The model client: the one file that talks to the model API (ADR 0012).
MODEL_CLIENT_FILE = "app/harness/model_client.py"
# The loader: the one file that reads the synthetic corpus's generator (ADR 0010).
LOADER_FILE = "app/db/load.py"
# The live recorder (ADR 0014): it drives the app over ASGI, and reads the date once.
LIVE_FILE = "app/runners/live.py"
# The eval scorer: record ownership from the corpus generator (ADR 0014).
SCORER_FILE = "app/runners/scoring.py"
# Modules that one file alone may import (two, where a tuple names them).
SINGLE_FILE: dict[str, str | tuple[str, ...]] = {
    # The loop's exception handler, set in the lifespan; the hooks (M1 PR 5); the live
    # recorder's event loop (M1 PR 16).
    "asyncio": ("app/main.py", LIVE_FILE),
    "threading": "app/observability/logs.py",
    "warnings": "app/observability/logs.py",
    # The route's clock (M1 PR 12).
    "time": "app/api/ask.py",
    "anthropic": MODEL_CLIENT_FILE,
    "httpx2": MODEL_CLIENT_FILE,
    "psycopg_pool": POOL_FILE,
    "numpy": EMBEDDER_FILE,
    "onnxruntime": EMBEDDER_FILE,
    "tokenizers": EMBEDDER_FILE,
    "data": (LOADER_FILE, SCORER_FILE),
}
# Reading the date from the clock: the route, for the prompt, and the live recorder, for
# its run (M1 PR 16). Scoring reads none: a sample carries its date. time.* is
# SINGLE_FILE's (app/api/ask.py).
CLOCK_NAMES = frozenset({"today", "now", "utcnow"})
CLOCK_FILES = ("app/api/ask.py", LIVE_FILE)
# In the model client, the SDK's names it may use, and nothing else of the package: no
# other client, no credential helper, no beta namespace, no copied client (review
# finding sec-r2). Attribute chains from the module and from-imports are both checked.
ANTHROPIC_NAMES = frozenset(
    {"Anthropic", "APIError", "APIConnectionError", "APIStatusError", "APITimeoutError", "types"}
)
ANTHROPIC_TYPES = frozenset({"Message"})
# Attributes banned in the model client alone: a copy of the SDK client with other options.
MODEL_CLIENT_ATTRIBUTES = frozenset({"copy"})
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
    sdk_names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.partition(".")[0]
                modules.add(top)
                dotted.add(alias.name)
                if top == "os":
                    os_names.add(alias.asname or "os")
                if top == "anthropic" and rel == MODEL_CLIENT_FILE:
                    if alias.name != "anthropic":
                        problems.append(f"{rel}:{node.lineno}: import {alias.name}")
                    sdk_names.add(alias.asname or "anthropic")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            top = "app" if node.level else module.partition(".")[0]
            modules.add(top)
            if not node.level:
                dotted.add(module)
                dotted.update(f"{module}.{alias.name}" for alias in node.names)
            if any(alias.name == "*" for alias in node.names):
                # A star import binds names the scan never sees (review finding test-r1).
                problems.append(f"{rel}:{node.lineno}: from {module or '.'} import *")
            if top == "anthropic" and rel == MODEL_CLIENT_FILE:
                problems += [
                    f"{rel}:{node.lineno}: from {module} import {alias.name}"
                    for alias in node.names
                    if module != "anthropic" or alias.name not in ANTHROPIC_NAMES
                ]
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
                if alias.name in CLOCK_NAMES and rel not in CLOCK_FILES:
                    problems.append(f"{rel}:{node.lineno}: import {alias.name} reads the clock")
        elif isinstance(node, ast.Attribute):
            on_os = isinstance(node.value, ast.Name) and node.value.id in os_names
            if rel == MODEL_CLIENT_FILE:
                on_sdk = isinstance(node.value, ast.Name) and node.value.id in sdk_names
                if on_sdk and node.attr not in ANTHROPIC_NAMES:
                    problems.append(f"{rel}:{node.lineno}: anthropic.{node.attr}")
                inner = node.value
                if (
                    isinstance(inner, ast.Attribute)
                    and inner.attr == "types"
                    and isinstance(inner.value, ast.Name)
                    and inner.value.id in sdk_names
                    and node.attr not in ANTHROPIC_TYPES
                ):
                    problems.append(f"{rel}:{node.lineno}: anthropic.types.{node.attr}")
                if node.attr in MODEL_CLIENT_ATTRIBUTES:
                    problems.append(f"{rel}:{node.lineno}: .{node.attr} in the model client")
            if node.attr in BANNED_ATTRIBUTES or (on_os and OS_PROGRAM.fullmatch(node.attr)):
                problems.append(f"{rel}:{node.lineno}: .{node.attr}")
            if node.attr in CLOCK_NAMES and rel not in CLOCK_FILES:
                problems.append(f"{rel}:{node.lineno}: .{node.attr} reads the clock")
            if node.attr == "connect" and rel != DB_CONNECT_FILE:
                problems.append(f"{rel}:{node.lineno}: .connect outside {DB_CONNECT_FILE}")
        elif isinstance(node, ast.Name) and node.id in BANNED_NAMES:
            problems.append(f"{rel}:{node.lineno}: {node.id}")
    for module, files in sorted(SINGLE_FILE.items()):
        allowed = (files,) if isinstance(files, str) else files
        if module in modules and rel not in allowed:
            problems.append(f"{rel}: {module} outside {' and '.join(allowed)}")
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
        MODEL_CLIENT_FILE,
        LOADER_FILE,
        LIVE_FILE,
        SCORER_FILE,
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
    # The generator, outside the loader (ADR 0010) and the eval scorer (ADR 0014).
    ("from data.synthetic import generate\n", "data outside app/db/load.py and app/runners"),
    ("import data.synthetic.generate\n", "data outside app/db/load.py and app/runners"),
    # The date from the clock, outside the route and the live recorder (M1 PR 16).
    ("import datetime\ndatetime.date.today()\n", ".today reads the clock"),
    ("from datetime import datetime\ndatetime.now()\n", ".now reads the clock"),
    ("import datetime as d\nd.datetime.utcnow()\n", ".utcnow reads the clock"),
    ("from datetime import date\nclock = date.today\n", ".today reads the clock"),
    ("from somewhere import today\n", "import today reads the clock"),
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
    ("from asyncio import *\nopen_connection('h', 1)\n", "from asyncio import *"),
    ("from app.config import *\n", "from app.config import *"),
    ("loop.run_in_executor(None, f)\n", ".run_in_executor"),
    ("threading.Thread(target=f).start()\n", ".Thread"),
    ("from threading import Timer\n", "import Timer"),
    ("loop.start_tls(t, p, c)\n", ".start_tls"),
    ("from asyncio import subprocess\n", "banned module asyncio.subprocess"),
    ("import asyncio\n", "asyncio outside app/main.py and app/runners/live.py"),
    ("import threading\n", "threading outside app/observability/logs.py"),
    # The clock, outside the route that needs it, and any sleep (M1 PR 12).
    ("import time\n", "time outside app/api/ask.py"),
    ("from time import monotonic\n", "time outside app/api/ask.py"),
    ("import time\ntime.sleep(1)\n", ".sleep"),
    ("from time import sleep\n", "import sleep"),
    ("await asyncio.sleep(1)\n", ".sleep"),
    # The model client's libraries, outside app/harness/model_client.py (ADR 0012).
    ("import anthropic\n", "anthropic outside app/harness/model_client.py"),
    ("from anthropic import Anthropic\n", "anthropic outside app/harness/model_client.py"),
    ("import httpx2\n", "httpx2 outside app/harness/model_client.py"),
    ("from httpx2 import Client\n", "httpx2 outside"),
    # Anywhere: the SDK's other clients, and its HTTP clients that read proxy variables.
    ("from anthropic.lib.bedrock import AnthropicBedrock\n", "banned module anthropic.lib"),
    ("import anthropic.lib.tools\n", "banned module anthropic.lib"),
    ("anthropic.DefaultHttpxClient(trust_env=False)\n", ".DefaultHttpxClient"),
    ("from anthropic import DefaultAsyncHttpxClient\n", "import DefaultAsyncHttpxClient"),
    ("from anthropic import DefaultAioHttpClient\n", "import DefaultAioHttpClient"),
    # Review finding sec-5: the re-exports, and the paths out of the factory's options.
    ("anthropic.AnthropicFoundry(resource='x')\n", ".AnthropicFoundry"),
    ("from anthropic import AnthropicBedrock\n", "import AnthropicBedrock"),
    ("from anthropic import AnthropicVertex as V\n", "import AnthropicVertex"),
    ("anthropic.AnthropicAWS()\n", ".AnthropicAWS"),
    ("anthropic.AnthropicGoogleCloud()\n", ".AnthropicGoogleCloud"),
    ("from anthropic import beta_tool\n", "import beta_tool"),
    ("anthropic.BetaRefusalFallbackMiddleware()\n", ".BetaRefusalFallbackMiddleware"),
    ("sdk.beta.messages.create()\n", ".beta"),
    ("sdk.with_options(base_url='x').messages.create()\n", ".with_options"),
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


def test_the_clock_is_allowed_in_the_ask_route_only() -> None:
    assert scan("import time\nnow = time.monotonic\n", "app/api/ask.py")[1] == []
    assert scan("import time\ntime.sleep(1)\n", "app/api/ask.py")[1] != []


def test_the_date_is_read_in_the_route_and_the_live_recorder_only() -> None:
    source = "import datetime\ntoday = datetime.date.today\nday = datetime.date.today()\n"
    for rel in CLOCK_FILES:
        assert scan(source, rel)[1] == [], rel
    for rel in ("app/runners/scoring.py", "app/harness/loop.py", "app/harness/prompt.py"):
        assert scan(source, rel)[1] != [], rel
    # A date that is passed in reads no clock (the loop, the prompt and the scorer).
    assert scan("import datetime\nday = datetime.date(2026, 11, 13)\n", SCORER_FILE)[1] == []


def test_asyncio_is_allowed_in_the_app_and_the_live_recorder_only() -> None:
    assert scan("import asyncio\nasyncio.run(main())\n", LIVE_FILE)[1] == []
    assert scan("import asyncio\n", SCORER_FILE)[1] != []
    # Its connecting calls stay banned there too.
    assert scan("import asyncio\nasyncio.open_connection('h', 1)\n", LIVE_FILE)[1] != []


def test_the_pool_is_allowed_in_its_own_file() -> None:
    assert scan("from psycopg_pool import ConnectionPool\n", "app/db/session.py")[1] == []


def test_the_embedder_libraries_are_allowed_in_their_own_file() -> None:
    source = "import numpy as np\nimport onnxruntime as ort\nimport tokenizers\n"
    assert scan(source, "app/embeddings/onnx.py")[1] == []
    assert scan(source, "app/embeddings/chunking.py")[1] != []
    # The ban on from_pretrained holds there too.
    assert scan("tokenizers.Tokenizer.from_pretrained('x')\n", "app/embeddings/onnx.py")[1] != []


MODEL_CLIENT_PLANTED = [
    ("import anthropic\nanthropic.AsyncAnthropicFoundry()\n", "anthropic.AsyncAnthropicFoundry"),
    ("import anthropic\nanthropic.exchange_federation_assertion()\n", "anthropic.exchange_fed"),
    ("import anthropic as a\na.default_credentials()\n", "anthropic.default_credentials"),
    ("import anthropic\nanthropic.lib.credentials.TokenCache\n", "anthropic.lib"),
    ("import anthropic\nanthropic.types.beta.BetaMessage\n", "anthropic.types.beta"),
    ("from anthropic import AsyncAnthropicBedrock\n", "import AsyncAnthropicBedrock"),
    ("from anthropic.resources.beta.messages import Messages\n", "import Messages"),
    ("from anthropic.types import Message\n", "from anthropic.types import Message"),
    ("import anthropic.resources\n", "import anthropic.resources"),
    ("self._sdk.copy(base_url='x')\n", ".copy in the model client"),
]


@pytest.mark.parametrize(("source", "needle"), MODEL_CLIENT_PLANTED)
def test_the_model_client_uses_the_reviewed_sdk_names_only(source: str, needle: str) -> None:
    problems = scan(source, MODEL_CLIENT_FILE)[1]
    assert any(needle in problem for problem in problems), problems


def test_the_reviewed_sdk_names_pass() -> None:
    source = (
        "import anthropic\nimport httpx2\nanthropic.Anthropic(api_key='x')\n"
        "anthropic.APIError\nanthropic.APITimeoutError\nanthropic.APIConnectionError\n"
        "anthropic.APIStatusError\nanthropic.types.Message\nhttpx2.Client()\n"
        "from anthropic import Anthropic, APIError\n"
    )
    assert scan(source, MODEL_CLIENT_FILE)[1] == []
    # Elsewhere a dict's copy is fine; in the model client it is not.
    assert scan("d.copy()\n", "app/harness/tools.py")[1] == []


def test_the_model_client_libraries_are_allowed_in_their_own_file() -> None:
    source = "import anthropic\nimport httpx2\n"
    assert scan(source, MODEL_CLIENT_FILE)[1] == []
    assert scan(source, "app/harness/model.py")[1] != []
    # The bans hold there too.
    assert scan("anthropic.DefaultHttpxClient()\n", MODEL_CLIENT_FILE)[1] != []
    assert scan("import anthropic.lib.vertex\n", MODEL_CLIENT_FILE)[1] != []
    # The near miss: httpx2 is not httpx, and httpx stays banned.
    assert banned_module("httpx2") is None and banned_module("httpx") == "httpx"


def test_the_generator_is_allowed_in_the_loader_and_the_scorer_only() -> None:
    source = "from data.synthetic import generate\n"
    assert scan(source, LOADER_FILE)[1] == []
    assert scan(source, SCORER_FILE)[1] == []
    assert scan(source, "app/db/migrate.py")[1] != []
    assert scan(source, LIVE_FILE)[1] != []


def test_connect_is_allowed_in_its_own_file() -> None:
    source = "import psycopg\npsycopg.connect('x')\n"
    assert scan(source, "app/db/connect.py")[1] == []
    assert scan(source, "app/db/session.py")[1] != []
