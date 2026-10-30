"""The page in a real browser: it loads, carries the CSP, takes its title from branding,
and makes no request to another origin.

Service workers are blocked and WebSockets are listened for. Playwright may not see a
request that the CSP blocks, so `securitypolicyviolation` events are collected as well.
Two controls show that both listeners see what they are meant to catch.
"""

from __future__ import annotations

from collections.abc import Iterator
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import Browser, BrowserContext, Page
from playwright.sync_api import Error as PlaywrightError

from app.config import branding

LISTEN_FOR_VIOLATIONS = """
window.__violations = [];
document.addEventListener("securitypolicyviolation", (event) => {
  window.__violations.push(event.effectiveDirective);
});
"""


def origin(url: str) -> tuple[str, str]:
    parts = urlsplit(url)
    return parts.scheme, parts.netloc


class Recorder:
    def __init__(self, context: BrowserContext) -> None:
        self.requests: list[str] = []
        self.websockets: list[str] = []
        context.on("request", lambda request: self.requests.append(request.url))
        context.on("page", self._watch)

    def _watch(self, page: Page) -> None:
        page.on("websocket", lambda websocket: self.websockets.append(websocket.url))


def _context(browser: Browser, *, bypass_csp: bool) -> BrowserContext:
    context = browser.new_context(service_workers="block", bypass_csp=bypass_csp)
    context.add_init_script(LISTEN_FOR_VIOLATIONS)
    return context


@pytest.fixture
def context(browser: Browser) -> Iterator[BrowserContext]:
    context = _context(browser, bypass_csp=False)
    yield context
    context.close()


@pytest.fixture
def context_without_csp(browser: Browser) -> Iterator[BrowserContext]:
    context = _context(browser, bypass_csp=True)
    yield context
    context.close()


def test_smoke(context: BrowserContext, app_url: str) -> None:
    recorder = Recorder(context)
    page = context.new_page()
    response = page.goto(app_url + "/")
    assert response is not None and response.status == 200, response
    csp = response.header_value("content-security-policy") or ""
    assert "img-src 'self'" in csp, csp
    assert page.title() == f"{branding.PRODUCT.name} — {branding.BANK.display_name}"
    page.wait_for_load_state("networkidle")

    assert page.evaluate("window.__violations") == []
    assert app_url + "/" in recorder.requests, recorder.requests  # non-vacuity
    foreign = [url for url in recorder.requests if origin(url) != origin(app_url)]
    assert foreign == [], foreign
    assert recorder.websockets == [], recorder.websockets


def test_control_a_blocked_inline_script_is_a_violation(
    context: BrowserContext, app_url: str
) -> None:
    page = context.new_page()
    page.goto(app_url + "/")
    page.evaluate(
        "() => { const s = document.createElement('script');"
        " s.textContent = 'window.ran = 1'; document.head.appendChild(s); }"
    )
    page.wait_for_function("window.__violations.length > 0", timeout=5000)
    assert page.evaluate("window.__violations") == ["script-src-elem"]
    assert page.evaluate("window.ran") is None


def test_control_a_foreign_origin_request_is_recorded(
    context_without_csp: BrowserContext, app_url: str, unused_port: int
) -> None:
    # With the CSP bypassed, an image on another loopback port is requested (and fails:
    # nothing listens there). Another origin, no name to resolve, no egress.
    recorder = Recorder(context_without_csp)
    page = context_without_csp.new_page()
    page.goto(app_url + "/")
    elsewhere = f"http://127.0.0.1:{unused_port}/pixel.png"
    with page.expect_event("requestfailed", timeout=5000):
        page.evaluate("(url) => { const i = new Image(); i.src = url; }", elsewhere)
    foreign = [url for url in recorder.requests if origin(url) != origin(app_url)]
    assert foreign == [elsewhere], recorder.requests


def test_control_names_do_not_resolve(context: BrowserContext) -> None:
    # The resolver rules answer "not found" for any name, so nothing leaves by DNS.
    page = context.new_page()
    with pytest.raises(PlaywrightError, match="ERR_NAME_NOT_RESOLVED"):
        page.goto(f"http://{branding.DOMAINS.canary}/", timeout=5000)
