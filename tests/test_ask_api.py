"""POST /api/ask without a database (M1 plan, PR 12; the API contract).

In process, through TestClient, with the dev login's middleware on a 127.0.0.1 peer and
without the lifespan, so the pool stays closed and the route stops at the database
(503) whenever a request gets that far. The model client is a scripted fake
(tests/scripted_model.py): no socket, no key. The read path itself, with the database,
is in tests/db/test_ask.py; in Chromium, in tests/web/test_ask.py.

- The login comes first: 401 without one, with the shim on or off. Then the shim's
  Origin check: 403 for a missing, null or foreign Origin. Neither reaches the model.
- One fixed 422 for every request that is not an ask: another content type, a body
  over MAX_BODY, no JSON, another shape, a question empty, blank or over 2,000
  characters, a mandate id of another form. The body echoes nothing it was sent.
- 503 without a key (the model client is built at the first ask, once), without the
  embedding model, and without a database; /health is unaffected.
- Every answer is JSON with Cache-Control: no-store.
- The rate limit and the deadline, on their own, with a clock the test moves.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient
from scripted_model import EchoModel

from app.api import ask
from app.auth import dev_shim
from app.config import branding
from app.config.settings import LOOPBACK, Settings, env_name
from app.embeddings import onnx
from app.embeddings.fake import FakeEmbedder
from app.harness.context import BudgetExceeded
from app.harness.model_client import ModelClientRefused
from app.main import create_app

PORT = 8000
BASE = f"http://{LOOPBACK}:{PORT}"
ORIGIN = {"Origin": BASE}
JSON = {**ORIGIN, "Content-Type": "application/json"}
PEER = (LOOPBACK, 50000)
MINE = branding.DEMO_MANDATE.advisor_id
OTHER = next(person.id for person in branding.ADVISORS if person.id != MINE)
CANARY = "cnry-ask-7d3f"
QUESTION = {
    "question": f"What do I need to know before the call with {branding.DEMO_MANDATE.family}?"
}


class Clock:
    """A monotonic clock the test moves."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


def dev_settings(monkeypatch: pytest.MonkeyPatch, *, dev_auth: str = "1") -> Settings:
    monkeypatch.setenv(env_name("dev_auth"), dev_auth)
    monkeypatch.setenv(env_name("port"), str(PORT))
    return Settings(_env_file=None)


def client(settings: Settings, **kwargs: Any) -> TestClient:
    # No lifespan: the pool stays closed, so a request that gets past the checks stops
    # at the database with 503.
    app = create_app(settings, bind_host=LOOPBACK, **kwargs)
    return TestClient(app, base_url=BASE, client=PEER, raise_server_exceptions=False)


