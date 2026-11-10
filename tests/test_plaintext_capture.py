"""Plaintext traces refused while OpenTelemetry's header capture is set (M1 PR 5, ADR 0007).

Timur's answer 5 to the PR 7 questions, "plaintext refused with OTel header capture in
M1". With plaintext traces on (the trace_plaintext setting) the console exporter runs without the allowlist, so a
capture variable naming `cookie` or `set-cookie` would print the dev login's session
cookie, or any header, to stdout. The rule: while any variable whose name starts with
OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_ is set, in any case and whatever its value,
plaintext refuses, and `python -m app` exits 2 before it binds.

The refusals run as subprocesses with a hard timeout, from an empty working directory (no
.env), as tests/auth/test_entry_point.py does.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest
import uvicorn
from opentelemetry.sdk.trace.export import ConsoleSpanExporter

from app import __main__ as entry
from app.config.settings import Settings, env_name
from app.observability import tracing

PLAINTEXT, PORT, HOST = env_name("trace_plaintext"), env_name("port"), env_name("host")
PREFIX = "OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_"
# The names the FastAPI and HTTP instrumentations read, and one they might add.
CAPTURE = [
    f"{PREFIX}SERVER_REQUEST",
    f"{PREFIX}SERVER_RESPONSE",
    f"{PREFIX}SANITIZE_FIELDS",
    f"{PREFIX}CLIENT_REQUEST",
    f"{PREFIX}NOT_YET_READ",
    f"{PREFIX}SERVER_REQUEST".lower(),
]


TIMEOUT = 8.0


# The subprocess helpers of tests/auth/test_entry_point.py, repeated here so that one test
# module does not import another.
def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def scrubbed_env(home: Path, **variables: str) -> dict[str, str]:
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home), "NO_PROXY": "*"}
    return env | {"no_proxy": "*"} | variables


def run_entry(workdir: Path, env: dict[str, str]) -> tuple[int | None, str]:
    """(exit code, stderr); None when the app still ran at the timeout: it served."""
    log = workdir / "stderr.log"  # a file, not a pipe: a full pipe would block the app
    with log.open("wb") as stderr:
        process = subprocess.Popen(
            [sys.executable, "-P", "-m", "app"],
            cwd=workdir,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=stderr,
        )
    try:
        code = process.wait(timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        code = None
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
    return code, log.read_text(encoding="utf-8", errors="replace")


@pytest.mark.parametrize("name", CAPTURE)
def test_plaintext_refuses_to_start_with_header_capture(tmp_path: Path, name: str) -> None:
    # The real entry point, once per name. An empty value, and the order before the bind,
    # are pinned in process below (review findings test-3 and test-10).
    value = "cookie,set-cookie"
    port = free_port()
    env = scrubbed_env(
        tmp_path, **{PLAINTEXT: "1", HOST: "127.0.0.1", PORT: str(port), name: value}
    )
    code, err = run_entry(tmp_path, env)
    assert code == 2, (code, err)
    assert "refused to start" in err and name in err and PLAINTEXT in err, err
    assert value not in err, err


def _no_bind(self: uvicorn.Config) -> socket.socket:
    raise AssertionError("the entry point bound a socket before refusing")


@pytest.mark.parametrize("value", ["x-probe-header", ""])
@pytest.mark.usefixtures("restore_logging")
def test_the_refusal_comes_before_the_bind(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    value: str,
) -> None:
    monkeypatch.setattr(uvicorn.Config, "bind_socket", _no_bind)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(PLAINTEXT, "1")
    monkeypatch.setenv(f"{PREFIX}SERVER_RESPONSE", value)
    assert entry.main() == 2
    err = capsys.readouterr().err
    assert f"{PREFIX}SERVER_RESPONSE" in err and (not value or value not in err), err


def test_the_chain_refuses_in_process(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(PLAINTEXT, "1")
    plaintext = Settings(_env_file=None)
    # Control: without a capture variable, plaintext on loopback is the bare console.
    assert type(tracing.exporter_chain(plaintext, "127.0.0.1")) is ConsoleSpanExporter
    monkeypatch.setenv(f"{PREFIX}SERVER_REQUEST", "cookie")
    with pytest.raises(tracing.PlaintextRefused, match=f"{PREFIX}SERVER_REQUEST"):
        tracing.exporter_chain(plaintext, "127.0.0.1")


def test_capture_without_plaintext_is_left_to_the_allowlist(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    # The allowlist drops captured headers (tests/test_no_plaintext.py turns capture on),
    # so without plaintext the variables change nothing and nothing is refused.
    monkeypatch.setenv(f"{PREFIX}SERVER_REQUEST", "cookie")
    assert type(tracing.exporter_chain(settings, "127.0.0.1")) is tracing.AllowlistExporter


def test_capture_variables_are_named_in_any_case() -> None:
    environ = {
        f"{PREFIX}SERVER_REQUEST": "x",
        f"{PREFIX}server_response".lower(): "",
        "OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADER": "x",  # not the prefix: singular
        "OTEL_SERVICE_NAME": "x",
        "X_" + PREFIX: "x",
    }
    assert tracing.capture_variables(environ) == sorted(
        [f"{PREFIX}SERVER_REQUEST", f"{PREFIX}server_response".lower()]
    )
