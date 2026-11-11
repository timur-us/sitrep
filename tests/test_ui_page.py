"""The advisor page, read without a browser (M1 PR 11): the two assets it loads, the
markup's shape, and a scan of app.js for the sinks that would turn model output into
markup, a request or a link.

The page renders untrusted model output (threat model, path 1): the image and link
exfiltration paths run through it. The browser half is tests/web/test_answer.py. The
scan is a lint over our own code, not a proof: it reads the text, so a sink reached
through a computed property name (`el[name] = value`) is reported only because the
scan also refuses computed member assignment.
"""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import branding
from app.config.settings import Settings
from app.main import create_app, index_html

STATIC = Path(__file__).resolve().parents[1] / "app" / "web" / "static"
SCRIPT = "/static/app.js"
STYLE = "/static/app.css"
# Written out here, not imported, so that a change in the app shows up as a failure.
ASSETS = {SCRIPT: ("app.js", "text/javascript"), STYLE: ("app.css", "text/css")}
MAX_QUESTION = 2000

# Elements the page itself may contain. No link, image, frame, embed, base or inline
# style anywhere; one script, from the page's own origin.
ALLOWED_ELEMENTS = frozenset(
    {
        "html", "head", "meta", "title", "link", "script", "body", "main", "header",
        "h1", "h2", "p", "form", "label", "textarea", "button", "section", "div",
        "ol", "dl", "dt", "dd", "span",
    }
)  # fmt: skip
# Elements app.js may create. The text of each is set through textContent.
CREATED_ELEMENTS = frozenset({"li", "span"})


