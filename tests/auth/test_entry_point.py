"""The entry point serves the dev login on exactly 127.0.0.1, or not at all (plan, PR 7).

With the dev login on (DEV_AUTH=1, prefixed), `python -P -m app` refuses, with exit code 2 and before it binds
anything, unless the host is exactly 127.0.0.1 and the port is explicit; it refuses
--uds and --fd (and any other argument), and it refuses when app.auth.dev_shim cannot be
imported, as in the image. Binding goes through uvicorn.Config.bind_socket(), and after
startup verify_listeners() checks every listening socket's address again.

The refusals run as subprocesses, with a hard timeout, from an empty working directory
(so no .env is read) and an environment built from scratch.
"""

from __future__ import annotations

import http.client
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
import uvicorn

from app import __main__ as entry
from app.config.settings import env_name

TIMEOUT = 8.0
LOOPBACK = "127.0.0.1"
DEV_AUTH, HOST, PORT = env_name("dev_auth"), env_name("host"), env_name("port")
REFUSED_HOSTS = [
    "0.0.0.0",  # noqa: S104 - a bind the entry point must refuse
    "0",
    "localhost",
    "::",
    "127.0.0.2",
    "127.0.0.1 ",
    "192.0.2.1",
]


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((LOOPBACK, 0))
        return probe.getsockname()[1]


def scrubbed_env(home: Path, **variables: str) -> dict[str, str]:
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home), "NO_PROXY": "*"}
    return env | {"no_proxy": "*"} | variables


