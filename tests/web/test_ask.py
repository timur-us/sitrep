"""The read path in Chromium (M1 plan, PR 12): sign in, ask, get a cited answer.

The app is tests/web/ask_server.py: create_app() with the dev login on 127.0.0.1, the
real database (`make load`'s corpus) and embedder, and a scripted model that searches
for the demo family and cites what the advisor's own search returned. Chromium resolves
no name and reaches no address but 127.0.0.1 (tests/web/conftest.py).

- The demo advisor signs in through the login form, opens the app's page, and asks. The
  ask is the page's own fetch(), same-origin, as the UI sends it: until PR 11's ask box
  is merged there is no form to type into, so the page runs the request itself. It
  carries the app's exact Origin under the app's no-referrer policy (a fetch is "cors"
  mode, where the policy does not null the Origin, unlike the login form's navigation).
  The answer cites the demo advisor's own documents, titles from the database, as JSON
  with no-store; no CSP violation fires and no request leaves the origin.
- Another advisor asks the same and gets no record id of the demo advisor's.
- Control: a page on another local port (same site, so the SameSite=Strict cookie goes
  with it) posts to /api/ask with the session cookie, and the Origin check refuses it.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import Browser, BrowserContext, Page
from psycopg import sql

from app.auth import dev_shim
from app.config import branding
from app.config.settings import loopback_database_url
from app.db import names
from app.db.connect import connect

DEMO = branding.DEMO_MANDATE
MINE = next(person for person in branding.ADVISORS if person.id == DEMO.advisor_id)
OTHER = next(person for person in branding.ADVISORS if person.id != MINE.id)
QUESTION = f"What do I need to know before the call with {DEMO.family}?"
ROUTE = "/api/ask"
LISTEN_FOR_VIOLATIONS = """
window.__violations = [];
document.addEventListener("securitypolicyviolation", (event) => {
  window.__violations.push(event.effectiveDirective);
});
"""
ASK = """async ([route, question]) => {
  const response = await fetch(route, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({question}),
  });
  return {status: response.status, body: await response.text()};
}"""


def origin(url: str) -> tuple[str, str]:
    parts = urlsplit(url)
    return parts.scheme, parts.netloc


def corpus_rows() -> tuple[dict[str, str], dict[str, str], set[str]]:
    """Each document's advisor, each record's title, and the sheets, as the superuser
    sees them; fails unless `make load` has run."""
    with connect(loopback_database_url("postgres")) as su:
        documents = su.execute(
            sql.SQL("SELECT id, advisor_id, title FROM {}").format(
                sql.Identifier(names.SCHEMA, "documents")
            )
        ).fetchall()
        sheets = su.execute(
            sql.SQL("SELECT id, title FROM {}").format(
                sql.Identifier(names.SCHEMA, "product_sheets")
            )
        ).fetchall()
    owner = {record: advisor for record, advisor, _ in documents}
    if MINE.id not in owner.values():
        pytest.fail("no corpus in the database: run `make load` (make web-test does)")
    titles = {record: title for record, _, title in documents} | dict(sheets)
    return owner, titles, {record for record, _ in sheets}


@pytest.fixture
def context(browser: Browser) -> Iterator[BrowserContext]:
    context = browser.new_context(service_workers="block")
    context.add_init_script(LISTEN_FOR_VIOLATIONS)
    yield context
    context.close()


def sign_in(page: Page, app_url: str, person: branding.Person) -> None:
    page.goto(app_url + "/dev/login")
    with page.expect_response(app_url + "/dev/login") as posted:
        page.click(f'button[value="{person.id}"]')
    assert posted.value.status == 200, f"login as {person.id}: {posted.value.status}"


def ask_from_the_page(page: Page, app_url: str) -> tuple[Any, dict[str, Any]]:
    """Opens the app's page and asks from it; the network response and what fetch() saw."""
    assert page.goto(app_url + "/").status == 200
    with page.expect_response(app_url + ROUTE) as answered:
        seen = page.evaluate(ASK, [ROUTE, QUESTION])
    return answered.value, seen


def test_the_demo_advisor_asks_in_the_browser_and_gets_own_sources(
    context: BrowserContext, ask_app_url: str
) -> None:
    owner, titles, sheets = corpus_rows()
    requests: list[str] = []
    context.on("request", lambda request: requests.append(request.url))
    page = context.new_page()
    sign_in(page, ask_app_url, MINE)
    response, seen = ask_from_the_page(page, ask_app_url)

    assert response.status == 200 == seen["status"], seen["body"][:200]
    assert response.request.method == "POST"
    assert response.request.header_value("origin") == ask_app_url
    assert response.header_value("content-type") == "application/json"
    assert response.header_value("cache-control") == "no-store"
    body = response.json()
    assert body["stop"] == "end_turn" and body["proposals"] == []
    citations = body["citations"]
    assert citations, "no citation"
    for item in citations:
        record = item["record_id"]
        assert owner.get(record) == MINE.id or record in sheets, (
            f"{record} is not the demo advisor's"
        )
        assert item["title"] == titles[record]
    assert any(owner.get(item["record_id"]) == MINE.id for item in citations)
    assert body["trace_id"] == response.header_value("x-trace-id")

    assert page.evaluate("window.__violations") == []
    assert ask_app_url + ROUTE in requests  # non-vacuity
    foreign = [url for url in requests if origin(url) != origin(ask_app_url)]
    assert foreign == [], foreign


def test_another_advisor_asks_the_same_and_gets_nothing_of_theirs(
    context: BrowserContext, ask_app_url: str
) -> None:
    owner, _, _ = corpus_rows()
    page = context.new_page()
    sign_in(page, ask_app_url, OTHER)
    response, seen = ask_from_the_page(page, ask_app_url)
    assert response.status == 200, seen["body"][:200]
    assert response.json()["citations"], (
        "control: the other advisor's search returned nothing to cite"
    )
    theirs = [record for record, advisor in owner.items() if advisor == MINE.id]
    leaked = [record for record in theirs if record in seen["body"]]
    assert leaked == [], (
        f"the demo advisor's record ids in the other advisor's answer: {leaked[:3]}"
    )


def test_control_a_page_on_another_port_is_refused_by_the_origin_check(
    context: BrowserContext, ask_app_url: str, unused_port: int
) -> None:
    page = context.new_page()
    sign_in(page, ask_app_url, MINE)
    elsewhere = f"http://127.0.0.1:{unused_port}/"
    context.route(
        elsewhere,
        lambda route: route.fulfill(
            body="<!doctype html><title>x</title>", content_type="text/html"
        ),
    )
    page.goto(elsewhere)
    # A simple request (text/plain, no preflight), so the POST itself is sent, with the
    # cookie: 127.0.0.1 on another port is the same site.
    with page.expect_response(ask_app_url + ROUTE) as answered:
        page.evaluate(
            """(url) => fetch(url, {method: "POST", mode: "no-cors", credentials: "include",
                 headers: {"Content-Type": "text/plain"}, body: '{"question": "q"}'})""",
            ask_app_url + ROUTE,
        )
    refused = answered.value
    assert refused.request.header_value("origin") == elsewhere.rstrip("/")
    assert f"{dev_shim.COOKIE}=" in (refused.request.header_value("cookie") or ""), "no cookie"
    assert refused.status == 403, refused.status
