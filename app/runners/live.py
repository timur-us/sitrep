"""The live recorder: `python -P -m app.runners.evals --live --yes` (ADR 0014).

By hand only, by Timur, after the cost estimate (`--estimate`; plan default 17): it calls
the model with the key from .env and spends money. It records each case's samples into
`recorded/<prompt version>/<case id>.json` (app/runners/transcript.py).

**No new identity path.** Each sample asks through the app's own routes, in process:
create_app() with the dev login on (ADR 0009), then `POST /dev/login` as the case's
advisor and `POST /api/ask` with the session cookie, the shim's Host and Origin, over an
ASGI scope built here with the peer 127.0.0.1. The login, the rate limit, the loop, the
READ handlers and row-level security are the ones the browser meets; only the entry
point's socket checks do not apply, since nothing binds (plan, "What is shaky"). Each
sample gets a fresh app, so the rate limit of 10 asks a minute does not throttle a run.

**The model client** is the app's own (app/harness/model_client.py), wrapped in
RecordingClient, which keeps a copy of every request and reply. The key is read the
way `python -P -m app` reads it: from .env only, refused while it is exported
(app/__main__.py), with every SDK variable refused by the factory. Without a key, or with
another model than the default (a recording would be stale), the run exits 2 before any
call. The embedding model and the database are the app's: a 503 stops the run.

**The date** is read once, when the run starts, and passed to the app as its date, so
every sample of a run has the same prompt; each sample records it.

**What it records** besides the transcript: the advisor it logged in as, and the loop's
error word from each ask's span (`guard` for a broken boundary). **Limits:** a 401, 403
or 503 stops the run; so does spending past the ceiling the estimate prints for these
asks, and a usage the price table cannot price. Any other failure stops it naming only
the exception's type (python -m app.runners.evals installs the entry point's logging
config and exception hooks first).

Nothing here logs a question, an answer or a tool result. The output names cases,
statuses, stops and cost.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import os
import sys
from collections.abc import Awaitable, Callable, Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor, TracerProvider
from pydantic import ValidationError

from app.api import ask
from app.config.settings import DEFAULT_MODEL, LOOPBACK, Settings, SettingsRefused
from app.embeddings.base import Embedder
from app.harness.model import ModelClient
from app.harness.model_client import (
    ModelClientRefused,
    PriceUnknown,
    build_model_client,
    cost_usd,
)
from app.harness.prompt import PROMPT_VERSION
from app.main import create_app
from app.observability import tracing
from app.observability.tracing import PlaintextRefused
from app.runners import estimate, transcript
from app.runners.scoring import Case

# Read from the working directory, as the entry point reads it.
ENV_FILE = ".env"
# The app's port for the Host and Origin checks; nothing binds it.
PORT = 8000
PEER = (LOOPBACK, 50000)
BASE = f"http://{LOOPBACK}:{PORT}"
FORM = "application/x-www-form-urlencoded"
REFUSED = 2
# A sample with these statuses ends the run: the app cannot answer at all.
FATAL = frozenset({401, 403, 503})


class LiveError(Exception):
    """A run that cannot go on. The message names the reason, never a value."""


class RecordingClient:
    """The ModelClient of app/harness/model.py around another: every request, copied as
    JSON before the call, so a call that fails is evidence too (review finding F3), and
    the reply of every call that answered."""

    def __init__(self, inner: ModelClient) -> None:
        self._inner = inner
        self.model = inner.model
        self.requests: list[dict[str, Any]] = []
        self.replies: list[dict[str, Any]] = []

    def create(self, **request: Any) -> Mapping[str, Any]:
        self.requests.append(json.loads(json.dumps(request)))
        reply = self._inner.create(**request)
        self.replies.append(json.loads(json.dumps(dict(reply))))
        return reply


class LoopErrors(SpanProcessor):
    """The loop's error word (tracing.LOOP["error"]) from the ended spans of one ask: a
    reviewed word, never text. `guard` is a broken boundary (review finding F2)."""

    def __init__(self) -> None:
        self.kind: str | None = None

    def on_end(self, span: ReadableSpan) -> None:
        value = (span.attributes or {}).get(tracing.LOOP["error"])
        if isinstance(value, str):
            self.kind = value


# --- The app, over ASGI ---------------------------------------------------------------


async def _request(
    app: Any, method: str, path: str, headers: Sequence[tuple[str, str]], body: bytes
) -> tuple[int, dict[str, str], bytes]:
    """One HTTP request to app from 127.0.0.1: status, headers (the last of each name)
    and body."""
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(name.lower().encode(), value.encode()) for name, value in headers],
        "client": PEER,
        "server": (LOOPBACK, PORT),
    }
    done = asyncio.Event()
    pending = [{"type": "http.request", "body": body, "more_body": False}]
    status = 0
    found: dict[str, str] = {}
    chunks: list[bytes] = []

    async def receive() -> dict[str, Any]:
        if pending:
            return pending.pop()
        await done.wait()  # the client stays until the response is complete
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        nonlocal status
        if message["type"] == "http.response.start":
            status = message["status"]
            found.update(
                (name.decode("latin-1").lower(), value.decode("latin-1"))
                for name, value in message.get("headers", [])
            )
        elif message["type"] == "http.response.body":
            chunks.append(message.get("body", b""))
            if not message.get("more_body", False):
                done.set()

    await app(scope, receive, send)
    done.set()
    return status, found, b"".join(chunks)


async def _served[T](app: Any, work: Callable[[], Awaitable[T]]) -> T:
    """work() between the app's lifespan startup and shutdown (the pool opens and closes)."""
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    outbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    scope = {"type": "lifespan", "asgi": {"version": "3.0"}, "state": {}}
    task = asyncio.create_task(app(scope, inbox.get, outbox.put))
    await inbox.put({"type": "lifespan.startup"})
    if (await outbox.get())["type"] != "lifespan.startup.complete":
        raise LiveError("the app did not start")
    try:
        return await work()
    finally:
        await inbox.put({"type": "lifespan.shutdown"})
        await outbox.get()
        await task


