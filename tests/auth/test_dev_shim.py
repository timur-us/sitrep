"""The dev login shim, in process (plan, PR 7). No database: the routes under test answer
before one is needed.

- The shim is mounted only when the (prefixed) DEV_AUTH setting is exactly "1" and the
  entry point binds exactly 127.0.0.1; otherwise /dev/login is 404 and /api/mandates is
  401. Behind it, every request must come from a 127.0.0.1 peer (review finding sec-2).
- Identity comes from the session cookie only: headers, query parameters and other
  cookies that name an advisor change nothing; a tampered cookie, one from another
  process and one replayed after logout are 401; only advisors can log in.
- Browser origins: every request needs Host exactly 127.0.0.1:<port> (DNS rebinding),
  and every non-GET request an Origin of exactly http://127.0.0.1:<port> (CSRF), with
  Sec-Fetch-Site same-origin when it is sent.

The responses of real sessions against the database are in tests/db/test_dev_login.py.
"""

from __future__ import annotations

from collections.abc import Iterator

import anyio
import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.auth import dev_shim
from app.config import branding
from app.config.settings import Settings, env_name
from app.main import create_app

LOOPBACK = "127.0.0.1"
PORT = 8000
BASE = f"http://{LOOPBACK}:{PORT}"
ORIGIN = {"Origin": BASE}
MINE = next(p for p in branding.ADVISORS if p.id == branding.DEMO_MANDATE.advisor_id)
OTHER = next(p for p in branding.ADVISORS if p is not MINE)
NOT_ADVISORS = [person for person in branding.CAST if person not in branding.ADVISORS]
OFF_VALUES = [None, "0", "true", "yes", "on", " 1", "01", "+1", "１"]


def dev_settings(monkeypatch: pytest.MonkeyPatch, value: str | None = "1") -> Settings:
    if value is None:
        monkeypatch.delenv(env_name("dev_auth"), raising=False)
    else:
        monkeypatch.setenv(env_name("dev_auth"), value)
    monkeypatch.setenv(env_name("port"), str(PORT))
    return Settings(_env_file=None)


FORM = {**ORIGIN, "Content-Type": "application/x-www-form-urlencoded"}
# The TCP peer TestClient reports; its default ("testclient", 50000) is no address.
PEER = (LOOPBACK, 50000)


def client(
    settings: Settings, bind_host: str | None = LOOPBACK, peer: tuple[str, int] = PEER
) -> TestClient:
    # No lifespan: the pool stays closed, and nothing here needs the database.
    return TestClient(create_app(settings, bind_host=bind_host), base_url=BASE, client=peer)


