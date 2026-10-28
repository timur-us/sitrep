"""Tests run offline: pytest-socket refuses connect() except to loopback.

pyproject.toml loads the plugin by name with autoloading off:
`--disable-plugin-autoload -p pytest_socket --disable-socket --allow-hosts=127.0.0.1,::1`.

The claim is limited to TCP connect() on in-process Python sockets. Not covered:
UDP sendto(); connect_ex() (unverified); DNS lookups through libc; subprocesses
and native libraries; and anything reached through a loopback proxy or tunnel
(an HTTP(S)_PROXY on 127.0.0.1 turns an allowed connect into egress).
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest
import pytest_socket
import repo_files
from repo_files import ROOT, list_files, read_text

TEST_NET_1 = "192.0.2.1"  # RFC 5737: documentation only, never routed


def _join(*pieces: str) -> str:
    return "".join(pieces)


# Ways a test or the config could switch the block off: markers, a fixture, a
# command-line flag, unloading the plugin. Assembled at runtime so that this
# file does not match itself.
OPT_OUTS = (
    _join("enable", "_socket"),
    _join("socket", "_enabled"),
    _join("allow", "_hosts"),
    _join("force-enable", "-socket"),
    _join("no:", "socket"),
    _join("no:", "pytest_socket"),
)


def socket_opt_outs(text: str) -> list[str]:
    return [needle for needle in OPT_OUTS if needle in text]


def test_network_is_blocked() -> None:
    error: Exception | None = None
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1)
        try:
            sock.connect((TEST_NET_1, 9))
        except (OSError, RuntimeError) as exc:  # the block is a RuntimeError
            error = exc
    assert isinstance(error, pytest_socket.SocketConnectBlockedError), (
        f"connect to {TEST_NET_1} was not blocked by pytest-socket: {error!r}"
    )

    # Positive control: loopback stays open, so the block above is not just a
    # broken socket module.
    with socket.create_server(("127.0.0.1", 0)) as server:
        port = server.getsockname()[1]
        with socket.create_connection(("127.0.0.1", port), timeout=2) as client:
            assert client.getpeername() == ("127.0.0.1", port)


def test_plugins_load_only_by_name(pytestconfig: pytest.Config) -> None:
    # An installed package's pytest11 entry point would run before the socket
    # guard is in place; platformdirs, a dependency of pip-audit, declares one.
    manager = pytestconfig.pluginmanager
    autoloaded = sorted(dist.project_name for _, dist in manager.list_plugin_distinfo())
    assert autoloaded == [], f"plugins autoloaded through entry points: {autoloaded}"
    assert manager.has_plugin("pytest_socket"), "pytest_socket is not loaded by name"


def test_effective_socket_config(pytestconfig: pytest.Config) -> None:
    # What pytest actually runs with, whatever the source: pyproject, another
    # config file, PYTEST_ADDOPTS or the command line.
    assert pytestconfig.inipath == ROOT / "pyproject.toml", f"config from {pytestconfig.inipath}"
    for name in (
        "pytest.ini",
        ".pytest.ini",
        "pytest.toml",
        ".pytest.toml",
        "tox.ini",
        "setup.cfg",
    ):
        assert not (ROOT / name).exists(), f"{name} would compete with pyproject.toml"
    # Option names assembled like OPT_OUTS, so that the scan below stays clean.
    assert pytestconfig.getoption("disable_socket") is True
    assert pytestconfig.getoption(_join("allow", "_hosts")) == "127.0.0.1,::1"
    assert not pytestconfig.getoption(_join("force_", "enable", "_socket"))


def opt_out_files(root: Path) -> list[str]:
    """Code and config that could switch the block off: every .py, .toml, .ini and
    .cfg file git sees, the Makefile and .github/."""
    return [
        rel
        for rel in list_files(root)
        if rel.endswith((".py", ".toml", ".ini", ".cfg"))
        or rel == "Makefile"
        or rel.startswith(".github/")
    ]


def test_no_socket_opt_outs() -> None:
    files = opt_out_files(ROOT)
    assert len(files) >= 18, f"only {len(files)} files scanned"
    named = {"tests/test_network.py", "tests/guardrails/test_block_secrets.py", "app/__init__.py"}
    assert named | {"pyproject.toml", "Makefile", ".claude/hooks/block_secrets.py"} <= set(files)
    found = [
        f"{rel}: {hit}" for rel in files for hit in socket_opt_outs(read_text(ROOT, rel) or "")
    ]
    assert not found, "the socket block is switched off:\n" + "\n".join(found)


def test_opt_out_scan_planted(tmp_path: Path) -> None:
    for needle in OPT_OUTS:
        assert socket_opt_outs(f"x = {needle!r}\n") == [needle]
    assert socket_opt_outs("--disable-socket --allow-hosts=127.0.0.1,::1") == []

    repo_files.git(tmp_path, "init", "-q")
    planted = {
        "tests/sub/test_x.py": f"pytestmark = {OPT_OUTS[0]!r}\n",
        "pytest.ini": f"[pytest]\naddopts = -p {OPT_OUTS[4]}\n",
        ".github/workflows/ci.yml": f"run: pytest --{OPT_OUTS[3]}\n",
        "docs/notes.md": f"{OPT_OUTS[0]}\n",  # prose is not scanned
    }
    for rel, text in planted.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text)
    flagged = {
        rel for rel in opt_out_files(tmp_path) if socket_opt_outs(read_text(tmp_path, rel) or "")
    }
    assert flagged == {"tests/sub/test_x.py", "pytest.ini", ".github/workflows/ci.yml"}
