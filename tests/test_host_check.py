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
    # The header itself: httpx would refuse or normalise some of these in a URL.
    response = client(settings).get(path, headers={"host": host})
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
    # A name is allowed for a bind off loopback only (review finding sec-r5).
    monkeypatch.setenv(env_name("host"), "0.0.0.0")  # noqa: S104 - the image's bind
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


def test_the_shim_allows_loopback_alone_whatever_the_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Review findings test-4 and sec-8: with another host allowed for the app (on a
    # loopback bind, localhost is the one other name it may allow), the shim still
    # refuses it, and 127.0.0.1 on the port still passes both checks.
    monkeypatch.setenv(env_name("dev_auth"), "1")
    port = Settings(_env_file=None).port
    monkeypatch.setenv(ALLOWED_HOSTS, f"localhost:{port},127.0.0.1:{port}")
    shimmed = Settings(_env_file=None)
    assert host_check.allowed_hosts(shimmed) >= {f"localhost:{port}".encode()}
    app = create_app(shimmed, bind_host="127.0.0.1")
    base = f"http://127.0.0.1:{port}"
    with TestClient(app, base_url=base, client=("127.0.0.1", 50000)) as c:
        assert c.get("/health").status_code == 200
        assert c.get("/health", headers={"host": f"localhost:{port}"}).status_code == 400


@pytest.mark.parametrize("variant", [str.upper, lambda h: h + ".", lambda h: h + " ", str.title])
def test_a_configured_host_is_matched_exactly(monkeypatch: pytest.MonkeyPatch, variant) -> None:
    # Review finding test-8: no case folding, no trimming, no trailing dot.
    staff = f"{branding.SLUG}.{branding.DOMAINS.staff}"
    monkeypatch.setenv(env_name("host"), "0.0.0.0")  # noqa: S104 - the image's bind
    monkeypatch.setenv(ALLOWED_HOSTS, staff)
    settings = Settings(_env_file=None)
    served = client(settings, staff)
    assert served.get("/health").status_code == 200
    assert served.get("/health", headers={"host": variant(staff)}).status_code == 400


def test_the_port_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    # Review finding test-11.
    monkeypatch.setenv(ALLOWED_HOSTS, "127.0.0.1:65535,127.0.0.1:1")
    assert Settings(_env_file=None).allowed_host_values == ("127.0.0.1:65535", "127.0.0.1:1")
    monkeypatch.setenv(ALLOWED_HOSTS, "127.0.0.1:65536")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_the_bare_name_is_ignored(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    monkeypatch.setenv("ALLOWED_HOSTS", f"{branding.DOMAINS.attacker}")
    assert Settings(_env_file=None).allowed_host_values == (f"127.0.0.1:{settings.port}",)


@pytest.mark.parametrize(
    "name", [branding.SLUG, f"{branding.SLUG}:8000", "advisor-box.local", "x.local:8000"]
)
def test_a_name_that_is_resolved_locally_is_refused(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    # Review finding sec-5: a single-label name (search domains, LLMNR) or a .local name
    # (mDNS) can be answered by anyone on the network, which reopens DNS rebinding.
    monkeypatch.setenv(ALLOWED_HOSTS, name)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
    monkeypatch.setenv(ALLOWED_HOSTS, "localhost:8000")
    assert Settings(_env_file=None).allowed_host_values == ("localhost:8000",)


@pytest.mark.parametrize(
    "value", [f"{branding.SLUG}.{branding.DOMAINS.staff}", "advisor.home.arpa", "192.0.2.1:8000"]
)
def test_a_loopback_bind_allows_loopback_only(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    # Review finding sec-r5: on 127.0.0.1 whoever answers the client's DNS can rebind any
    # name, so the list may hold 127.0.0.1 and localhost only; a name or another address
    # is for a deployment that binds elsewhere (the image binds 0.0.0.0).
    monkeypatch.setenv(ALLOWED_HOSTS, value)
    with pytest.raises(ValidationError) as refused:
        Settings(_env_file=None)
    assert ALLOWED_HOSTS in str(refused.value)
    monkeypatch.setenv(env_name("host"), "0.0.0.0")  # noqa: S104 - the image's bind
    assert Settings(_env_file=None).allowed_host_values == (value,)
    monkeypatch.setenv(env_name("host"), "127.0.0.1")
    monkeypatch.setenv(ALLOWED_HOSTS, "127.0.0.1:9000,localhost:9000")
    assert Settings(_env_file=None).allowed_host_values == ("127.0.0.1:9000", "localhost:9000")


def test_the_lifespan_passes_through() -> None:
    reached: list[str] = []

    async def app(scope: dict, receive: Any, send: Any) -> None:
        reached.append(scope["type"])

    async def nothing() -> dict:
        return {}

    async def ignore(message: dict) -> None:
        return None

    middleware = host_check.HostCheckMiddleware(app, frozenset({b"127.0.0.1:8000"}))
    asyncio.run(middleware({"type": "lifespan"}, nothing, ignore))
    assert reached == ["lifespan"]


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


@pytest.mark.usefixtures("restore_logging")
def test_a_refused_allowlist_names_the_variable_at_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    # Review finding sec-r1: with the hooks installed first, a refused setting must still
    # say which variable and why, exit 2 and bind nothing; never the value.
    import uvicorn

    from app import __main__ as entry

    def no_bind(self: uvicorn.Config) -> Any:
        raise AssertionError("bound before refusing")

    monkeypatch.setattr(uvicorn.Config, "bind_socket", no_bind)
    monkeypatch.chdir(tmp_path)
    value = f"{branding.SLUG}probe"
    monkeypatch.setenv(ALLOWED_HOSTS, value)
    assert entry.main() == 2
    err = capsys.readouterr().err
    assert "refused to start: invalid settings" in err and ALLOWED_HOSTS in err, err
    assert value not in err, err