def refused_connection(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(1.0)
        return probe.connect_ex((LOOPBACK, port)) != 0


def run_entry(workdir: Path, env: dict[str, str], *args: str) -> tuple[int | None, str]:
    """(exit code, stderr); the exit code is None when the app was still running at the
    timeout, which means it started serving."""
    log = workdir / "stderr.log"  # a file, not a pipe: a full pipe would block the app
    with log.open("wb") as stderr:
        process = subprocess.Popen(
            [sys.executable, "-P", "-m", "app", *args],
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


@pytest.mark.parametrize("host", REFUSED_HOSTS, ids=repr)
def test_entry_point_refuses(tmp_path: Path, host: str) -> None:
    port = free_port()
    env = scrubbed_env(tmp_path, **{DEV_AUTH: "1", HOST: host, PORT: str(port)})
    code, stderr = run_entry(tmp_path, env)
    assert code == 2, f"{HOST}={host!r} with {DEV_AUTH}=1: exit {code} (None: it served)"
    assert "refused to start" in stderr and HOST in stderr, stderr[-2000:]
    assert "Uvicorn running" not in stderr, "it bound before refusing"
    assert refused_connection(port), f"something listens on {LOOPBACK}:{port}"


@pytest.mark.parametrize("option", ["--uds", "--fd"])
def test_entry_point_refuses_a_socket_option(tmp_path: Path, option: str) -> None:
    port = free_port()
    target = str(tmp_path / "app.sock") if option == "--uds" else "0"
    env = scrubbed_env(tmp_path, **{DEV_AUTH: "1", HOST: LOOPBACK, PORT: str(port)})
    code, stderr = run_entry(tmp_path, env, option, target)
    assert code == 2, f"{option} with {DEV_AUTH}=1: exit {code} (None: it served)"
    assert "refused to start" in stderr and option in stderr, stderr[-2000:]
    assert not (tmp_path / "app.sock").exists(), "a Unix socket was created"
    assert refused_connection(port), f"something listens on {LOOPBACK}:{port}"


def test_entry_point_refuses_port_0(tmp_path: Path) -> None:
    # The Host and Origin checks compare against the port, so it must be known up front.
    env = scrubbed_env(tmp_path, **{DEV_AUTH: "1", HOST: LOOPBACK, PORT: "0"})
    code, stderr = run_entry(tmp_path, env)
    assert code == 2, f"{PORT}=0 with {DEV_AUTH}=1: exit {code} (None: it served)"
    assert "refused to start" in stderr and PORT in stderr, stderr[-2000:]


def _get(port: int, path: str, host: str) -> int:
    connection = http.client.HTTPConnection(LOOPBACK, port, timeout=1.0)
    try:
        connection.request("GET", path, headers={"Host": host})
        return connection.getresponse().status
    finally:
        connection.close()


def test_entry_point_serves_the_shim_on_loopback(tmp_path: Path) -> None:
    # Control: exactly 127.0.0.1 and an explicit port start the shim.
    port = free_port()
    env = scrubbed_env(tmp_path, **{DEV_AUTH: "1", HOST: LOOPBACK, PORT: str(port)})
    log = tmp_path / "stderr.log"
    with log.open("wb") as stderr:
        process = subprocess.Popen(
            [sys.executable, "-P", "-m", "app"],
            cwd=tmp_path,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=stderr,
        )
    try:
        deadline = time.monotonic() + TIMEOUT
        statuses: tuple[int, int] | None = None
        while statuses is None and time.monotonic() < deadline:
            if process.poll() is not None:
                pytest.fail(f"the app exited with {process.returncode}")
            try:
                statuses = (
                    _get(port, "/dev/login", f"{LOOPBACK}:{port}"),
                    _get(port, "/dev/login", f"localhost:{port}"),
                )
            except OSError:
                time.sleep(0.1)
        assert statuses == (200, 400), f"/dev/login: {statuses} (the right Host, then another)"
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


@pytest.mark.usefixtures("restore_logging")
def test_entry_point_refuses_without_the_shim(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # As in the image, where .dockerignore leaves the shim out: importing it fails.
    def no_bind(self: uvicorn.Config) -> socket.socket:
        raise AssertionError("bound a socket; the entry point should have refused")

    def no_server(self: uvicorn.Server, sockets: list | None = None) -> None:
        raise AssertionError("the server started; the entry point should have refused")

    # Both: a start that binds by itself never reaches bind_socket(), and must not serve.
    monkeypatch.setattr(uvicorn.Config, "bind_socket", no_bind)
    monkeypatch.setattr(uvicorn.Server, "run", no_server)
    monkeypatch.setitem(sys.modules, "app.auth.dev_shim", None)
    monkeypatch.chdir(tmp_path)
    for name, value in {DEV_AUTH: "1", HOST: LOOPBACK, PORT: str(free_port())}.items():
        monkeypatch.setenv(name, value)
    assert entry.main() == 2
    assert "dev_shim" in capsys.readouterr().err


class FakeSocket:
    def __init__(self, family: socket.AddressFamily, name: object) -> None:
        self.family = family
        self._name = name

    def getsockname(self) -> object:
        return self._name


def fakes(port: int) -> dict[str, FakeSocket]:
    return {
        "0.0.0.0": FakeSocket(socket.AF_INET, ("0.0.0.0", port)),  # noqa: S104
        "::": FakeSocket(socket.AF_INET6, ("::", port, 0, 0)),
        "path": FakeSocket(socket.AF_UNIX, "/tmp/app.sock"),  # noqa: S108 - a name only
        "other port": FakeSocket(socket.AF_INET, (LOOPBACK, port + 1)),
    }


@pytest.mark.parametrize("name", list(fakes(1)))
def test_verify_listeners(name: str) -> None:
    port = 8000
    with pytest.raises(entry.StartRefused):
        entry.verify_listeners([fakes(port)[name]], port)
    with pytest.raises(entry.StartRefused):
        entry.verify_listeners(
            [FakeSocket(socket.AF_INET, (LOOPBACK, port)), fakes(port)[name]], port
        )
    with pytest.raises(entry.StartRefused):
        entry.verify_listeners([], port)
    # Control: exactly 127.0.0.1 on the port passes.
    entry.verify_listeners([FakeSocket(socket.AF_INET, (LOOPBACK, port))], port)


@pytest.mark.parametrize("name", list(fakes(1)))
@pytest.mark.usefixtures("restore_logging")
def test_verify_listeners_after_startup_exits_2(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    name: str,
) -> None:
    # The real server binds 127.0.0.1; what startup reports is replaced by a fake socket.
    # The main loop never runs, and the port is closed again when main() returns 2.
    served = []

    async def main_loop(self: uvicorn.Server) -> None:
        served.append(True)

    port = free_port()
    monkeypatch.setattr(uvicorn.Server, "main_loop", main_loop)
    monkeypatch.setattr(entry, "listening_sockets", lambda server: [fakes(port)[name]])
    monkeypatch.chdir(tmp_path)
    for variable, value in {DEV_AUTH: "1", HOST: LOOPBACK, PORT: str(port)}.items():
        monkeypatch.setenv(variable, value)
    assert entry.main() == 2, "a listener off 127.0.0.1 was served"
    assert served == [], "the main loop ran"
    assert "refused to start" in capsys.readouterr().err
    assert refused_connection(port), f"{LOOPBACK}:{port} still listens"
