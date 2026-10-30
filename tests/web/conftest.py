"""Browser fixtures: the real entry point on loopback, and a Chromium that resolves no names.

The app runs as `python -P -m app` in a subprocess, from an empty working directory (so
no `.env` is read), with an environment built from scratch: PATH, HOME, the bind on
127.0.0.1 and a free port, and NO_PROXY. Chromium gets host-resolver rules that map
every name, and every IP literal except 127.0.0.1, to "not found", so a request
anywhere but the loopback server fails inside the browser, without a DNS lookup.
"""

from __future__ import annotations

import http.client
import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import Browser, sync_playwright

START_TIMEOUT = 20.0
NO_NAMES = "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1"
# WebRTC sends UDP that neither the resolver rules nor Playwright's events see. This
# policy allows only proxied UDP, and there is no proxy (not verified by a test).
NO_WEBRTC_UDP = "--force-webrtc-ip-handling-policy=disable_non_proxied_udp"


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def unused_port() -> int:
    """A loopback port nothing listens on (closed again before the test uses it)."""
    return free_port()


def _healthy(port: int) -> bool:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=0.5)
    try:
        connection.request("GET", "/health")
        return connection.getresponse().status == 200
    except OSError:
        return False
    finally:
        connection.close()


@pytest.fixture(scope="session")
def app_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    workdir: Path = tmp_path_factory.mktemp("app")
    port = free_port()
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(workdir),
        "HOST": "127.0.0.1",
        "PORT": str(port),
        "NO_PROXY": "*",
        "no_proxy": "*",
    }
    log = workdir / "stderr.log"  # a file, not a pipe: a full pipe would block the app
    with log.open("wb") as stderr:
        server = subprocess.Popen(
            [sys.executable, "-P", "-m", "app"],
            cwd=workdir,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=stderr,
        )
    try:
        deadline = time.monotonic() + START_TIMEOUT
        while not _healthy(port):
            if server.poll() is not None:
                # The exit code only: from M1 a crash could carry prompt text (rule 10).
                pytest.fail(f"the app exited with {server.returncode} (stderr: {log})")
            if time.monotonic() > deadline:
                pytest.fail(f"the app did not answer /health within {START_TIMEOUT} s")
            time.sleep(0.1)
        yield f"http://127.0.0.1:{port}"
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=5)


@pytest.fixture(scope="session")
def browser(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Browser]:
    # Chromium gets an environment built from scratch too: no proxy, token or key from
    # the shell or the runner.
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path_factory.mktemp("browser"))}
    with sync_playwright() as playwright:
        chromium = playwright.chromium.launch(args=[NO_NAMES, NO_WEBRTC_UDP], env=env)
        try:
            yield chromium
        finally:
            chromium.close()
