"""The read path in Chromium (M1 plan, PR 12): sign in, ask in the page, see a cited answer.

The app is tests/web/ask_server.py: create_app() with the dev login on 127.0.0.1, the
real database (`make load`'s corpus) and embedder, and a scripted model that searches
for the demo family and cites what the advisor's own search returned. Chromium resolves
no name and reaches no address but 127.0.0.1 (tests/web/conftest.py).

- The demo advisor signs in through the login form, opens the advisor page (M1 PR 11),
  types the question into its ask box and presses Ask. The page's request reaches the
  real route through the real shim: exactly `{"question": ...}`, the app's exact Origin
  (a same-origin fetch is "cors" mode, where the no-referrer policy does not null the
  Origin, unlike the login form's navigation). The page shows the answer and its
  sources: every listed record is the demo advisor's document or a product sheet, with
  its title from the database and its kind; at least one is the advisor's own. The
  route's JSON carries no-store; no CSP violation fires and no request leaves the origin.
- Another advisor asks the same in the page and is shown sources, none of them, and
  nothing on the page, the demo advisor's.
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
# What the page shows for each kind (app/web/static/app.js, KINDS).
SHOWN_KINDS = {"document": "document", "product_sheet": "product sheet"}
LISTEN_FOR_VIOLATIONS = """
window.__violations = [];
document.addEventListener("securitypolicyviolation", (event) => {
  window.__violations.push(event.effectiveDirective);
});
"""


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


def ask_in_the_page(page: Page, app_url: str) -> Any:
    """Opens the advisor page, types the question and presses Ask; the route's response,
    once the page shows the answer."""
    assert page.goto(app_url + "/").status == 200
    page.fill("#question", QUESTION)
    with page.expect_response(app_url + ROUTE) as answered:
        page.click("#ask button[type=submit]")
    assert answered.value.status == 200, answered.value.text()[:200]
    # Not wait_for_function: the page's CSP refuses the eval it needs (#34).
    page.wait_for_selector("#result:not([hidden])")
    return answered.value


def shown_sources(page: Page) -> list[tuple[str, str, str]]:
    """(record id, title, kind) of each source the page lists, in order."""
    items = page.locator("#citations li")
    return [
        (
            items.nth(i).locator(".record").text_content() or "",
            items.nth(i).locator(".title").text_content() or "",
            items.nth(i).locator(".kind").text_content() or "",
        )
        for i in range(items.count())
    ]


def test_the_demo_advisor_asks_in_the_page_and_sees_own_sources(
    context: BrowserContext, ask_app_url: str
) -> None:
    owner, titles, sheets = corpus_rows()
    requests: list[str] = []
    context.on("request", lambda request: requests.append(request.url))
    page = context.new_page()
    sign_in(page, ask_app_url, MINE)
    response = ask_in_the_page(page, ask_app_url)

    sent = response.request
    assert sent.method == "POST"
    assert sent.post_data_json == {"question": QUESTION}, "the page sent another body"
    assert sent.header_value("origin") == ask_app_url
    assert response.header_value("content-type") == "application/json"
    assert response.header_value("cache-control") == "no-store"
    body = response.json()
    assert body["stop"] == "end_turn" and body["proposals"] == []

    sources = shown_sources(page)
    assert sources, "the page lists no source"
    assert [record for record, _, _ in sources] == [c["record_id"] for c in body["citations"]]
    for record, title, kind in sources:
        assert owner.get(record) == MINE.id or record in sheets, (
            f"{record} is not the demo advisor's"
        )
        assert title == titles[record]
        expected = "product_sheet" if record in sheets else "document"
        assert kind == SHOWN_KINDS[expected]
    assert any(owner.get(record) == MINE.id for record, _, _ in sources)
    assert page.locator("#answer").text_content() == body["answer"]
    assert page.locator("#trace").text_content() == body["trace_id"]
    assert body["trace_id"] == response.header_value("x-trace-id")

    assert page.evaluate("window.__violations") == []
    assert ask_app_url + ROUTE in requests  # non-vacuity
    foreign = [url for url in requests if origin(url) != origin(ask_app_url)]
    assert foreign == [], foreign


def test_another_advisor_asks_the_same_and_sees_nothing_of_theirs(
    context: BrowserContext, ask_app_url: str
) -> None:
    owner, _, _ = corpus_rows()
    page = context.new_page()
    sign_in(page, ask_app_url, OTHER)
    response = ask_in_the_page(page, ask_app_url)
    sources = shown_sources(page)
    assert sources, "control: the other advisor's search returned nothing to cite"
    assert all(owner.get(record) != MINE.id for record, _, _ in sources), sources
    theirs = [record for record, advisor in owner.items() if advisor == MINE.id]
    shown = page.locator("body").text_content() or ""
    leaked = [record for record in theirs if record in shown or record in response.text()]
    assert leaked == [], f"the demo advisor's record ids shown to another advisor: {leaked[:3]}"


def test_control_a_page_on_another_port_is_refused_by_the_origin_check(
    context: BrowserContext, ask_app_url: str, unused_port: int
) -> None:
    # The classic forged form: a page on another local port posts to /api/ask in a
    # top-level navigation (text/plain, so no preflight). 127.0.0.1 on another port is
    # the same site, so the SameSite=Strict session cookie goes with it; only the Origin
    # check stops it. A navigation, not fetch(): Cross-Origin-Resource-Policy would block
    # a cross-origin fetch's answer before the test could read its status.
    page = context.new_page()
    sign_in(page, ask_app_url, MINE)
    elsewhere = f"http://127.0.0.1:{unused_port}"
    form = (
        f'<!doctype html><form method="post" action="{ask_app_url}{ROUTE}" enctype="text/plain">'
        """<input name='{"question": "q", "x": "' value='"}'><button>go</button></form>"""
    )
    context.route(
        elsewhere + "/",
        lambda route: route.fulfill(body=form, content_type="text/html"),
    )
    page.goto(elsewhere + "/")
    with page.expect_response(ask_app_url + ROUTE) as answered:
        page.click("button")
    refused = answered.value
    assert refused.request.method == "POST"
    assert refused.request.header_value("origin") == elsewhere
    cookie = refused.request.header_value("cookie") or ""
    assert f"{dev_shim.COOKIE}=" in cookie, "control: the session cookie was not sent"
    assert refused.status == 403, refused.status
