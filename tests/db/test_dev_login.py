"""From the dev login to row-level security (plan, PR 7; threat-model path 2).

Each advisor logs in through the shim and lists their mandates through /api/mandates,
which runs inside advisor_transaction(): the session's advisor goes into
set_config('app.advisor_id', ..., true), and the policies do the scoping. The superuser,
which bypasses row-level security, computes the expected sets. The demo mandate is
planted for its advisor, and one more mandate for each other advisor.

Forged identity inputs ride along with a real session: headers, query parameters and
cookies that name another advisor leave the response exactly as it was.
"""

from __future__ import annotations

import socket
from collections.abc import Iterator

import pytest
from dbtools import SUPERUSER_URL, Connection, column
from fastapi.testclient import TestClient
from psycopg import sql
from seed_rows import MINE, OTHER, insert

from app.auth import dev_shim
from app.config import branding
from app.config.settings import Settings, env_name
from app.db import names
from app.db.connect import connect
from app.db.session import make_pool
from app.main import create_app

pytestmark = pytest.mark.usefixtures("database", "seeded")

LOOPBACK = "127.0.0.1"
PORT = 8000
BASE = f"http://{LOOPBACK}:{PORT}"
FORM = {"Origin": BASE, "Content-Type": "application/x-www-form-urlencoded"}
PLANT = "dev-login-plant"
MANDATES = sql.Identifier(names.SCHEMA, "mandates")


@pytest.fixture(scope="module")
def planted(seeded: None) -> Iterator[None]:
    """The demo mandate for its advisor, and one more mandate for every other advisor."""
    rows = [
        {"advisor_id": branding.DEMO_MANDATE.advisor_id, "id": branding.DEMO_MANDATE.id}
        | {"family": branding.DEMO_MANDATE.family}
    ]
    rows += [
        {"advisor_id": person.id, "id": f"{PLANT}-{person.id}", "family": "Plantfamily"}
        for person in branding.ADVISORS
        if person.id != branding.DEMO_MANDATE.advisor_id
    ]
    with connect(SUPERUSER_URL, autocommit=True) as conn:
        inserted = [
            row for row in rows if insert(conn, f"{names.SCHEMA}.mandates", row, if_absent=True)
        ]
        try:
            yield
        finally:
            for row in inserted:
                conn.execute(
                    sql.SQL("DELETE FROM {} WHERE advisor_id = %s AND id = %s").format(MANDATES),
                    (row["advisor_id"], row["id"]),
                )


@pytest.fixture
def app_client(monkeypatch: pytest.MonkeyPatch, planted: None) -> Iterator[TestClient]:
    monkeypatch.setenv(env_name("dev_auth"), "1")
    monkeypatch.setenv(env_name("port"), str(PORT))
    app = create_app(Settings(_env_file=None), bind_host=LOOPBACK)
    with TestClient(app, base_url=BASE) as test_client:
        yield test_client


def expected(superuser: Connection, advisor_id: str) -> set[str]:
    query = sql.SQL("SELECT id FROM {} WHERE advisor_id = %s").format(MANDATES)
    return set(column(superuser, query, (advisor_id,)))


def mandates_of(test_client: TestClient, advisor_id: str, **kwargs: object) -> tuple[int, list]:
    test_client.cookies.clear()
    response = test_client.post("/dev/login", content=f"advisor={advisor_id}", headers=FORM)
    assert response.status_code == 200, f"login as {advisor_id}: {response.status_code}"
    listed = test_client.get("/api/mandates", **kwargs)  # type: ignore[arg-type]
    return listed.status_code, listed.json()


@pytest.mark.parametrize("person", branding.ADVISORS, ids=lambda p: p.id)
def test_login_scopes_queries(
    app_client: TestClient, superuser: Connection, person: branding.Person
) -> None:
    status, body = mandates_of(app_client, person.id)
    assert status == 200, status
    ids = {row["id"] for row in body}
    assert len(ids) == len(body), f"duplicate rows: {body}"
    assert ids, f"{person.name} sees no mandate"
    assert ids == expected(superuser, person.id), f"{person.name}: {sorted(ids)}"
    assert all(set(row) == {"id", "family"} for row in body), body


def test_login_scopes_are_disjoint(app_client: TestClient) -> None:
    seen = {
        person.id: {row["id"] for row in mandates_of(app_client, person.id)[1]}
        for person in branding.ADVISORS
    }
    assert all(seen.values()), seen
    ids = list(seen)
    overlaps = [
        (a, b, seen[a] & seen[b])
        for i, a in enumerate(ids)
        for b in ids[i + 1 :]
        if seen[a] & seen[b]
    ]
    assert not overlaps, overlaps
    assert branding.DEMO_MANDATE.id in seen[branding.DEMO_MANDATE.advisor_id], seen


FORGED = [
    {"headers": {"X-Dev-Advisor": OTHER.id}},
    {"headers": {"X-Advisor-Id": OTHER.id}},
    {"headers": {"X-MS-CLIENT-PRINCIPAL-NAME": OTHER.email}},
    {"headers": {"Authorization": f"Bearer {OTHER.id}"}},
    {"params": {"advisor": OTHER.id}},
    {"params": {"advisor_id": OTHER.id}},
]


@pytest.mark.parametrize("forged", FORGED, ids=lambda f: next(iter(next(iter(f.values())))))
def test_forged_identity_inputs(app_client: TestClient, forged: dict) -> None:
    plain = mandates_of(app_client, MINE.id)
    other = mandates_of(app_client, OTHER.id)
    assert plain[0] == 200 and plain != other, "control: the two advisors differ"
    assert mandates_of(app_client, MINE.id, **forged) == plain, f"{forged} changed the answer"


def test_forged_identity_cookie(app_client: TestClient) -> None:
    plain = mandates_of(app_client, MINE.id)
    app_client.cookies.set("advisor", OTHER.id)
    assert app_client.get("/api/mandates").json() == plain[1], "an advisor cookie counted"
    assert dev_shim.COOKIE in app_client.cookies, "control: the session cookie is still sent"


def test_mandates_with_the_database_down(app_client: TestClient, settings: Settings) -> None:
    # A loopback port where nothing listens, and a short pool timeout: 503, not 500.
    with socket.socket() as probe:
        probe.bind((LOOPBACK, 0))
        closed = probe.getsockname()[1]
    url = f"postgresql://{names.APP_ROLE}@{LOOPBACK}:{closed}/{names.DATABASE}"
    status, _ = mandates_of(app_client, MINE.id)
    assert status == 200, "control: the database answers"
    with make_pool(url, max_size=1, timeout=0.5) as down:
        app_client.app.state.pool = down  # type: ignore[attr-defined]
        response = app_client.get("/api/mandates")
    assert response.status_code == 503, response.status_code
    assert names.APP_ROLE not in response.text and str(closed) not in response.text