class Page(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.elements: list[tuple[str, dict[str, str | None]]] = []
        self.text_in: dict[str, str] = {}
        self._open: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.elements.append((tag, dict(attrs)))
        self._open.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in self._open:
            del self._open[len(self._open) - 1 - self._open[::-1].index(tag) :]

    def handle_data(self, data: str) -> None:
        if self._open:
            tag = self._open[-1]
            self.text_in[tag] = self.text_in.get(tag, "") + data

    def all(self, tag: str) -> list[dict[str, str | None]]:
        return [attrs for name, attrs in self.elements if name == tag]


def parse(markup: str) -> Page:
    page = Page()
    page.feed(markup)
    page.close()
    return page


@pytest.fixture
def client(settings: Settings) -> TestClient:
    return TestClient(create_app(settings), base_url="http://127.0.0.1:8000")


# --- the assets ---------------------------------------------------------------------


@pytest.mark.parametrize("path", sorted(ASSETS))
def test_assets_are_served(client: TestClient, path: str) -> None:
    name, media_type = ASSETS[path]
    response = client.get(path)
    assert response.status_code == 200, f"{path}: {response.status_code}"
    assert response.headers["content-type"] == f"{media_type}; charset=utf-8", response.headers
    assert response.content == (STATIC / name).read_bytes()
    assert response.content.strip(), f"{name} is empty"
    # The page's headers (is_page): the CSP and nosniff, no no-store.
    assert "script-src 'self'" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "cache-control" not in response.headers


@pytest.mark.parametrize(
    "path",
    [
        "/static/index.html",
        "/static/app.js.map",
        "/static/APP.JS",
        "/static/app.js/",
        "/static/../main.py",
        "/static/%2e%2e/main.py",
        "/static/",
    ],
)
def test_nothing_else_under_static(client: TestClient, path: str) -> None:
    # Two exact routes, no directory mount: nothing else in the package is served.
    response = client.get(path)
    assert response.status_code == 404, f"{path}: {response.status_code}"


# --- the markup ---------------------------------------------------------------------


def test_page_loads_its_own_assets_only() -> None:
    page = parse(index_html())
    tags = {tag for tag, _ in page.elements}
    assert tags <= ALLOWED_ELEMENTS, f"unexpected elements: {sorted(tags - ALLOWED_ELEMENTS)}"
    scripts = page.all("script")
    assert scripts == [{"src": SCRIPT, "defer": None}], scripts
    assert page.text_in.get("script", "").strip() == "", "inline script"
    links = page.all("link")
    assert links == [{"rel": "stylesheet", "href": STYLE}], links
    for tag, attrs in page.elements:
        for name, value in attrs.items():
            assert not name.startswith("on"), f"<{tag} {name}>: an inline handler"
            assert name != "style", f"<{tag} style>: an inline style"
            assert "//" not in (value or ""), f"<{tag} {name}={value!r}>: an absolute URL"
        if tag == "meta":
            assert "http-equiv" not in attrs, f"<meta {attrs}>"


def test_page_takes_its_names_from_branding() -> None:
    markup = index_html()
    page = parse(markup)
    title = f"{branding.PRODUCT.name} — {branding.BANK.display_name}"
    assert page.text_in.get("title") == title
    assert page.text_in.get("h1") == title
    assert html.escape(branding.PRODUCT.tagline) in markup
    assert "{{" not in markup, "a placeholder was not filled in"


def test_question_box_sends_nothing_without_the_script() -> None:
    # Without app.js a submitted form would put its named fields into a URL. The box has
    # no name, so a native submit carries no question; the form posts nowhere else.
    page = parse(index_html())
    [form] = page.all("form")
    assert "action" not in form and "method" not in form, form
    [box] = page.all("textarea")
    assert "name" not in box, box
    assert box.get("maxlength") == str(MAX_QUESTION), box
    assert box.get("id") == "question", box


def test_grounding_wording() -> None:
    # The number guard proves provenance, not truth (threat model, path 7; PR 9 sec-9).
    for name in ("index.html", "app.js"):
        text = (STATIC / name).read_text(encoding="utf-8")
        assert not re.search(r"verif", text, re.IGNORECASE), f"{name} says 'verified'"
    assert "found in the sources" in (STATIC / "app.js").read_text(encoding="utf-8")


# --- the sink scan ------------------------------------------------------------------

# Each pattern is a way from a string to markup, a request, a navigation, a new
# resource or code. app.js uses none of them.
SINKS = {
    "innerHTML": r"\binnerHTML\b",
    "outerHTML": r"\bouterHTML\b",
    "insertAdjacentHTML": r"\binsertAdjacentHTML\b",
    "document.write": r"\bwrite(?:ln)?\s*\(",
    "eval": r"\beval\s*\(",
    "Function": r"\bFunction\s*\(",
    "string timer": r"\bset(?:Timeout|Interval)\s*\(\s*[\"'`]",
    "setAttribute": r"\bsetAttribute(?:NS)?\b",
    "attribute node": r"\b(?:createAttribute|setAttributeNode)\b",
    "src": r"\.src\b",
    "href": r"\.href\b",
    "srcdoc": r"\bsrcdoc\b",
    "action": r"\.(?:action|formAction)\b",
    "DOMParser": r"\bDOMParser\b",
    "createContextualFragment": r"\bcreateContextualFragment\b",
    "setHTMLUnsafe": r"\b(?:setHTML|setHTMLUnsafe|parseHTMLUnsafe)\b",
    "import()": r"\bimport\s*\(",
    "Image": r"\bnew\s+Image\b",
    "open": r"\bopen\s*\(",
    "location": r"\blocation\b",
    "XMLHttpRequest": r"\bXMLHttpRequest\b",
    "sendBeacon": r"\bsendBeacon\b",
    "WebSocket": r"\bWebSocket\b",
    "EventSource": r"\bEventSource\b",
    "postMessage": r"\bpostMessage\b",
    "cssText": r"\bcssText\b",
    "style": r"\.style\b",
    "createElementNS": r"\bcreateElementNS\b",
    "computed member assignment": r"\]\s*=[^=]",
    "document.cookie": r"\bcookie\b",
}
CREATE_ELEMENT = re.compile(r"\bcreateElement\s*\(\s*(?:\"([a-z]+)\"|'([a-z]+)'|([^)]*))\)")
FETCH = re.compile(r"\bfetch\s*\(\s*([^,)]*)")
BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
LINE_COMMENT = re.compile(r"^\s*//.*$", re.MULTILINE)


def code(source: str) -> str:
    """The source without block comments and whole-line comments. A trailing comment
    stays, and is scanned: the rule errs towards reporting."""
    return LINE_COMMENT.sub("", BLOCK_COMMENT.sub("", source))


def scan(source: str) -> list[str]:
    body = code(source)
    found = [f"{name}: {m.group(0)!r}" for name, p in SINKS.items() for m in re.finditer(p, body)]
    for match in CREATE_ELEMENT.finditer(body):
        tag = match.group(1) or match.group(2)
        if tag not in CREATED_ELEMENTS:
            found.append(f"createElement: {match.group(0)!r}")
    for match in FETCH.finditer(body):
        if match.group(1).strip() != "ASK":
            found.append(f"fetch: {match.group(0)!r}")
    if re.search(r"\bASK\b", body) and not re.search(r"\bconst ASK = \"/api/ask\";", body):
        found.append("fetch: ASK is not the constant /api/ask")
    return found


@pytest.mark.parametrize(
    "planted",
    [
        "out.innerHTML = data.answer;",
        "out.outerHTML = x;",
        "out.insertAdjacentHTML('beforeend', x);",
        "document.write(x);",
        "document.writeln(x);",
        "eval(x);",
        "new Function(x)();",
        "setTimeout('go()', 1);",
        "setInterval(`go()`, 1);",
        "el.setAttribute('href', x);",
        "el.setAttributeNS(null, 'src', x);",
        "el.setAttributeNode(document.createAttribute('src'));",
        "img.src = x;",
        "a.href = x;",
        "frame.srcdoc = x;",
        "form.action = x;",
        "button.formAction = x;",
        "new DOMParser().parseFromString(x, 'text/html');",
        "document.createRange().createContextualFragment(x);",
        "el.setHTML(x);",
        "el.setHTMLUnsafe(x);",
        "import(x);",
        "const i = new Image();",
        "window.open(x);",
        "location = x;",
        "window.location.assign(x);",
        "new XMLHttpRequest();",
        "navigator.sendBeacon(x, y);",
        "new WebSocket(x);",
        "new EventSource(x);",
        "parent.postMessage(x, '*');",
        "el.style.cssText = x;",
        "el.style.backgroundImage = x;",
        "document.createElementNS(ns, 'svg');",
        "el[name] = x;",
        "document.cookie;",
        "document.createElement('img');",
        'document.createElement("a");',
        "document.createElement(tag);",
        "fetch(x);",
        "fetch('/api/ask', {});",
        'const ASK = "https://x.example/";\nfetch(ASK, {});',
        "out.innerHTML = x; // a trailing comment does not hide it",
    ],
)
def test_scan_reports_a_planted_sink(planted: str) -> None:
    assert scan(planted), f"not reported: {planted!r}"


@pytest.mark.parametrize(
    "allowed",
    [
        "// innerHTML is never used here",
        "/* el.innerHTML = x; */",
        "/*\n * document.write(x)\n */",
        "out.textContent = data.answer;",
        "document.createElement('li');",
        'document.createElement("span");',
        'const ASK = "/api/ask";\nfetch(ASK, { method: "POST" });',
        "if (a[0] === b) {}",
    ],
)
def test_scan_passes_the_allowed_shapes(allowed: str) -> None:
    assert scan(allowed) == [], scan(allowed)


def test_app_js_has_no_sink() -> None:
    source = (STATIC / "app.js").read_text(encoding="utf-8")
    body = code(source)
    # Non-vacuity: the scan reads the real script, which renders through textContent,
    # creates its list items, and asks through exactly one fetch of the constant.
    assert len(re.findall(r"\.textContent\s*=", body)) >= 8, "the script renders nothing"
    assert len(FETCH.findall(body)) == 1, FETCH.findall(body)
    assert {m.group(1) or m.group(2) for m in CREATE_ELEMENT.finditer(body)} == CREATED_ELEMENTS
    assert scan(source) == [], "\n".join(scan(source))


def test_app_css_loads_nothing() -> None:
    # A stylesheet can fetch too: url(), @import and image-set() are refused.
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    assert css.strip(), "app.css is empty"
    assert not re.search(r"url\s*\(|@import|image-set|@font-face", css, re.IGNORECASE), css
