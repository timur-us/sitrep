"""Exceptions outside a request log their type and the trace id, nothing else (M1 PR 5).

ADR 0007's Watch item for M1: Python's own hooks print the whole exception, message and
traceback, to stderr: an exception in a thread (threading.excepthook), one that cannot be
raised, in a __del__ or a finaliser (sys.unraisablehook), and one in an asyncio task that
nobody retrieved (the loop's exception handler). The loop's tool calls and model calls
(PR 10) can raise with client data in the message.

app/observability/logs.py replaces all three. Each logs a fixed message on the "app"
logger with the exception's type and the trace id as fields: no message, no traceback,
no exception object on the record. The entry point installs the two process hooks; the
app's lifespan installs the asyncio handler on the loop that serves it.

Each test has a control: Python's default hook does print the canary, so the check can
fail.
"""

from __future__ import annotations

import asyncio
import gc
import logging
import sys
import threading
from collections.abc import Iterator

import pytest
import uvicorn
from fastapi.testclient import TestClient

from app import __main__ as entry
from app.config.settings import Settings
from app.main import create_app
from app.observability import logs

CANARY = "canary-exception-text-7f3a"


class Leaky(RuntimeError):
    pass


def _texts(caplog: pytest.LogCaptureFixture) -> str:
    """Everything a record carries: message, arguments, exception and formatted text."""
    return caplog.text + "".join(
        f"{r.getMessage()}{r.args!r}{r.exc_info!r}{r.exc_text!r}{vars(r)!r}" for r in caplog.records
    )


@pytest.fixture
def hooks() -> Iterator[None]:
    """Install the app's hooks for one test, and put Python's back afterwards."""
    saved = (threading.excepthook, sys.unraisablehook)
    logs.install_exception_hooks()
    yield
    threading.excepthook, sys.unraisablehook = saved


def _raise_in_thread() -> None:
    def boom() -> None:
        raise Leaky(CANARY)

    worker = threading.Thread(target=boom)
    worker.start()
    worker.join()


class Finaliser:
    def __del__(self) -> None:
        raise Leaky(CANARY)


def _raise_unraisable() -> None:
    finaliser = Finaliser()
    del finaliser
    gc.collect()


def _the_record(caplog: pytest.LogCaptureFixture, message: str) -> logging.LogRecord:
    records = [r for r in caplog.records if r.getMessage() == message]
    assert len(records) == 1, [r.getMessage() for r in caplog.records]
    return records[0]


@pytest.mark.usefixtures("hooks")
def test_a_thread_exception_logs_its_type_only(
    caplog: pytest.LogCaptureFixture, capfd: pytest.CaptureFixture[str]
) -> None:
    caplog.set_level(logging.DEBUG)
    _raise_in_thread()
    out, err = capfd.readouterr()
    assert CANARY not in out + err
    assert CANARY not in _texts(caplog)
    record = _the_record(caplog, logs.THREAD_MESSAGE)
    assert (record.name, record.levelname) == ("app", "ERROR")
    assert record.exc_type == f"{__name__}.Leaky" and record.exc_info is None
    assert record.trace_id == logs.NO_TRACE


def test_the_default_thread_hook_prints_the_canary(capfd: pytest.CaptureFixture[str]) -> None:
    saved = threading.excepthook
    threading.excepthook = threading.__excepthook__
    try:
        _raise_in_thread()
    finally:
        threading.excepthook = saved
    assert CANARY in capfd.readouterr().err


@pytest.mark.usefixtures("hooks")
def test_an_unraisable_exception_logs_its_type_only(
    caplog: pytest.LogCaptureFixture, capfd: pytest.CaptureFixture[str]
) -> None:
    caplog.set_level(logging.DEBUG)
    _raise_unraisable()
    out, err = capfd.readouterr()
    assert CANARY not in out + err
    assert CANARY not in _texts(caplog)
    record = _the_record(caplog, logs.UNRAISABLE_MESSAGE)
    assert record.exc_type == f"{__name__}.Leaky" and record.exc_info is None


def test_the_default_unraisable_hook_prints_the_canary(capfd: pytest.CaptureFixture[str]) -> None:
    saved = sys.unraisablehook
    sys.unraisablehook = sys.__unraisablehook__
    try:
        _raise_unraisable()
    finally:
        sys.unraisablehook = saved
    assert CANARY in capfd.readouterr().err


def _orphan_task(handler: object | None) -> None:
    """A task whose exception nobody retrieves; its loop reports it when it is collected."""
    loop = asyncio.new_event_loop()
    if handler is not None:
        loop.set_exception_handler(handler)  # type: ignore[arg-type]

    async def boom() -> None:
        raise Leaky(CANARY)

    async def main() -> None:
        loop.create_task(boom())
        await asyncio.sleep(0.01)

    try:
        loop.run_until_complete(main())
    finally:
        loop.close()
    gc.collect()


def test_an_asyncio_task_exception_logs_its_type_only(
    caplog: pytest.LogCaptureFixture, capfd: pytest.CaptureFixture[str]
) -> None:
    caplog.set_level(logging.DEBUG)
    _orphan_task(logs.asyncio_exception_handler)
    out, err = capfd.readouterr()
    assert CANARY not in out + err
    assert CANARY not in _texts(caplog)
    record = _the_record(caplog, logs.ASYNCIO_MESSAGE)
    assert record.exc_type == f"{__name__}.Leaky" and record.exc_info is None


def test_the_default_asyncio_handler_logs_the_canary(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    _orphan_task(None)
    assert CANARY in _texts(caplog)


def test_an_asyncio_context_without_an_exception(caplog: pytest.LogCaptureFixture) -> None:
    # asyncio reports some problems with a message and no exception; the message can
    # carry a repr, so it is not logged either.
    caplog.set_level(logging.DEBUG)
    logs.asyncio_exception_handler(
        asyncio.new_event_loop(), {"message": f"unclosed {CANARY}", "transport": CANARY}
    )
    record = _the_record(caplog, logs.ASYNCIO_MESSAGE)
    assert record.exc_type is None and CANARY not in _texts(caplog)


def test_the_entry_point_installs_the_process_hooks(
    monkeypatch: pytest.MonkeyPatch, tmp_path, restore_logging: None
) -> None:
    saved = (threading.excepthook, sys.unraisablehook)
    monkeypatch.setattr(uvicorn.Config, "bind_socket", lambda config: __import__("socket").socket())
    monkeypatch.setattr(uvicorn.Server, "run", lambda server, sockets=None: None)
    monkeypatch.chdir(tmp_path)
    try:
        assert entry.main() == 0
        assert threading.excepthook is logs.thread_excepthook
        assert sys.unraisablehook is logs.unraisable_hook
    finally:
        threading.excepthook, sys.unraisablehook = saved


def test_the_lifespan_installs_the_asyncio_handler(settings: Settings) -> None:
    app = create_app(settings)
    seen: list[object] = []

    @app.get("/probe-loop-handler")
    async def probe() -> dict[str, bool]:
        seen.append(asyncio.get_running_loop().get_exception_handler())
        return {"ok": True}

    base = f"http://127.0.0.1:{settings.port}"
    with TestClient(app, base_url=base) as client:
        assert client.get("/probe-loop-handler").status_code == 200
    assert seen == [logs.asyncio_exception_handler]