def login(test_client: TestClient, advisor_id: str) -> None:
    """Signs test_client in as advisor_id, through the shim's form."""
    test_client.cookies.clear()
    response = test_client.post(
        "/dev/login",
        content=f"advisor={advisor_id}",
        headers={**ORIGIN, "Content-Type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 200, f"login as {advisor_id}: {response.status_code}"
    assert test_client.cookies.get(dev_shim.COOKIE), "no session cookie"


def post(test_client: TestClient, body: object = QUESTION, **kwargs: Any) -> Any:
    headers = kwargs.pop("headers", JSON)
    if isinstance(body, (bytes, str)):
        return test_client.post(ask.ROUTE, content=body, headers=headers, **kwargs)
    return test_client.post(ask.ROUTE, json=body, headers=headers, **kwargs)


def assert_json_no_store(response: Any) -> None:
    assert response.headers.get("content-type") == "application/json", response.headers
    assert response.headers.get("cache-control") == "no-store", response.headers


@pytest.fixture
def model() -> EchoModel:
    return EchoModel(branding.DEMO_MANDATE.family)


@pytest.fixture
def signed_in(monkeypatch: pytest.MonkeyPatch, model: EchoModel) -> Iterator[TestClient]:
    """The demo advisor, signed in through the shim; a scripted model, a fake embedder."""
    test_client = client(dev_settings(monkeypatch), model_client=model, embedder=FakeEmbedder())
    login(test_client, MINE)
    yield test_client


# The login, then the Origin.


def test_without_a_login_it_is_401(monkeypatch: pytest.MonkeyPatch, model: EchoModel) -> None:
    test_client = client(dev_settings(monkeypatch), model_client=model, embedder=FakeEmbedder())
    response = post(test_client)
    assert response.status_code == 401, response.status_code
    assert_json_no_store(response)
    assert model.calls == []


def test_without_the_shim_it_is_401(monkeypatch: pytest.MonkeyPatch, model: EchoModel) -> None:
    test_client = client(
        dev_settings(monkeypatch, dev_auth="0"), model_client=model, embedder=FakeEmbedder()
    )
    response = post(test_client)
    assert response.status_code == 401, response.status_code
    assert model.calls == []


@pytest.mark.parametrize(
    "origin",
    [
        None,
        "null",
        "http://localhost:8000",
        "http://127.0.0.1:8001",
        f"https://{branding.DOMAINS.attacker}",
    ],
    ids=repr,
)
def test_without_the_origin_it_is_403(
    signed_in: TestClient, model: EchoModel, origin: str | None
) -> None:
    headers = {"Content-Type": "application/json"}
    if origin is not None:
        headers["Origin"] = origin
    response = post(signed_in, headers=headers)
    assert response.status_code == 403, response.status_code
    assert model.calls == []
    # Control: the same request with the app's own Origin gets past the shim, to the
    # closed pool (review finding test-4: a 404 would not do).
    control = post(signed_in)
    assert (control.status_code, control.json()) == (503, {"detail": ask.NO_DATABASE})


def test_get_is_not_an_ask(signed_in: TestClient, model: EchoModel) -> None:
    response = signed_in.get(ask.ROUTE)
    assert response.status_code == 405, response.status_code
    assert_json_no_store(response)
    assert signed_in.post(ask.ROUTE + "/", json=QUESTION, headers=JSON).status_code == 404
    assert model.calls == []


# One fixed 422.

INVALID_BODIES = {
    "not json": b"{question: 1}",
    "a list": b'[{"question": "q"}]',
    "a string": b'"question"',
    "no question": b"{}",
    "an extra key": b'{"question": "q", "advisor_id": "' + OTHER.encode() + b'"}',
    "a number": b'{"question": 7}',
    "empty": b'{"question": ""}',
    "blank": b'{"question": " \\n\\t "}',
    "too long": ('{"question": "' + "x" * 2_001 + '"}').encode(),
    "a mandate id with a space": b'{"question": "q", "mandate_id": "a b"}',
    "a mandate id with a slash": b'{"question": "q", "mandate_id": "../x"}',
    # Python's `$` would match before this newline; pydantic's engine must not.
    "a mandate id with a trailing newline": b'{"question": "q", "mandate_id": "m1\\n"}',
    "an empty mandate id": b'{"question": "q", "mandate_id": ""}',
    "a long mandate id": ('{"question": "q", "mandate_id": "' + "m" * 129 + '"}').encode(),
    "a mandate id that is a number": b'{"question": "q", "mandate_id": 5}',
}


@pytest.mark.parametrize("body", INVALID_BODIES.values(), ids=INVALID_BODIES.keys())
def test_a_body_that_is_not_an_ask_is_422(
    signed_in: TestClient, model: EchoModel, body: bytes
) -> None:
    response = post(signed_in, body)
    assert response.status_code == 422, response.status_code
    assert response.json() == {"detail": ask.INVALID}
    assert_json_no_store(response)
    assert model.calls == []


@pytest.mark.parametrize(
    "content_type",
    [
        None,
        "text/plain",
        "application/x-www-form-urlencoded",
        "multipart/form-data",
        "application/json; charset=utf-8",
        "Application/JSON",
        "application/jsonx",
    ],
    ids=repr,
)
def test_another_content_type_is_422(
    signed_in: TestClient, model: EchoModel, content_type: str | None
) -> None:
    headers = dict(ORIGIN)
    if content_type is not None:
        headers["Content-Type"] = content_type
    response = post(signed_in, b'{"question": "q"}', headers=headers)
    assert response.status_code == 422, response.status_code
    assert model.calls == []


def test_two_content_types_are_422(signed_in: TestClient, model: EchoModel) -> None:
    one = [("origin", BASE), ("content-type", "application/json")]
    two = [*one, ("content-type", "application/json")]
    response = signed_in.post(ask.ROUTE, content=b'{"question": "q"}', headers=two)
    assert response.status_code == 422, response.status_code
    assert model.calls == []
    # Control (review finding test-11): one header gets past the check.
    control = signed_in.post(ask.ROUTE, content=b'{"question": "q"}', headers=one)
    assert control.status_code == 503, control.status_code


def padded(size: int) -> bytes:
    """A valid ask of exactly size bytes: whitespace after the object is JSON too."""
    body = b'{"question": "q"}'
    return body + b" " * (size - len(body))


def test_the_body_cap_is_exact(signed_in: TestClient, model: EchoModel) -> None:
    # Review finding test-6: exactly MAX_BODY bytes get past the check, one more do not.
    at_cap = post(signed_in, padded(ask.MAX_BODY))
    assert (at_cap.status_code, at_cap.json()) == (503, {"detail": ask.NO_DATABASE})
    over = post(signed_in, padded(ask.MAX_BODY + 1))
    assert (over.status_code, over.json()) == (422, {"detail": ask.INVALID})
    assert model.calls == []


def test_a_chunked_body_is_capped_without_a_length(signed_in: TestClient) -> None:
    # Review finding test-6: the cap counts what arrives, not Content-Length.
    def chunks() -> Iterator[bytes]:
        yield b'{"question": "'
        for _ in range(64):
            yield b"x" * 16_384
        yield b'"}'

    response = signed_in.post(ask.ROUTE, content=chunks(), headers=JSON)
    assert "content-length" not in response.request.headers
    assert response.request.headers.get("transfer-encoding") == "chunked"
    assert (response.status_code, response.json()) == (422, {"detail": ask.INVALID})


def escaped(text: str) -> str:
    """text as a JSON string with every character escaped, surrogate pairs included."""
    units = text.encode("utf-16-be")
    return (
        '"' + "".join(f"\\u{units[i]:02x}{units[i + 1]:02x}" for i in range(0, len(units), 2)) + '"'
    )


LONGEST_MANDATE = "a" + "b-_" * 42 + "c"  # 128 characters, every allowed kind


@pytest.mark.parametrize(
    "body",
    [
        # Review finding sec-8: 2,000 characters outside the BMP, each escaped as a
        # surrogate pair (12 bytes, Python's json.dumps default), and the longest
        # mandate id escaped too: the most a valid ask can take.
        '{"question": '
        + escaped(chr(0x1F600) * 2_000)
        + ', "mandate_id": '
        + escaped(LONGEST_MANDATE)
        + "}",
        '{"question": "' + "x" * 2_000 + '", "mandate_id": "' + LONGEST_MANDATE + '"}',
    ],
    ids=["escaped", "plain"],
)
def test_the_largest_valid_asks_get_past_the_body_check(
    signed_in: TestClient, model: EchoModel, body: str
) -> None:
    # Review finding test-5: posted, not computed. 503 is the closed pool, past the body.
    response = post(signed_in, body.encode("ascii"))
    assert (response.status_code, response.json()) == (503, {"detail": ask.NO_DATABASE})
    assert len(LONGEST_MANDATE) == ask.MAX_MANDATE_ID


def test_a_422_echoes_nothing(signed_in: TestClient) -> None:
    response = post(signed_in, {"question": CANARY * 300})
    assert response.status_code == 422
    assert CANARY not in response.text


def test_a_refused_body_carries_no_context() -> None:
    # Review finding sec-7: pydantic's error quotes the input; the 422 is raised outside
    # the except, so it is neither the cause nor the context (as in the loop).
    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": f'{{"question": {CANARY!r}}}'.encode()}

    scope = {
        "type": "http",
        "method": "POST",
        "path": ask.ROUTE,
        "headers": [(b"content-type", b"application/json")],
        "query_string": b"",
    }
    with pytest.raises(HTTPException) as refused:
        asyncio.run(ask.ask_body(Request(scope, receive)))
    assert refused.value.status_code == 422
    assert refused.value.__cause__ is None and refused.value.__context__ is None


# The order: login, the content type, the limit, the body, the model, the embedder,
# the database (review finding test-7).


def test_a_bad_body_is_422_before_a_missing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    built: list[int] = []
    monkeypatch.setattr(ask, "build_model_client", lambda settings: built.append(1))
    test_client = client(dev_settings(monkeypatch), embedder=FakeEmbedder())
    login(test_client, MINE)
    assert post(test_client, b"{}").status_code == 422
    assert built == [], "the model client was built for a malformed ask"


def test_a_missing_key_is_named_before_a_missing_embedder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loads: list[int] = []
    monkeypatch.setattr(ask, "build_embedder", lambda: loads.append(1))
    test_client = client(dev_settings(monkeypatch))
    login(test_client, MINE)
    response = post(test_client)
    assert (response.status_code, response.json()) == (503, {"detail": ask.NO_MODEL})
    assert loads == [], "the embedder was loaded without a model client"


def test_over_the_limit_a_bad_body_is_429(
    monkeypatch: pytest.MonkeyPatch, model: EchoModel
) -> None:
    test_client = client(dev_settings(monkeypatch), model_client=model, embedder=FakeEmbedder())
    login(test_client, MINE)
    for _ in range(ask.REQUESTS_PER_WINDOW):
        post(test_client)
    assert post(test_client, b"{}").status_code == 429


def test_another_content_type_is_not_counted(
    monkeypatch: pytest.MonkeyPatch, model: EchoModel
) -> None:
    # Review finding sec-2: a cross-site form (text/plain, no preflight) that carried
    # the session cookie past an Origin check could otherwise hold an advisor at 429.
    # The content type is refused before the limit counts.
    test_client = client(dev_settings(monkeypatch), model_client=model, embedder=FakeEmbedder())
    login(test_client, MINE)
    form = {**ORIGIN, "Content-Type": "text/plain"}
    for _ in range(ask.REQUESTS_PER_WINDOW + 2):
        assert post(test_client, b'{"question": "q"}', headers=form).status_code == 422
    response = post(test_client)
    assert (response.status_code, response.json()) == (503, {"detail": ask.NO_DATABASE})


# 503 and /health.


def test_without_a_key_it_is_503(monkeypatch: pytest.MonkeyPatch) -> None:
    built: list[int] = []
    real = ask.build_model_client

    def counting(settings: Settings) -> Any:
        built.append(1)
        return real(settings)

    monkeypatch.setattr(ask, "build_model_client", counting)
    test_client = client(dev_settings(monkeypatch), embedder=FakeEmbedder())
    assert built == [], "the model client was built before an ask"
    login(test_client, MINE)
    for _ in range(2):
        response = post(test_client)
        assert response.status_code == 503, response.status_code
        assert response.json() == {"detail": ask.NO_MODEL}
        assert_json_no_store(response)
    assert built == [1], "the failed build was not kept"
    health = test_client.get("/health")
    assert (health.status_code, health.json()) == (200, {"status": "ok"})


def test_the_refusal_names_no_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    def refused(settings: Settings) -> Any:
        raise ModelClientRefused("ANTHROPIC_API_KEY is not set")

    monkeypatch.setattr(ask, "build_model_client", refused)
    test_client = client(dev_settings(monkeypatch), embedder=FakeEmbedder())
    login(test_client, MINE)
    response = post(test_client)
    assert response.status_code == 503 and "ANTHROPIC" not in response.text


def test_the_model_client_is_built_once(monkeypatch: pytest.MonkeyPatch) -> None:
    built: list[EchoModel] = []

    def build(settings: Settings) -> EchoModel:
        built.append(EchoModel(branding.DEMO_MANDATE.family))
        return built[-1]

    monkeypatch.setattr(ask, "build_model_client", build)
    test_client = client(dev_settings(monkeypatch), embedder=FakeEmbedder())
    login(test_client, MINE)
    assert built == []
    for _ in range(3):
        # The pool is closed here, so each ask stops at the database, past the client.
        response = post(test_client)
        assert (response.status_code, response.json()) == (503, {"detail": ask.NO_DATABASE})
    assert len(built) == 1


def test_without_the_embedding_model_it_is_503(
    monkeypatch: pytest.MonkeyPatch, model: EchoModel
) -> None:
    loads: list[int] = []

    def missing() -> Any:
        loads.append(1)
        raise ask.EmbedderUnavailable

    monkeypatch.setattr(ask, "build_embedder", missing)
    test_client = client(dev_settings(monkeypatch), model_client=model)
    login(test_client, MINE)
    for _ in range(2):
        response = post(test_client)
        assert response.status_code == 503, response.status_code
        assert response.json() == {"detail": ask.NO_SEARCH}
        assert "onnx" not in response.text
    assert loads == [1], "the failed load was not kept"
    assert model.calls == []


def test_a_transient_failure_to_load_is_not_kept(
    monkeypatch: pytest.MonkeyPatch, model: EchoModel
) -> None:
    # Review finding sec-4: only a model that is missing or not the pinned bytes is kept
    # as "search unavailable"; another failure (here an I/O error) is tried again.
    loads: list[int] = []

    def flaky() -> Any:
        loads.append(1)
        raise OSError("EIO")

    monkeypatch.setattr(ask, "build_embedder", flaky)
    test_client = client(dev_settings(monkeypatch), model_client=model)
    login(test_client, MINE)
    for _ in range(2):
        response = post(test_client)
        assert (response.status_code, response.json()) == (503, {"detail": ask.NO_SEARCH})
        assert "EIO" not in response.text
    assert loads == [1, 1], "a transient failure was kept until a restart"


def test_missing_model_files_make_the_embedder_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The real build, on an empty model directory: ModelFileError becomes the one
    # failure that is kept, raised without its message as context.
    monkeypatch.setattr(onnx, "model_dir", lambda: tmp_path)
    with pytest.raises(ask.EmbedderUnavailable) as unavailable:
        ask.build_embedder()
    assert unavailable.value.__context__ is None and unavailable.value.__cause__ is None


def test_the_embedder_is_loaded_at_the_first_ask(
    monkeypatch: pytest.MonkeyPatch, model: EchoModel
) -> None:
    loads: list[FakeEmbedder] = []

    def load() -> FakeEmbedder:
        loads.append(FakeEmbedder())
        return loads[-1]

    monkeypatch.setattr(ask, "build_embedder", load)
    test_client = client(dev_settings(monkeypatch), model_client=model)
    assert loads == []
    login(test_client, MINE)
    for _ in range(2):
        response = post(test_client)  # the closed pool, past the embedder
        assert (response.status_code, response.json()) == (503, {"detail": ask.NO_DATABASE})
    assert len(loads) == 1


def test_without_a_database_it_is_503(signed_in: TestClient, model: EchoModel) -> None:
    response = post(signed_in)
    assert response.status_code == 503, response.status_code
    assert response.json() == {"detail": ask.NO_DATABASE}
    assert_json_no_store(response)
    assert model.calls == [], "the model was called with the database down"
    assert signed_in.get("/health").status_code == 200


# The rate limit, on its own.


def test_the_rate_limit_counts_per_advisor_in_a_sliding_window() -> None:
    clock = Clock()
    limit = ask.RateLimit(per_window=3, window_seconds=60.0, clock=clock)
    for _ in range(3):
        assert limit.admit(MINE)
        limit.release(MINE)
    assert not limit.admit(MINE), "a fourth request in the window"
    assert limit.admit(OTHER), "one advisor's requests limit another"
    limit.release(OTHER)
    clock.now += 59.5
    assert not limit.admit(MINE)
    clock.now += 0.5
    assert limit.admit(MINE), "the window did not slide"
    limit.release(MINE)


def test_the_window_slides_from_each_start() -> None:
    # Starts at 0, 30 and 59 s with room for three: each frees its slot 60 s after it
    # began, not all at once (a fixed window would free all three at 60 s).
    clock = Clock()
    limit = ask.RateLimit(per_window=3, window_seconds=60.0, clock=clock)
    for at in (0.0, 30.0, 59.0):
        clock.now = 1_000.0 + at
        assert limit.admit(MINE)
        limit.release(MINE)
    clock.now = 1_060.0
    assert limit.admit(MINE), "the slot of the start at 0 s was not freed"
    limit.release(MINE)
    assert not limit.admit(MINE), "a second slot was freed at 60 s"
    clock.now = 1_090.0
    assert limit.admit(MINE), "the slot of the start at 30 s was not freed"
    limit.release(MINE)
    assert not limit.admit(MINE)


def test_the_rate_limit_allows_one_request_in_flight() -> None:
    limit = ask.RateLimit(per_window=10, window_seconds=60.0, clock=Clock())
    assert limit.admit(MINE)
    assert not limit.admit(MINE), "a second request in flight"
    assert limit.admit(OTHER)
    limit.release(MINE)
    assert limit.admit(MINE), "release did not free the slot"


def test_a_refused_request_is_not_counted() -> None:
    clock = Clock()
    limit = ask.RateLimit(per_window=2, window_seconds=60.0, clock=clock)
    assert limit.admit(MINE)
    for _ in range(5):
        assert not limit.admit(MINE)  # in flight: refused, not counted
    limit.release(MINE)
    assert limit.admit(MINE), "the refusals were counted"


def test_the_defaults() -> None:
    assert (ask.REQUESTS_PER_WINDOW, ask.WINDOW_SECONDS) == (10, 60.0)
    assert ask.DEADLINE_SECONDS == 90.0


def test_over_the_limit_it_is_429(monkeypatch: pytest.MonkeyPatch, model: EchoModel) -> None:
    # Without a database every admitted ask is 503, and each one counts.
    clock = Clock()
    test_client = client(
        dev_settings(monkeypatch), model_client=model, embedder=FakeEmbedder(), clock=clock
    )
    login(test_client, MINE)
    statuses = [post(test_client).status_code for _ in range(ask.REQUESTS_PER_WINDOW)]
    assert statuses == [503] * ask.REQUESTS_PER_WINDOW
    response = post(test_client)
    assert response.status_code == 429, response.status_code
    assert response.json() == {"detail": ask.TOO_MANY}
    assert_json_no_store(response)
    login(test_client, OTHER)
    assert post(test_client).status_code == 503, "one advisor's requests limit another"
    login(test_client, MINE)
    clock.now += ask.WINDOW_SECONDS
    assert post(test_client).status_code == 503, "the window did not slide"


def test_a_malformed_ask_counts_too(monkeypatch: pytest.MonkeyPatch, model: EchoModel) -> None:
    test_client = client(dev_settings(monkeypatch), model_client=model, embedder=FakeEmbedder())
    login(test_client, MINE)
    for _ in range(ask.REQUESTS_PER_WINDOW):
        assert post(test_client, b"{}").status_code == 422
    assert post(test_client).status_code == 429


def test_the_login_comes_before_the_limit(
    monkeypatch: pytest.MonkeyPatch, model: EchoModel
) -> None:
    test_client = client(dev_settings(monkeypatch), model_client=model, embedder=FakeEmbedder())
    for _ in range(ask.REQUESTS_PER_WINDOW + 2):
        assert post(test_client).status_code == 401


# The deadline, on its own.


def test_the_deadline_refuses_a_call_that_would_start_after_it() -> None:
    clock = Clock()
    model = EchoModel(branding.DEMO_MANDATE.family)
    client = ask.DeadlineClient(model, ends_at=clock.now + 10.0, clock=clock)
    assert client.model == model.model
    request = {
        "system": "s",
        "messages": [{"role": "user", "content": "q"}],
        "tools": [],
        "max_tokens": 16,
    }
    client.create(**request)
    clock.now += 9.5
    client.create(**request)
    clock.now += 0.5
    with pytest.raises(BudgetExceeded):
        client.create(**request)
    assert len(model.calls) == 2, "a call started after the deadline"
