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

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from scripted_model import EchoModel

from app.api import ask
from app.auth import dev_shim
from app.config import branding
from app.config.settings import LOOPBACK, Settings, env_name
from app.embeddings.fake import FakeEmbedder
from app.embeddings.model_files import ModelFileError
from app.harness import loop
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
    # Control: the same request with the app's own Origin gets past the shim.
    assert post(signed_in).status_code != 403


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
    headers = [("origin", BASE), ("content-type", "application/json")]
    headers.append(("content-type", "application/json"))
    response = signed_in.post(ask.ROUTE, content=b'{"question": "q"}', headers=headers)
    assert response.status_code == 422, response.status_code


def test_a_body_over_the_cap_is_422(signed_in: TestClient, model: EchoModel) -> None:
    padded = b'{"question": "q"' + b" " * ask.MAX_BODY + b"}"
    response = post(signed_in, padded)
    assert response.status_code == 422, response.status_code
    # Control: the same body without the padding gets past the check.
    assert post(signed_in, b'{"question": "q"}').status_code == 503


def test_every_valid_question_fits_the_cap() -> None:
    # The longest question in the most expensive JSON form a client may send (every
    # character a \\u escape), with the longest mandate id, stays under the cap.
    longest = '{"question": "' + "\\u0001" * 2_000 + '", "mandate_id": "' + "m" * 128 + '"}'
    assert len(longest.encode()) <= ask.MAX_BODY


def test_a_422_echoes_nothing(signed_in: TestClient) -> None:
    response = post(signed_in, {"question": CANARY * 300})
    assert response.status_code == 422
    assert CANARY not in response.text


def test_the_limits_match_the_loop() -> None:
    # The route refuses what the loop would refuse, before any model call.
    assert ask.MAX_QUESTION == loop.MAX_QUESTION == 2_000
    assert ask.MAX_MANDATE_ID == loop.MAX_MANDATE_ID


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
        assert post(test_client).status_code == 503
    assert len(built) == 1


def test_without_the_embedding_model_it_is_503(
    monkeypatch: pytest.MonkeyPatch, model: EchoModel
) -> None:
    loads: list[int] = []

    def missing() -> Any:
        loads.append(1)
        raise ModelFileError("onnx/model.onnx: missing")

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
        assert post(test_client).status_code == 503  # the closed pool
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
    clock.now += 59.9
    assert not limit.admit(MINE)
    clock.now += 0.1
    assert limit.admit(MINE), "the window did not slide"
    limit.release(MINE)


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
    clock.now += 9.9
    client.create(**request)
    clock.now += 0.1
    with pytest.raises(BudgetExceeded):
        client.create(**request)
    assert len(model.calls) == 2, "a call started after the deadline"
