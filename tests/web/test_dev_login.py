"""The dev login in a real browser (ADR 0009): Chromium signs in through the form, under
the app's CSP and the shim's Origin check.

The app sends Referrer-Policy: no-referrer, under which a browser sends `Origin: null`
even on a same-origin POST, and the shim refuses that. The login page sets its own
policy to same-origin, so the form's POST carries http://127.0.0.1:<port>. The control
shows the browser's POST is the one the shim checks: the same click from a page served
with no-referrer only is refused.

No database runs in the web job: a signed-in /api/mandates answers 503 there and 200
with a local database, both past the identity; a signed-out one answers 401.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from playwright.sync_api import Browser, BrowserContext, Route

from app.config import branding

ADVISOR = next(p for p in branding.ADVISORS if p.id == branding.DEMO_MANDATE.advisor_id)
REFERRER_META = '<meta name="referrer" content="same-origin">'


@pytest.fixture
def context(browser: Browser) -> Iterator[BrowserContext]:
    context = browser.new_context(service_workers="block")
    yield context
    context.close()


def test_dev_login_in_the_browser(context: BrowserContext, dev_app_url: str) -> None:
    page = context.new_page()
    assert page.goto(dev_app_url + "/api/mandates").status == 401, "signed in before login"
    response = page.goto(dev_app_url + "/dev/login")
    assert response is not None and response.status == 200
    with page.expect_response(dev_app_url + "/dev/login") as posted:
        page.click(f'button[value="{ADVISOR.id}"]')
    assert posted.value.request.method == "POST"
    assert posted.value.status == 200, f"the form's POST was refused: {posted.value.status}"
    assert posted.value.request.header_value("origin") == dev_app_url
    assert f"Signed in as {ADVISOR.name}" in page.content()
    cookies = context.cookies(dev_app_url)
    assert [c["httpOnly"] for c in cookies] == [True] and cookies[0]["sameSite"] == "Strict"
    listed = page.goto(dev_app_url + "/api/mandates")
    # 503 without a database (the web job), 200 with one: past the identity either way.
    assert listed is not None and listed.status in (200, 503), f"signed in: {listed.status}"


def test_the_signed_in_page_leads_to_the_ask_box(context: BrowserContext, dev_app_url: str) -> None:
    # The page after the login links to the advisor page; following it is a GET of /
    # on the same origin, which shows the ask box.
    page = context.new_page()
    page.goto(dev_app_url + "/dev/login")
    with page.expect_response(dev_app_url + "/dev/login") as posted:
        page.click(f'button[value="{ADVISOR.id}"]')
    assert posted.value.status == 200, posted.value.status
    with page.expect_response(dev_app_url + "/") as opened:
        page.click("text=Ask a question")
    assert opened.value.request.method == "GET"
    assert opened.value.status == 200, opened.value.status
    assert page.url == dev_app_url + "/"
    assert page.locator("#question").is_visible()
    assert page.locator("#ask button[type=submit]").is_visible()


def test_no_referrer_page_sends_origin_null(context: BrowserContext, dev_app_url: str) -> None:
    # Control: the same page without the meta element; the browser sends Origin: null.
    def without_meta(route: Route) -> None:
        response = route.fetch()
        body = response.text()
        assert REFERRER_META in body, "the login page has no referrer meta element"
        route.fulfill(response=response, body=body.replace(REFERRER_META, ""))

    context.route(
        dev_app_url + "/dev/login",
        lambda route: without_meta(route) if route.request.method == "GET" else route.continue_(),
    )
    page = context.new_page()
    page.goto(dev_app_url + "/dev/login")
    with page.expect_response(dev_app_url + "/dev/login") as posted:
        page.click(f'button[value="{ADVISOR.id}"]')
    assert posted.value.request.header_value("origin") == "null"
    assert posted.value.status == 403, posted.value.status
    assert context.cookies(dev_app_url) == []