def _cookie(headers: Mapping[str, str]) -> str:
    """`name=value` of the session cookie the login set."""
    pair = headers.get("set-cookie", "").partition(";")[0].strip()
    if "=" not in pair:
        raise LiveError("the dev login set no cookie")
    return pair


async def _ask(app: Any, advisor_id: str, question: str) -> tuple[int, Any]:
    host = ("host", f"{LOOPBACK}:{PORT}")
    origin = ("origin", BASE)
    form = [host, origin, ("content-type", FORM)]
    status, headers, _ = await _request(
        app, "POST", "/dev/login", form, f"advisor={advisor_id}".encode()
    )
    if status != 200:
        raise LiveError(f"the dev login answered {status}")
    asking = [host, origin, ("content-type", "application/json"), ("cookie", _cookie(headers))]
    body = json.dumps({"question": question}).encode()
    status, _, raw = await _request(app, "POST", ask.ROUTE, asking, body)
    try:
        payload = json.loads(raw)
    except ValueError:
        payload = None
    return status, payload


# --- Recording ------------------------------------------------------------------------


def record_sample(
    case: Case,
    *,
    client: ModelClient,
    embedder: Embedder,
    settings: Settings,
    today: datetime.date,
) -> dict[str, Any]:
    """One ask of case through a fresh app, as a transcript sample."""
    recorder = RecordingClient(client)
    errors = LoopErrors()
    provider = TracerProvider()
    provider.add_span_processor(errors)
    app = create_app(
        settings,
        tracer_provider=provider,
        bind_host=LOOPBACK,
        model_client=recorder,
        embedder=embedder,
        today=lambda: today,
    )

    async def work() -> tuple[int, Any]:
        return await _ask(app, case.advisor_id, case.question)

    status, body = asyncio.run(_served(app, work))
    systems = {request["system"] for request in recorder.requests}
    tools = {transcript.tools_sha256(request["tools"]) for request in recorder.requests}
    if len(systems) > 1 or len(tools) > 1:
        raise LiveError("the requests of one ask differ in their prompt or tools")
    last = recorder.requests[-1] if recorder.requests else {}
    return {
        "date": today.isoformat(),
        "system": last.get("system", ""),
        "messages": last.get("messages", []),  # a JSON copy already
        "replies": recorder.replies,
        "response": {"status": status, "body": body},
        "error": errors.kind,
        "_tools_sha256": tools.pop() if tools else None,
        "_spent": _spent(recorder),
    }


def _spent(recorder: RecordingClient) -> tuple[Decimal, int]:
    """What the sample's answered calls cost, from their usage, and how many replies the
    price table cannot price. A failed call may have billed too; its cost is not known
    (ADR 0012). The caller counts the priced part, then stops the run on an unpriced
    one rather than count it as nothing (review finding F6)."""
    total, unpriced = Decimal(0), 0
    for reply in recorder.replies:
        usage = reply.get("usage")
        if not isinstance(usage, dict):
            continue
        try:
            total += cost_usd(recorder.model, usage)
        except PriceUnknown:
            unpriced += 1
    return total, unpriced


