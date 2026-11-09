"""The Host check on every route, with or without the dev login (M1 PR 5, ADR 0009).

Timur's answer 4 to the PR 7 questions: "Host check without the shim in M1", before any
route serves data without it. A page on any origin can make a browser send requests to
the app, and a name that resolves to 127.0.0.1 (DNS rebinding) can read the answers:
only a Host on the allowlist is served. The allowlist is exactly 127.0.0.1:<port> unless
the allowed-hosts setting (env_name("allowed_hosts")) names others (the image's smoke test; M3's Azure host). The dev
login's own Host check is the same function.

None of these tests needs the database: the routes answer before they touch it.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import branding
from app.config.settings import Settings, env_name
from app.http import host_check
from app.http.security_headers import CSP
from app.main import create_app

ALLOWED_HOSTS = env_name("allowed_hosts")
ROUTES = ("/", "/health", "/api/mandates")


def client(settings: Settings, host: str | None = None, **kwargs: Any) -> TestClient:
    base = f"http://{host or f'127.0.0.1:{settings.port}'}"
    return TestClient(create_app(settings), base_url=base, follow_redirects=False, **kwargs)


def test_the_default_is_loopback_on_the_port(settings: Settings) -> None:
    assert host_check.allowed_hosts(settings) == frozenset({f"127.0.0.1:{settings.port}".encode()})


@pytest.mark.parametrize(
    "host",
    [
        branding.DOMAINS.attacker,
        f"{branding.DOMAINS.attacker}:8000",
        "localhost:8000",
        "127.0.0.1",
        "127.0.0.1:8001",
        "127.0.0.2:8000",
        "[::1]:8000",
        "127.0.0.1:8000.",
        "127.0.0.1:08000",
        "0.0.0.0:8000",
        "testserver",
    ],
)
@pytest.mark.parametrize("path", ROUTES)
def test_another_host_is_refused_without_the_shim(settings: Settings, host: str, path: str) -> None:
    response = client(settings, host).get(path)
    assert response.status_code == 400, (host, path, response.status_code)
    assert response.text == "Bad Request"
    # The refusal carries the app's headers, and is no redirect.
    assert response.headers["content-security-policy"] == CSP
    assert "location" not in response.headers


def test_loopback_on_the_port_is_served(settings: Settings) -> None:
    served = client(settings)
    assert served.get("/").status_code == 200
    assert served.get("/health").json() == {"status": "ok"}
    # No login without the shim: the route refuses before it touches the database.
    assert served.get("/api/mandates").status_code == 401


def test_the_allowlist_comes_from_the_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    staff = f"{branding.SLUG}.{branding.DOMAINS.staff}"
    monkeypatch.setenv(ALLOWED_HOSTS, f"{staff},127.0.0.1:9999")
    settings = Settings(_env_file=None)
    assert host_check.allowed_hosts(settings) == frozenset({staff.encode(), b"127.0.0.1:9999"})
    assert client(settings, staff).get("/health").status_code == 200
    assert client(settings, "127.0.0.1:9999").get("/health").status_code == 200
    # The list replaces the default.
    assert client(settings, f"127.0.0.1:{settings.port}").get("/health").status_code == 400


@pytest.mark.parametrize(
    "value",
    [
        "http://127.0.0.1:8000",
        "127.0.0.1:8000/",
        f"*.{branding.DOMAINS.staff}",
        "127.0.0.1:8000, localhost:8000",
        "127.0.0.1:8000,,localhost:8000",
        "LOCALHOST:8000",
        "127.0.0.1:",
        "127.0.0.1:99999",
        "127.0.0.1:0x50",
        "user@127.0.0.1:8000",
        "[::1]:8000",
        "127.0.0.1:8000\n",
        "höst:8000",
        ",",
    ],
)
def test_an_allowlist_of_another_shape_is_refused_unseen(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv(ALLOWED_HOSTS, value)
    with pytest.raises(ValidationError) as refused:
        Settings(_env_file=None)
    message = str(refused.value)
    assert ALLOWED_HOSTS in message
    assert len(value.strip()) < 3 or value.strip() not in message, message


def _asgi(headers: list[tuple[bytes, bytes]], kind: str = "http") -> list[dict]:
    """Run one request through the middleware alone; the messages it sends."""
    sent: list[dict] = []
    reached: list[bool] = []

    async def app(scope: dict, receive: Any, send: Any) -> None:
        reached.append(True)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive() -> dict:
        return {"type": "http.request", "body": b""}

    async def send(message: dict) -> None:
        sent.append(message)

    scope = {"type": kind, "method": "GET", "path": "/health", "headers": headers}
    if kind == "http":
        scope["client"] = ("127.0.0.1", 50000)
    middleware = host_check.HostCheckMiddleware(app, frozenset({b"127.0.0.1:8000"}))
    asyncio.run(middleware(scope, receive, send))
    return [*sent, {"reached": bool(reached)}]


@pytest.mark.parametrize(
    "headers",
    [
        [],
        [(b"host", b"127.0.0.1:8000"), (b"host", b"127.0.0.1:8000")],
        [(b"host", b"127.0.0.1:8000"), (b"host", branding.DOMAINS.attacker.encode())],
        [(b"host", b"")],
    ],
)
def test_a_missing_or_repeated_host_is_refused(headers: list[tuple[bytes, bytes]]) -> None:
    messages = _asgi(headers)
    assert messages[0]["status"] == 400 and messages[-1] == {"reached": False}


def test_one_allowed_host_reaches_the_app() -> None:
    messages = _asgi([(b"host", b"127.0.0.1:8000")])
    assert messages[0]["status"] == 200 and messages[-1] == {"reached": True}


def test_a_websocket_is_closed() -> None:
    # The app has none; uvicorn runs with ws="none" anyway.
    messages = _asgi([(b"host", b"127.0.0.1:8000")], kind="websocket")
    assert messages[0] == {"type": "websocket.close", "code": 1008}
    assert messages[-1] == {"reached": False}


def test_the_shim_checks_with_the_same_function(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    monkeypatch.setenv(env_name("dev_auth"), "1")
    shimmed = Settings(_env_file=None)
    calls: list[frozenset[bytes]] = []
    real = host_check.host_allowed

    def spy(headers: list[tuple[bytes, bytes]], allowed: frozenset[bytes]) -> bool:
        calls.append(allowed)
        return real(headers, allowed)

    monkeypatch.setattr(host_check, "host_allowed", spy)
    app = create_app(shimmed, bind_host="127.0.0.1")
    with TestClient(
        app, base_url=f"http://127.0.0.1:{shimmed.port}", client=("127.0.0.1", 50000)
    ) as c:
        assert c.get("/health").status_code == 200
    own = frozenset({f"127.0.0.1:{shimmed.port}".encode()})
    # Once for the app's check, once for the shim's, each with loopback on the port.
    assert calls == [own, own], calls
