"""Settings: no implicit dotenv, flags on only for exactly "1", plaintext traces only with
the console exporter on a 127.0.0.1 bind made by the entry point, and tests that run
without proxy variables.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import uvicorn
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import ConsoleSpanExporter
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from repo_files import ROOT

from app import __main__ as entry
from app.config.settings import Settings
from app.main import create_app
from app.observability import tracing

ALL_INTERFACES = "0.0.0.0"  # noqa: S104 - the bind the plaintext check must refuse
PROXY_VARIABLES = tuple(
    name for base in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY") for name in (base, base.lower())
)
LOOPBACK_PROXY = "http://127.0.0.1:9"

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
    monkeypatch.setenv("TRACE_PLAINTEXT", value)
    monkeypatch.setenv("DEV_AUTH", value)
    settings = Settings(_env_file=None)
    assert settings.trace_plaintext == value, "the value was changed on the way in"
    assert (settings.trace_plaintext_on, settings.dev_auth_on) == (on, on), repr(value)


def test_plaintext_needs_console_and_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRACE_PLAINTEXT", "1")
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
    monkeypatch.setenv("TRACE_PLAINTEXT", "1")
    monkeypatch.setenv("HOST", ALL_INTERFACES)
    assert entry.main() == 2
    assert "TRACE_PLAINTEXT=1" in capsys.readouterr().err


@pytest.mark.usefixtures("restore_logging")
def test_entry_point_reads_dotenv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Only `python -m app` reads .env; the same refusal, driven from the file.
    monkeypatch.setattr(uvicorn.Server, "run", _no_server)
    (tmp_path / ".env").write_text(f"TRACE_PLAINTEXT=1\nHOST={ALL_INTERFACES}\n")
    monkeypatch.chdir(tmp_path)
    assert entry.main() == 2


def test_settings_ignore_dotenv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(f"HOST={ALL_INTERFACES}\nPORT=9\nTRACE_PLAINTEXT=1\n")
    monkeypatch.chdir(tmp_path)
    # Control: the planted file would change the settings if it were read.
    assert Settings(_env_file=".env").host == ALL_INTERFACES
    settings = Settings()
    assert (settings.host, settings.port, settings.trace_plaintext) == ("127.0.0.1", 8000, "0")


def test_proxy_environment_is_clean() -> None:
    # The probe. In the normal run nothing sets a proxy, so on its own it proves
    # little; test_proxy_variables_are_scrubbed runs it with every variable set.
    # Locals only: pytest's assertion rewriting would print all of os.environ.
    leaked = sorted(name for name in PROXY_VARIABLES if name in os.environ)
    no_proxy = (os.environ.get("NO_PROXY"), os.environ.get("no_proxy"))
    assert not leaked, f"proxy variables visible to tests: {leaked}"
    assert no_proxy == ("*", "*"), f"NO_PROXY, no_proxy: {no_proxy}"


def test_proxy_variables_are_scrubbed() -> None:
    env = {name: value for name, value in os.environ.items() if name.lower() != "no_proxy"}
    env |= dict.fromkeys(PROXY_VARIABLES, LOOPBACK_PROXY)
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