def record_case(
    case: Case,
    *,
    samples: int,
    client: ModelClient,
    embedder: Embedder,
    settings: Settings,
    today: datetime.date,
    report: Callable[[str], None] = print,
    spend: Callable[[Decimal], None] = lambda cost: None,
) -> dict[str, Any]:
    """The case's recording: samples asks, each through a fresh app; spend() hears each
    sample's cost. LiveError when the app cannot answer at all (FATAL statuses)."""
    taken = []
    for number in range(1, samples + 1):
        sample = record_sample(
            case, client=client, embedder=embedder, settings=settings, today=today
        )
        cost, unpriced = sample.pop("_spent")
        spend(cost)
        if unpriced:
            raise LiveError(f"a reply's usage has no price in the table ({case.id})")
        status = sample["response"]["status"]
        body = sample["response"]["body"] if isinstance(sample["response"]["body"], dict) else {}
        report(
            f"evals: live: {case.id} sample {number}/{samples}: {status}"
            f" {body.get('stop', '-')} USD {body.get('cost_usd', '-')}"
        )
        if status in FATAL:
            raise LiveError(f"the app answered {status} ({case.id}, not written)")
        taken.append(sample)
    hashes = {sample.pop("_tools_sha256") for sample in taken} - {None}
    if len(hashes) > 1:
        raise LiveError("the samples of one case were sent different tools")
    now = transcript.current()
    return {
        "format": transcript.FORMAT,
        "case_id": case.id,
        **now,
        "tools_sha256": hashes.pop() if hashes else now["tools_sha256"],
        "model": client.model,
        "advisor": case.advisor_id,
        "samples": taken,
    }


def write(directory: Path, recording: Mapping[str, Any]) -> Path:
    """recording as `recorded/<prompt version>/<case id>.json` under the suite directory."""
    path = directory / "recorded" / PROMPT_VERSION / f"{recording['case_id']}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(recording, ensure_ascii=False, indent=1)
    path.write_text(text + "\n", encoding="utf-8")
    return path


# --- The command ----------------------------------------------------------------------


def _today() -> datetime.date:
    """The server's date: read once per run (main)."""
    return datetime.date.today()


def _settings() -> Settings:
    """The entry point's settings, from .env, with the dev login on 127.0.0.1."""
    from app.__main__ import check_environment

    check_environment(os.environ)
    return Settings(_env_file=ENV_FILE, dev_auth="1", host=LOOPBACK, port=PORT)


def main(directory: Path, cases: Sequence[Case], samples: int) -> int:
    """Record cases into directory; 2 for a run refused before any call, 1 for one that
    stopped, 0 when every case is recorded."""
    from app.__main__ import StartRefused, invalid_settings

    try:
        settings = _settings()
        client = build_model_client(settings)
    except (StartRefused, SettingsRefused, ModelClientRefused) as error:
        print(f"evals: live: refused: {error}", file=sys.stderr)
        return REFUSED
    except ValidationError as error:
        print(f"evals: live: refused: {invalid_settings(error)}", file=sys.stderr)
        return REFUSED
    if settings.trace_plaintext_on:
        print("evals: live: refused: plaintext traces belong to python -m app", file=sys.stderr)
        return REFUSED
    if client.model != DEFAULT_MODEL:
        print(
            f"evals: live: refused: the recordings name {DEFAULT_MODEL}; another model's would"
            " be stale",
            file=sys.stderr,
        )
        return REFUSED
    try:
        embedder = ask.build_embedder()
    except ask.EmbedderUnavailable:
        print(
            "evals: live: refused: the embedding model is not in place (make model)",
            file=sys.stderr,
        )
        return REFUSED
    today = _today()
    # The cap: the ceiling the estimate prints for these asks (review finding F6).
    ceiling = estimate.ceiling_per_ask() * len(cases) * samples
    spent: list[Decimal] = []

    def spend(cost: Decimal) -> None:
        spent.append(cost)
        if sum(spent) > ceiling:
            raise LiveError(f"spent past the ceiling of USD {ceiling:.2f}")

    try:
        for case in cases:
            recording = record_case(
                case,
                samples=samples,
                client=client,
                embedder=embedder,
                settings=settings,
                today=today,
                spend=spend,
            )
            write(directory, recording)
    except (LiveError, PlaintextRefused) as error:
        print(f"evals: live: stopped: {error}; USD {sum(spent)} spent", file=sys.stderr)
        return 1
    except Exception as error:  # noqa: BLE001 - its message may quote a value: the type only
        name = type(error).__name__
        print(f"evals: live: stopped: {name}; USD {sum(spent)} spent", file=sys.stderr)
        return 1
    print(
        f"evals: live: {len(cases)} cases recorded on {today.isoformat()}; USD {sum(spent)} spent"
    )
    return 0
