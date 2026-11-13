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

from opentelemetry.sdk.trace import TracerProvider
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
from app.observability.tracing import PlaintextRefused
from app.runners import transcript
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
    """The ModelClient of app/harness/model.py around another: every request and reply
    of a successful call, copied as JSON."""

    def __init__(self, inner: ModelClient) -> None:
        self._inner = inner
        self.model = inner.model
        self.requests: list[dict[str, Any]] = []
        self.replies: list[dict[str, Any]] = []

    def create(self, **request: Any) -> Mapping[str, Any]:
        reply = self._inner.create(**request)
        self.requests.append(json.loads(json.dumps(request)))
        self.replies.append(json.loads(json.dumps(dict(reply))))
        return reply


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
    app = create_app(
        settings,
        tracer_provider=TracerProvider(),
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
        "_tools_sha256": tools.pop() if tools else None,
        "_cost": _spent(recorder),
    }


def _spent(recorder: RecordingClient) -> Decimal:
    """What the sample's answered calls cost, from their usage. A failed call may have
    billed too; its cost is not known (ADR 0012)."""
    total = Decimal(0)
    for reply in recorder.replies:
        usage = reply.get("usage")
        if isinstance(usage, dict):
            try:
                total += cost_usd(recorder.model, usage)
            except PriceUnknown:
                continue
    return total


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
        spend(sample.pop("_cost"))
        status = sample["response"]["status"]
        body = sample["response"]["body"] if isinstance(sample["response"]["body"], dict) else {}
        report(
            f"evals: live: {case.id} sample {number}/{samples}: {status}"
            f" {body.get('stop', '-')} USD {body.get('cost_usd', '-')}"
        )
        if status in FATAL:
            raise LiveError(f"the app answered {status}")
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
    today = datetime.date.today()  # the server's date, once per run
    spent: list[Decimal] = []
    try:
        for case in cases:
            recording = record_case(
                case,
                samples=samples,
                client=client,
                embedder=embedder,
                settings=settings,
                today=today,
                spend=spent.append,
            )
            write(directory, recording)
    except (LiveError, PlaintextRefused) as error:
        print(f"evals: live: stopped: {error}; USD {sum(spent)} spent", file=sys.stderr)
        return 1
    print(
        f"evals: live: {len(cases)} cases recorded on {today.isoformat()}; USD {sum(spent)} spent"
    )
    return 0
