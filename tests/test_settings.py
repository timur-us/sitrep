"""Settings: no implicit dotenv, flags on only for exactly "1", plaintext traces only with
the console exporter on a 127.0.0.1 bind made by the entry point, and tests that run
without proxy variables.
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
from pathlib import Path

import pytest
import uvicorn
from branding_scan import matches_any
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import ConsoleSpanExporter
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import ValidationError
from repo_files import ROOT, git, list_files, read_text

from app import __main__ as entry
from app.config import branding
from app.config.settings import (
    DATABASE_PORT,
    Settings,
    env_name,
    ignored_url_notices,
    loopback_database_url,
)
from app.db import names
from app.main import create_app
from app.observability import tracing

ALL_INTERFACES = "0.0.0.0"  # noqa: S104 - the bind the plaintext check must refuse
PROXY_VARIABLES = tuple(
    name for base in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY") for name in (base, base.lower())
)
LOOPBACK_PROXY = "http://127.0.0.1:9"
# The variables the settings read (ADR 0009: every one prefixed, the database URLs too
# since M1 PR 3).
HOST, PORT = env_name("host"), env_name("port")
DEV_AUTH, TRACE_PLAINTEXT = env_name("dev_auth"), env_name("trace_plaintext")
URL_FIELDS = ("database_url", "migration_database_url")
URL_ROLES = (names.APP_ROLE, names.MIGRATOR_ROLE)

# Every file git sees is read (review finding test-1), except the records that keep their
# history (decision records, plans, reports name the old variables on purpose) and the
# generated lock.
HISTORY = ("docs/decisions/**", "docs/plans/**", "docs/reports/**", "uv.lock")
# A database URL variable without the prefix: the bare name as a whole token. A prefixed
# name does not match, since an underscore precedes the bare part, and neither does a
# name built by an f-string or a shell expansion right before it ("}"). Built from the
# fields, so this file does not name the variables itself; it must keep building them,
# since the scan reads this file too. Known gaps (review finding test-r5): a real bare
# name right after "}" or glued to a flag (-eNAME), and a lower-case name in Python,
# where it is the field; the settings tests set variables through env_name() instead.
_BARE = rf"(?<![A-Za-z0-9_}}])(?:{'|'.join(field.upper() for field in URL_FIELDS)})(?![A-Za-z0-9_])"
# Settings match names in any case, so outside Python any case is reported (review
# finding test-2). In Python the lower-case spelling is the field itself.
BARE_URL_NAME = re.compile(_BARE)
BARE_URL_NAME_ANY_CASE = re.compile(_BARE, re.IGNORECASE)


def bare_url_names(text: str, *, any_case: bool = True) -> list[tuple[int, str]]:
    """(line number, name) for every unprefixed database URL variable in text."""
    pattern = BARE_URL_NAME_ANY_CASE if any_case else BARE_URL_NAME
    return [
        (number, match.group(0))
        for number, line in enumerate(text.splitlines(), start=1)
        for match in pattern.finditer(line)
    ]


def live_files(root: Path = ROOT) -> dict[str, str]:
    """Repo-relative path -> text, for every file git sees under root but HISTORY."""
    found = {}
    for rel in list_files(root):
        if not matches_any(rel, HISTORY) and (text := read_text(root, rel)) is not None:
            found[rel] = text
    return found


def bare_url_findings(root: Path = ROOT) -> list[str]:
    """ "path:line: name" for every unprefixed database URL variable in the live files."""
    return [
        f"{rel}:{number}: {name}"
        for rel, text in live_files(root).items()
        for number, name in bare_url_names(text, any_case=not rel.endswith(".py"))
    ]


FLAG_VALUES = [
    ("1", True),
    ("0", False),
    ("true", False),
    ("yes", False),
    ("on", False),
    (" 1", False),
    ("1 ", False),
    ("01", False),
    ("", False),
]


@pytest.mark.parametrize(("value", "on"), FLAG_VALUES)
def test_flags_exact_1(monkeypatch: pytest.MonkeyPatch, value: str, on: bool) -> None:
    monkeypatch.setenv(TRACE_PLAINTEXT, value)
    monkeypatch.setenv(DEV_AUTH, value)
    settings = Settings(_env_file=None)
    assert settings.trace_plaintext == value, "the value was changed on the way in"
    assert (settings.trace_plaintext_on, settings.dev_auth_on) == (on, on), repr(value)


@pytest.mark.parametrize(("plaintext", "dev_auth"), [("1", "0"), ("0", "1")])
def test_flags_are_independent(
    monkeypatch: pytest.MonkeyPatch, plaintext: str, dev_auth: str
) -> None:
    monkeypatch.setenv(TRACE_PLAINTEXT, plaintext)
    monkeypatch.setenv(DEV_AUTH, dev_auth)
    settings = Settings(_env_file=None)
    assert (settings.trace_plaintext_on, settings.dev_auth_on) == (
        plaintext == "1",
        dev_auth == "1",
    )


@pytest.mark.parametrize("port", ["-1", "65536", "http"])
def test_port_must_be_a_port(monkeypatch: pytest.MonkeyPatch, port: str) -> None:
    monkeypatch.setenv(PORT, port)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_settings_names_are_prefixed() -> None:
    # Review finding sec-7 of PR 3 (Timur, 2026-10-29): tcsh exports HOST, and the
    # names are matched without regard to case, so unprefixed names could move the bind.
    # Review finding test-7: the prefix from branding, not from env_name() itself. The
    # literal names live in .env.example and the Dockerfile (test_env_example_is_safe,
    # tests/test_dockerfile.py); the branding scan keeps them out of Python files.
    # M1 PR 3 (Timur's answer 1 to the PR 7 questions): the database URLs too.
    prefix = f"{branding.SLUG.upper()}_"
    assert HOST == f"{prefix}HOST", HOST
    assert {env_name(field) for field in Settings.model_fields} == {
        f"{prefix}HOST",
        f"{prefix}PORT",
        f"{prefix}DEV_AUTH",
        f"{prefix}TRACE_PLAINTEXT",
        f"{prefix}DATABASE_URL",
        f"{prefix}MIGRATION_DATABASE_URL",
        f"{prefix}ALLOWED_HOSTS",
    }
    assert {field: env_name(field) for field in Settings.model_fields} == {
        field: prefix + field.upper() for field in Settings.model_fields
    }


@pytest.mark.parametrize("spelling", [str.upper, str.lower, str.title])
def test_unprefixed_names_are_ignored(monkeypatch: pytest.MonkeyPatch, spelling) -> None:
    unprefixed = {"HOST": ALL_INTERFACES, "PORT": "9", "DEV_AUTH": "1", "TRACE_PLAINTEXT": "1"}
    for name, value in unprefixed.items():
        monkeypatch.setenv(spelling(name), value)
    settings = Settings(_env_file=None)
    values = (settings.host, settings.port, settings.dev_auth, settings.trace_plaintext)
    assert values == ("127.0.0.1", 8000, "0", "0"), values
    # Control: the prefixed names, in any case, are read.
    monkeypatch.setenv(spelling(HOST), ALL_INTERFACES)
    monkeypatch.setenv(spelling(PORT), "9")
    prefixed = Settings(_env_file=None)
    assert (prefixed.host, prefixed.port) == (ALL_INTERFACES, 9)


def test_env_example_is_safe() -> None:
    # The file a developer copies: every flag off, the bind on loopback, both database
    # URLs written out and equal to the defaults, and the variables Settings does not
    # know (the API key, the model) ignored. No setting under its unprefixed name: it
    # would do nothing (M1 PR 3 for the database URLs).
    path = ROOT / ".env.example"
    example = Settings(_env_file=path)
    assert (example.host, example.port) == ("127.0.0.1", 8000)
    assert (example.trace_plaintext_on, example.dev_auth_on) == (False, False)
    lines = path.read_text().splitlines()
    variables = {line.split("=", 1)[0] for line in lines if "=" in line}
    assert {env_name(field) for field in Settings.model_fields} <= variables, sorted(variables)
    # Settings match names in any case (review finding sec-4).
    bare = {name for name in variables if name.lower() in Settings.model_fields}
    assert not bare, sorted(variables)
    defaults = Settings(_env_file=None)
    assert (example.database_url, example.migration_database_url) == (
        defaults.database_url,
        defaults.migration_database_url,
    )


def test_live_files_name_only_prefixed_database_urls() -> None:
    # M1 PR 3: every file but the history names the database URLs by their prefixed
    # variables only: .env.example, the runbook, compose.yaml, the Makefile, CI, the code
    # and the tests among them. An unprefixed name there would be set, and do nothing.
    found = bare_url_findings()
    assert not found, "unprefixed database URL variables:\n" + "\n".join(found)
    # Non-vacuity: the files that matter are read, and the two that tell a developer
    # what to set name both prefixed variables.
    files = live_files()
    for rel in (".env.example", "docs/runbooks/local-setup.md", "compose.yaml", "Makefile"):
        assert rel in files, f"{rel} is not read"
    for rel in ("pyproject.toml", "app/config/settings.py", "tests/test_settings.py"):
        assert rel in files, f"{rel} is not read"
    for rel in (".env.example", "docs/runbooks/local-setup.md"):
        missing = [env_name(f) for f in URL_FIELDS if env_name(f) not in files[rel]]
        assert not missing, f"{rel} does not name {missing}"


def test_planted_bare_url_names() -> None:
    bare, migration = (field.upper() for field in URL_FIELDS)
    prefixed = f"{branding.SLUG.upper()}_{bare}"
    text = "\n".join(
        [
            f"{bare}=postgresql://x",  # .env.example's shape
            f"- **URLs.** `{migration}` (the migrator)",  # the runbook's
            f"      {bare}: x",  # compose or CI environment
            f"    monkeypatch.setenv({bare!r}, url)",  # a test's
            f"export {migration}",  # a shell's
            f"{prefixed}=postgresql://x {prefixed.lower()}",  # prefixed: not reported
            f"X{bare} {bare}X {bare}_2",  # longer tokens
            f'f"{{prefix}}{bare}" ${{SLUG}}{migration}',  # built in front: not reported
            f"${{{bare}}} {{{migration}}}",  # inside an expansion: reported
            f"{bare.lower()}={bare.title()}",  # other cases: reported outside Python
        ]
    )
    expected = [(1, bare), (2, migration), (3, bare), (4, bare), (5, migration)]
    expected += [(9, bare), (9, migration)]
    assert bare_url_names(text, any_case=False) == expected
    lower, title = bare.lower(), bare.title()
    assert bare_url_names(text) == [*expected, (10, lower), (10, title)]
    # The history and the lock are not read; the runbooks and everything else are.
    for rel in ("docs/runbooks/local-setup.md", "pyproject.toml", "evals/gate.toml", "x/y.md"):
        assert not matches_any(rel, HISTORY), rel
    for rel in ("docs/decisions/0008-data-access.md", "docs/plans/m1-mvp-core.md", "uv.lock"):
        assert matches_any(rel, HISTORY), rel


def test_planted_repository_is_scanned(tmp_path: Path) -> None:
    # Review finding test-8: the whole path (git's file list, the history, the case rule).
    bare, migration = (field.upper() for field in URL_FIELDS)
    git(tmp_path, "init", "-q")
    planted = {
        "compose.yaml": f"      {bare.lower()}: x\n",  # any case outside Python
        "evals/run.sh": f"export {migration}\n",
        "app/x.py": f"settings.{bare.lower()}\nos.environ[{bare!r}]\n",  # the field; the name
        "docs/plans/p.md": f"`{bare}`\n",  # history
        "docs/decisions/0001-x.md": f"`{bare}`\n",
        "uv.lock": f"{bare}\n",
    }
    for rel, text in planted.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text)
    assert bare_url_findings(tmp_path) == [
        f"app/x.py:2: {bare}",
        f"compose.yaml:1: {bare.lower()}",
        f"evals/run.sh:1: {migration}",
    ]


def test_plaintext_needs_console_and_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(TRACE_PLAINTEXT, "1")
    plaintext = Settings(_env_file=None)

    # Allowed: the console exporter, unfiltered, on 127.0.0.1.
    chain = tracing.exporter_chain(plaintext, bind_host="127.0.0.1")
    assert type(chain) is ConsoleSpanExporter, f"chain is {type(chain).__name__}"

    refused = []
    for host in (ALL_INTERFACES, "::1", "localhost", "127.0.0.2", "", None):
        try:
            tracing.exporter_chain(plaintext, bind_host=host)
        except tracing.PlaintextRefused:
            refused.append(host)
    assert refused == [ALL_INTERFACES, "::1", "localhost", "127.0.0.2", "", None], refused
    with pytest.raises(tracing.PlaintextRefused, match="console"):
        tracing.exporter_chain(plaintext, bind_host="127.0.0.1", inner=InMemorySpanExporter())
    with pytest.raises(tracing.PlaintextRefused):
        create_app(plaintext, bind_host=ALL_INTERFACES)
    with pytest.raises(tracing.PlaintextRefused):
        create_app(plaintext)
    with pytest.raises(tracing.PlaintextRefused):
        create_app(plaintext, tracer_provider=TracerProvider(), bind_host="127.0.0.1")


def _no_server(self: uvicorn.Server) -> None:
    raise AssertionError("the server started; the entry point should have refused")


@pytest.mark.usefixtures("restore_logging")
def test_entry_point_refuses_plaintext_off_loopback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(uvicorn.Server, "run", _no_server)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(TRACE_PLAINTEXT, "1")
    monkeypatch.setenv(HOST, ALL_INTERFACES)
    assert entry.main() == 2
    assert f"{TRACE_PLAINTEXT}=1" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("variables", "host"),
    [({TRACE_PLAINTEXT: "1"}, "127.0.0.1"), ({HOST: ALL_INTERFACES}, ALL_INTERFACES)],
)
@pytest.mark.usefixtures("restore_logging")
def test_entry_point_starts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, variables: dict[str, str], host: str
) -> None:
    # Positive cases: plaintext on loopback, and the image's 0.0.0.0 without plaintext.
    # Nothing is bound: bind_socket() hands out an unbound socket. Review finding test-4:
    # the server is given exactly that socket, so it binds nothing by itself.
    bound: list[socket.socket] = []
    started: list[tuple[str, list | None]] = []

    def bind_socket(config: uvicorn.Config) -> socket.socket:
        bound.append(socket.socket())
        return bound[-1]

    monkeypatch.setattr(uvicorn.Config, "bind_socket", bind_socket)
    monkeypatch.setattr(
        uvicorn.Server,
        "run",
        lambda server, sockets=None: started.append((server.config.host, sockets)),
    )
    monkeypatch.chdir(tmp_path)
    for name, value in variables.items():
        monkeypatch.setenv(name, value)
    assert entry.main() == 0
    assert len(bound) == 1, bound
    assert started == [(host, bound)], started
    assert bound[0].fileno() == -1, "the entry point did not close the socket"


@pytest.mark.usefixtures("restore_logging")
def test_entry_point_reads_dotenv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Only `python -m app` reads .env; the same refusal, driven from the file.
    monkeypatch.setattr(uvicorn.Server, "run", _no_server)
    (tmp_path / ".env").write_text(f"{TRACE_PLAINTEXT}=1\n{HOST}={ALL_INTERFACES}\n")
    monkeypatch.chdir(tmp_path)
    assert entry.main() == 2


# An old URL value, refused wherever it is read; the marker shows whether it is echoed.
NOT_ECHOED = "notEchoedMarker"


def _old_url(role: str) -> str:
    return f"postgresql://{role}@192.0.2.1:{DATABASE_PORT}/{NOT_ECHOED}"


def _start_without_binding(monkeypatch: pytest.MonkeyPatch) -> list[Settings]:
    """Let the entry point run without binding or serving; returns the settings it served."""
    served: list[Settings] = []
    real = entry.server_config

    def record(settings: Settings, *args, **kwargs) -> uvicorn.Config:
        served.append(settings)
        return real(settings, *args, **kwargs)

    monkeypatch.setattr(uvicorn.Config, "bind_socket", lambda config: socket.socket())
    monkeypatch.setattr(uvicorn.Server, "run", lambda server, sockets=None: None)
    monkeypatch.setattr(entry, "server_config", record)
    return served


@pytest.mark.usefixtures("restore_logging")
def test_entry_point_with_an_old_dotenv(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    settings: Settings,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Review finding test-5: a .env from before M1 PR 3, through `python -m app` itself.
    # It starts, and serves the default URLs.
    served = _start_without_binding(monkeypatch)
    lines = [
        f"{field.upper()}={_old_url(role)}\n"
        for field, role in zip(URL_FIELDS, URL_ROLES, strict=True)
    ]
    (tmp_path / ".env").write_text("".join(lines))
    monkeypatch.chdir(tmp_path)
    assert entry.main() == 0
    assert [(s.database_url, s.migration_database_url) for s in served] == [
        (settings.database_url, settings.migration_database_url)
    ]
    # Dropped without a word: the entry point does not read the file's names or values
    # (review finding test-r3).
    err = capsys.readouterr().err
    assert "ignored" not in err and NOT_ECHOED not in err, err
    # Control: the same file with the prefixed names is read, and refused: since M1 PR 5
    # with exit 2, naming the variables and not their values (review finding sec-r1).
    (tmp_path / ".env").write_text("".join(f"{branding.SLUG.upper()}_{line}" for line in lines))
    assert entry.main() == 2
    err = capsys.readouterr().err
    assert "refused to start: invalid settings" in err, err
    assert all(env_name(field) in err for field in URL_FIELDS), err
    assert NOT_ECHOED not in err and "192.0.2.1" not in err, err


def test_ignored_url_notices() -> None:
    # Review finding sec-1: an unprefixed URL variable is ignored, and said so, by name.
    bare, migration = (field.upper() for field in URL_FIELDS)
    environ = {
        bare: _old_url(names.APP_ROLE),
        migration.lower(): _old_url(names.MIGRATOR_ROLE),
        bare.title(): _old_url(names.APP_ROLE),
        env_name(URL_FIELDS[0]): loopback_database_url(names.APP_ROLE),
        "HOST": ALL_INTERFACES,
        f"X{bare}": "x",
    }
    notices = ignored_url_notices(environ)
    # Each names the variable as set (sorted); the tail names the prefixed ones for all
    # (review finding test-r1: the bare name is inside the prefixed one, so the head is
    # what pins it).
    heads = [notice.split(", but ", 1)[0] for notice in notices]
    assert heads == [f"ignored: {name} is set" for name in (bare, bare.title(), migration.lower())]
    for notice in notices:
        assert all(env_name(field) in notice for field in URL_FIELDS), notice
        assert NOT_ECHOED not in notice and "192.0.2.1" not in notice, notice
    assert ignored_url_notices({"HOST": ALL_INTERFACES, f"X{bare}": "x"}) == []


@pytest.mark.usefixtures("restore_logging")
def test_entry_point_names_ignored_url_variables(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    settings: Settings,
    capsys: pytest.CaptureFixture[str],
) -> None:
    served = _start_without_binding(monkeypatch)
    monkeypatch.chdir(tmp_path)
    # Control: nothing set, nothing said.
    assert entry.main() == 0
    assert "ignored" not in capsys.readouterr().err
    for field, role in zip(URL_FIELDS, URL_ROLES, strict=True):
        monkeypatch.setenv(field.upper(), _old_url(role))
    assert entry.main() == 0
    err = capsys.readouterr().err
    notices = [line for line in err.splitlines() if line.startswith("ignored:")]
    assert len(notices) == 2, err
    assert NOT_ECHOED not in err and "192.0.2.1" not in err, err
    assert served[-1] == settings


def test_settings_ignore_dotenv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(f"{HOST}={ALL_INTERFACES}\n{PORT}=9\n{TRACE_PLAINTEXT}=1\n")
    monkeypatch.chdir(tmp_path)
    # Control: the planted file would change the settings if it were read.
    assert Settings(_env_file=".env").host == ALL_INTERFACES
    settings = Settings()
    assert (settings.host, settings.port, settings.trace_plaintext) == ("127.0.0.1", 8000, "0")


def test_proxy_environment_is_clean(settings: Settings) -> None:
    # The probe. In the normal run nothing sets these, so on its own it proves little;
    # test_proxy_variables_are_scrubbed runs it with every one of them set.
    # Locals only: pytest's assertion rewriting would print all of os.environ.
    proxies = {name.lower() for name in PROXY_VARIABLES}
    leaked = sorted(name for name in os.environ if name.lower() in proxies)
    otel = sorted(name for name in os.environ if name.upper().startswith("OTEL_"))
    libpq = sorted(name for name in os.environ if name.startswith("PG"))
    no_proxy = (os.environ.get("NO_PROXY"), os.environ.get("no_proxy"))
    assert not leaked, f"proxy variables visible to tests: {leaked}"
    assert not otel, f"OpenTelemetry variables visible to tests: {otel}"
    assert not libpq, f"libpq variables visible to tests: {libpq}"
    assert no_proxy == ("*", "*"), f"NO_PROXY, no_proxy: {no_proxy}"
    defaults = (settings.host, settings.port, settings.dev_auth, settings.trace_plaintext)
    assert defaults == ("127.0.0.1", 8000, "0", "0"), defaults
    urls = (settings.database_url, settings.migration_database_url)
    assert urls == tuple(map(loopback_database_url, URL_ROLES)), "a database URL leaked in"
    # The bare names change no setting, so only their absence shows the scrub removed
    # them (review finding test-9).
    bare = sorted(name for name in os.environ if name.lower() in URL_FIELDS)
    assert not bare, f"unprefixed database URL variables visible to tests: {bare}"


def test_proxy_variables_are_scrubbed() -> None:
    # The inner run inherits this environment, the developer's included; the probe
    # prints variable names and settings only, never values from os.environ.
    env = {name: value for name, value in os.environ.items() if name.lower() != "no_proxy"}
    env |= dict.fromkeys(PROXY_VARIABLES, LOOPBACK_PROXY)
    env |= {"Https_Proxy": LOOPBACK_PROXY, "OTEL_SDK_DISABLED": "true"}
    env |= {"Host": ALL_INTERFACES, "PORT": "9", "DEV_AUTH": "1", "TRACE_PLAINTEXT": "1"}
    env |= {HOST.title(): ALL_INTERFACES, PORT: "9", DEV_AUTH.lower(): "1", TRACE_PLAINTEXT: "1"}
    env |= {"PGHOSTADDR": "192.0.2.1", "PGPORT": "5432", "PGOPTIONS": "-c x=y", "PGTZ": "UTC"}
    # The database URLs, prefixed (valid, on another port, so a leak shows as a value)
    # and unprefixed (M1 PR 3).
    for field, role in zip(URL_FIELDS, URL_ROLES, strict=True):
        env[env_name(field)] = loopback_database_url(role).replace(f":{DATABASE_PORT}/", ":9/")
        env[field.upper()] = f"postgresql://{role}@192.0.2.1:{DATABASE_PORT}/x"
    probe = f"{Path(__file__).relative_to(ROOT).as_posix()}::test_proxy_environment_is_clean"
    result = subprocess.run(
        [sys.executable, "-P", "-m", "pytest", "-q", "-p", "no:cacheprovider", probe],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout[-3000:]
    assert "1 passed" in result.stdout, result.stdout[-3000:]