@pytest.fixture
def shim(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    yield client(dev_settings(monkeypatch))


def login(test_client: TestClient, advisor_id: str) -> str:
    response = test_client.post(
        "/dev/login",
        content=f"advisor={advisor_id}",
        headers={**ORIGIN, "Content-Type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 200, f"login as {advisor_id}: {response.status_code}"
    cookie = response.cookies.get(dev_shim.COOKIE)
    assert cookie, f"no {dev_shim.COOKIE} cookie after login"
    return cookie


def whoami(test_client: TestClient, cookie: str | None, **kwargs: object) -> int:
    """The status of /api/mandates with only this cookie: 401 when no session is
    accepted, 503 when one is (the pool is closed here, so the route gets past the
    identity and stops at the database)."""
    test_client.cookies.clear()
    if cookie is not None:
        test_client.cookies.set(dev_shim.COOKIE, cookie)
    return test_client.get("/api/mandates", **kwargs).status_code  # type: ignore[arg-type]


# Off unless exactly "1", and only behind the entry point's 127.0.0.1.


@pytest.mark.parametrize("value", OFF_VALUES, ids=repr)
def test_shim_off_unless_exactly_1(monkeypatch: pytest.MonkeyPatch, value: str | None) -> None:
    off = client(dev_settings(monkeypatch, value))
    assert off.get("/dev/login").status_code == 404, f"{value!r} mounted the shim"
    assert off.get("/api/mandates").status_code == 401
    # Control: exactly "1" mounts it.
    on = client(dev_settings(monkeypatch, "1"))
    assert on.get("/dev/login").status_code == 200, "exactly 1 did not mount the shim"


@pytest.mark.parametrize("bind_host", [None, "0.0.0.0", "localhost", "::1", "127.0.0.2"])  # noqa: S104
def test_factory_without_entry_point_has_no_shim(
    monkeypatch: pytest.MonkeyPatch, bind_host: str | None
) -> None:
    settings = dev_settings(monkeypatch)
    app = client(settings, bind_host=bind_host)
    assert app.get("/dev/login").status_code == 404, f"bind_host {bind_host!r} mounted the shim"
    assert app.get("/api/mandates").status_code == 401
    assert client(settings).get("/dev/login").status_code == 200, "control: 127.0.0.1"


def test_login_page_lists_the_advisors(shim: TestClient) -> None:
    response = shim.get("/dev/login")
    assert response.status_code == 200
    for person in branding.ADVISORS:
        assert f'value="{person.id}"' in response.text, f"{person.name} is not listed"
    for person in NOT_ADVISORS:
        assert f'value="{person.id}"' not in response.text, f"{person.name} is listed"
    assert "<script" not in response.text


def test_login_sets_a_strict_session_cookie(shim: TestClient) -> None:
    response = shim.post(
        "/dev/login",
        content=f"advisor={MINE.id}",
        headers={**ORIGIN, "Content-Type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 200, response.status_code
    cookies = response.headers.get_list("set-cookie")
    assert len(cookies) == 1, cookies
    attributes = [part.strip().lower() for part in cookies[0].split(";")]
    name, _, value = cookies[0].split(";")[0].partition("=")
    assert name == dev_shim.COOKIE and value, cookies[0]
    assert MINE.id not in value, "the cookie names the advisor"
    assert {"httponly", "samesite=strict", "path=/"} <= set(attributes), attributes
    assert not any(a.startswith(("domain", "max-age", "expires")) for a in attributes), attributes
    assert response.headers.get("location") is None, "the login redirects"


# Identity from the session only.


FORGED_HEADERS = [
    {"X-Dev-Advisor": OTHER.id},
    {"X-Advisor-Id": OTHER.id},
    {"X-MS-CLIENT-PRINCIPAL-NAME": OTHER.email},
    {"Authorization": f"Bearer {OTHER.id}"},
    {"Cookie": f"advisor={OTHER.id}"},
]


@pytest.mark.parametrize("headers", FORGED_HEADERS, ids=lambda h: next(iter(h)))
def test_forged_identity_without_a_cookie_is_401(shim: TestClient, headers: dict) -> None:
    # Control: a real session is accepted (503: the pool is closed, past the identity).
    cookie = login(shim, MINE.id)
    assert whoami(shim, cookie) == 503, "control: the session was not accepted"
    assert whoami(shim, None, headers=headers) == 401, f"{headers} was taken as a login"
    assert whoami(shim, None, params={"advisor": OTHER.id}) == 401, "?advisor= was a login"


def test_tampered_cookie_is_401(shim: TestClient) -> None:
    cookie = login(shim, MINE.id)
    assert whoami(shim, cookie) == 503, "control: the session was not accepted"
    session_id, _, signature = cookie.rpartition(".")
    flipped = signature[:-1] + ("0" if signature[-1] != "0" else "1")
    for forged in (
        f"{session_id}.{flipped}",
        f"{session_id[:-1]}{'A' if session_id[-1] != 'A' else 'B'}.{signature}",
        session_id,
        f"{session_id}.",
        f".{signature}",
        MINE.id,
        "",
    ):
        assert whoami(shim, forged) == 401, f"a forged cookie was accepted: {forged!r}"


def test_cookie_from_another_process_is_401(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = dev_settings(monkeypatch)
    first, second = client(settings), client(settings)
    cookie = login(first, MINE.id)
    assert whoami(first, cookie) == 503, "control: the session was not accepted"
    assert whoami(second, cookie) == 401, "another app instance accepted the cookie"


def test_cookie_replayed_after_logout_is_401(shim: TestClient) -> None:
    cookie = login(shim, MINE.id)
    assert whoami(shim, cookie) == 503, "control: the session was not accepted"
    shim.cookies.set(dev_shim.COOKIE, cookie)
    response = shim.post("/dev/logout", headers=ORIGIN)
    assert response.status_code == 200, response.status_code
    assert whoami(shim, cookie) == 401, "the cookie still works after logout"


def test_login_again_ends_the_old_session(shim: TestClient) -> None:
    first = login(shim, MINE.id)
    # One cookie only: the shim takes two cookies of its name as none (the next test).
    shim.cookies.clear()
    shim.cookies.set(dev_shim.COOKIE, first)
    second = login(shim, OTHER.id)
    assert first != second
    assert whoami(shim, second) == 503, "control: the new session was not accepted"
    assert whoami(shim, first) == 401, "the old session survived a new login"


def test_two_session_cookies_are_none(shim: TestClient) -> None:
    # A cookie another local port sets for 127.0.0.1 arrives next to the real one;
    # with two, neither is taken (cookie tossing).
    cookie = login(shim, MINE.id)
    shim.cookies.clear()
    other = login(shim, OTHER.id)
    # Control: both sessions are live, each on its own.
    assert (whoami(shim, cookie), whoami(shim, other)) == (503, 503), "control"
    pair = f"{dev_shim.COOKIE}={cookie}; {dev_shim.COOKIE}={other}"
    assert whoami(shim, None, headers={"Cookie": pair}) == 401, "two session cookies"


@pytest.mark.parametrize("person", NOT_ADVISORS, ids=lambda p: p.role.value)
def test_only_advisors_can_log_in(shim: TestClient, person: branding.Person) -> None:
    login(shim, MINE.id)  # control: an advisor can
    shim.cookies.clear()
    response = shim.post(
        "/dev/login",
        content=f"advisor={person.id}",
        headers={**ORIGIN, "Content-Type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 403, f"{person.role.value}: {response.status_code}"
    assert dev_shim.COOKIE not in response.cookies


@pytest.mark.parametrize(
    "body",
    [
        f"advisor={MINE.id.upper()}",
        f"advisor={MINE.id}&advisor={OTHER.id}",
        f"advisor={MINE.id}%00",
        f" advisor={MINE.id}",
        f"advisor= {MINE.id}",
        f"user={MINE.id}",
        "",
    ],
)
def test_login_body_is_exact(shim: TestClient, body: str) -> None:
    # Review finding test-8: a malformed body is 400; 403 is for a well-formed id that
    # names no advisor (test_unknown_ids_are_403).
    response = shim.post("/dev/login", content=body, headers=FORM)
    assert response.status_code == 400, f"{body!r}: {response.status_code}"
    assert dev_shim.COOKIE not in response.cookies
    login(shim, MINE.id)  # control


@pytest.mark.parametrize("advisor_id", ["root", "admin", "zzz", "a" * 32])
def test_unknown_ids_are_403(shim: TestClient, advisor_id: str) -> None:
    # Review finding test-3: well-formed ids outside the cast.
    response = shim.post("/dev/login", content=f"advisor={advisor_id}", headers=FORM)
    assert response.status_code == 403, f"{advisor_id!r}: {response.status_code}"
    assert dev_shim.COOKIE not in response.cookies


@pytest.mark.parametrize(
    "content_type",
    [
        None,
        "application/json",
        "multipart/form-data; boundary=x",
        "text/plain",
        "application/x-www-form-urlencoded; charset=utf-8",
        "Application/X-WWW-Form-Urlencoded",
    ],
    ids=repr,
)
def test_login_content_type_is_exact(shim: TestClient, content_type: str | None) -> None:
    # Review findings spec-3 and test-3: only the form type a browser's form sends.
    headers = dict(ORIGIN) if content_type is None else {**ORIGIN, "Content-Type": content_type}
    response = shim.post("/dev/login", content=f"advisor={MINE.id}", headers=headers)
    assert response.status_code == 400, f"{content_type!r}: {response.status_code}"
    assert dev_shim.COOKIE not in response.cookies
    login(shim, MINE.id)  # control: the exact type


def test_long_login_body_is_400(shim: TestClient) -> None:
    # Review finding test-3. The pattern alone would refuse this body as well; what the
    # cap adds (the body is not buffered past MAX_BODY) is not visible from outside.
    body = f"advisor={MINE.id}" + "a" * (dev_shim.MAX_BODY * 4)
    response = shim.post("/dev/login", content=body, headers=FORM)
    assert response.status_code == 400, response.status_code
    assert dev_shim.COOKIE not in response.cookies


# Browser origins.


@pytest.mark.parametrize(
    "host",
    [
        branding.DOMAINS.attacker,
        f"{branding.DOMAINS.attacker}:{PORT}",
        f"localhost:{PORT}",
        LOOPBACK,
        f"{LOOPBACK}:9999",
        f"{LOOPBACK}:{PORT}.{branding.DOMAINS.attacker}",
        f"[::1]:{PORT}",
        # Review finding test-9: these replace a no-op upper() on digits.
        f"{LOOPBACK}:{PORT} ",
        f"{LOOPBACK}:0{PORT}",
        f"{LOOPBACK}.:{PORT}",
        f"LOCALHOST:{PORT}",
    ],
)
def test_host_must_be_exact(shim: TestClient, host: str) -> None:
    assert shim.get("/dev/login").status_code == 200, "control: the right Host"
    for path in ("/dev/login", "/health", "/", "/api/mandates"):
        response = shim.get(path, headers={"Host": host})
        assert response.status_code == 400, f"Host {host!r} on {path}: {response.status_code}"
        assert branding.DOMAINS.attacker not in response.text, "the Host is echoed"


@pytest.mark.parametrize(
    "origin",
    [
        None,
        "null",
        f"http://localhost:{PORT}",
        f"http://{LOOPBACK}:9999",
        f"http://{LOOPBACK}.{branding.DOMAINS.attacker}:{PORT}",
        f"https://{LOOPBACK}:{PORT}",
        f"http://{LOOPBACK}",
        f"{BASE}/",
        f"http://{branding.DOMAINS.attacker}",
    ],
    ids=repr,
)
@pytest.mark.parametrize("path", ["/dev/login", "/dev/logout"])
def test_browser_origin_defences(shim: TestClient, origin: str | None, path: str) -> None:
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if origin is not None:
        headers["Origin"] = origin
    response = shim.post(path, content=f"advisor={MINE.id}", headers=headers)
    assert response.status_code == 403, f"Origin {origin!r} on {path}: {response.status_code}"
    assert dev_shim.COOKIE not in response.cookies
    control = shim.post(path, content=f"advisor={MINE.id}", headers={**headers, **ORIGIN})
    assert control.status_code == 200, f"control: the right Origin on {path}"


@pytest.mark.parametrize("site", ["cross-site", "same-site", "none", "same-origin, cross-site"])
def test_sec_fetch_site_must_be_same_origin(shim: TestClient, site: str) -> None:
    headers = {**ORIGIN, "Content-Type": "application/x-www-form-urlencoded"}
    refused = shim.post(
        "/dev/login", content=f"advisor={MINE.id}", headers={**headers, "Sec-Fetch-Site": site}
    )
    assert refused.status_code == 403, f"Sec-Fetch-Site {site!r}: {refused.status_code}"
    allowed = shim.post(
        "/dev/login",
        content=f"advisor={MINE.id}",
        headers={**headers, "Sec-Fetch-Site": "same-origin"},
    )
    assert allowed.status_code == 200, "control: same-origin"


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
def test_every_method_but_get_needs_the_origin(shim: TestClient, method: str) -> None:
    response = shim.request(method, "/health")
    assert response.status_code == 403, f"{method} without Origin: {response.status_code}"
    assert shim.get("/health").status_code == 200, "control: GET needs no Origin"


def test_refusals_carry_the_security_headers(shim: TestClient) -> None:
    refused = [
        shim.get("/health", headers={"Host": branding.DOMAINS.attacker}),
        shim.post("/dev/login", content=f"advisor={MINE.id}"),
    ]
    assert [r.status_code for r in refused] == [400, 403]
    for response in refused:
        assert response.headers.get("cache-control") == "no-store", response.headers
        assert "default-src 'none'" in response.headers.get("content-security-policy", "")


# Review finding test-3: one header of each, as the comparison of lists means.


@pytest.mark.parametrize(
    ("name", "status"), [("Host", 400), ("Origin", 403), ("Sec-Fetch-Site", 403)]
)
def test_duplicate_headers_are_refused(shim: TestClient, name: str, status: int) -> None:
    right = {"Host": f"{LOOPBACK}:{PORT}", "Origin": BASE, "Sec-Fetch-Site": "same-origin"}
    single = [("Content-Type", "application/x-www-form-urlencoded"), *right.items()]
    assert shim.post("/dev/login", content=f"advisor={MINE.id}", headers=single).status_code == (
        200
    ), "control: one of each"
    doubled = [*single, (name, right[name])]
    response = shim.post("/dev/login", content=f"advisor={MINE.id}", headers=doubled)
    assert response.status_code == status, f"two {name} headers: {response.status_code}"
    assert dev_shim.COOKIE not in response.cookies


def _with_websocket(settings: Settings, bind_host: str | None) -> TestClient:
    app = create_app(settings, bind_host=bind_host)

    async def echo(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.close()

    app.add_api_websocket_route("/probe-ws", echo)
    return TestClient(app, base_url=BASE, client=PEER)


def test_websockets_are_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    # Review finding test-3: the shim closes any connection that is not HTTP, so none
    # goes around its Host check. Since M1 PR 5 the app's own Host check closes them too,
    # shim or not. Control: the same route on a bare app accepts.
    settings = dev_settings(monkeypatch)
    bare = FastAPI()

    async def echo(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.close()

    bare.add_api_websocket_route("/probe-ws", echo)
    with TestClient(bare, base_url=BASE).websocket_connect("/probe-ws") as socket:
        assert socket is not None, "control: the route accepts on a bare app"
    unshimmed = _with_websocket(settings, bind_host=None)
    with pytest.raises(WebSocketDisconnect) as closed, unshimmed.websocket_connect("/probe-ws"):
        pass
    assert closed.value.code == 1008, closed.value.code
    shimmed = _with_websocket(settings, bind_host=LOOPBACK)
    with pytest.raises(WebSocketDisconnect) as closed, shimmed.websocket_connect("/probe-ws"):
        pass
    assert closed.value.code == 1008, closed.value.code


# Review finding sec-2: the shim does not take create_app()'s bind_host on its caller's
# word. With proxy_headers off, scope["client"] is the TCP peer, which a remote host
# cannot choose; anything but 127.0.0.1 is refused, on every route.


@pytest.mark.parametrize(
    "peer",
    [("192.0.2.1", 50000), ("testclient", 50000), ("::1", 50000), ("127.0.0.2", 50000)],
    ids=lambda peer: peer[0],
)
def test_peer_must_be_loopback(monkeypatch: pytest.MonkeyPatch, peer: tuple[str, int]) -> None:
    settings = dev_settings(monkeypatch)
    assert client(settings).get("/dev/login").status_code == 200, "control: a 127.0.0.1 peer"
    remote = client(settings, peer=peer)
    for path in ("/dev/login", "/health", "/", "/api/mandates"):
        status = remote.get(path).status_code
        assert status == 403, f"peer {peer[0]} on {path}: {status}"
    response = remote.post("/dev/login", content=f"advisor={MINE.id}", headers=FORM)
    assert response.status_code == 403, f"peer {peer[0]} logged in: {response.status_code}"
    assert dev_shim.COOKIE not in response.cookies


def test_scope_without_a_peer_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    # A Unix socket gives no peer address (scope["client"] is None).
    app = create_app(dev_settings(monkeypatch), bind_host=LOOPBACK)
    sent: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/dev/login",
        "raw_path": b"/dev/login",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", f"{LOOPBACK}:{PORT}".encode())],
        "client": None,
        "server": (LOOPBACK, PORT),
        "state": {},
    }

    anyio.run(app, scope, receive, send)
    starts = [message["status"] for message in sent if message["type"] == "http.response.start"]
    assert starts == [403], starts
