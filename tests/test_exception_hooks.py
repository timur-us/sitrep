"""Exceptions outside a request log their type and the trace id, nothing else (M1 PR 5).

ADR 0007's Watch item for M1: Python's own hooks print the whole exception, message and
traceback, to stderr: an exception in a thread (threading.excepthook), one that cannot be
raised, in a __del__ or a finaliser (sys.unraisablehook), and one in an asyncio task that
nobody retrieved (the loop's exception handler). The loop's tool calls and model calls
(PR 10) can raise with client data in the message.

app/observability/logs.py replaces all three. Each logs a fixed message on the "app"
logger with the exception's type and the trace id as fields: no message, no traceback,
no exception object on the record. The entry point installs the two process hooks; the
app's lifespan installs the asyncio handler on the loop that serves it. After review
(M1 PR 5): the main thread's sys.excepthook and warnings.showwarning too, and a filter on
the "asyncio" logger for its default handler, which still runs on other loops.

Each test has a control: Python's default hook does print the canary, so the check can
fail.
"""

from __future__ import annotations

import asyncio
import gc
import logging
import sys
import threading
import types
import warnings
from collections.abc import Iterator

import pytest
import uvicorn
from fastapi.testclient import TestClient

from app import __main__ as entry
from app.config.settings import Settings
from app.main import create_app
from app.observability import logs
from app.observability.logs import LOG_CONFIG, TRACE_ID_ATTRIBUTE

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
    """Install the app's hooks for one test; tests/conftest.py puts the previous ones back."""
    logs.install_exception_hooks()
    yield


TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"


def _leaky() -> Leaky:
    """The canary exception, tagged with a trace id as the app's 500 handler tags one."""
    exc = Leaky(CANARY)
    setattr(exc, TRACE_ID_ATTRIBUTE, TRACE)
    return exc


def _raise_in_thread(exc: BaseException | None = None) -> None:
    def boom() -> None:
        raise exc if exc is not None else _leaky()

    worker = threading.Thread(target=boom)
    worker.start()
    worker.join()


class Finaliser:
    def __del__(self) -> None:
        raise _leaky()


def _raise_unraisable() -> None:
    # Reference counting runs __del__ at once; no gc.collect(), which would also finalise
    # whatever garbage earlier tests left (review finding test-6).
    finaliser = Finaliser()
    del finaliser


def _the_record(caplog: pytest.LogCaptureFixture, message: str) -> logging.LogRecord:
    leaky = f"{__name__}.Leaky"
    records = [r for r in caplog.records if r.getMessage() == message and r.exc_type == leaky]
    assert len(records) == 1, [(r.getMessage(), r.exc_type) for r in caplog.records]
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
    assert record.trace_id == TRACE


@pytest.mark.usefixtures("hooks")
def test_system_exit_in_a_thread_is_quiet(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    _raise_in_thread(SystemExit(CANARY))
    assert [r for r in caplog.records if r.getMessage() == logs.THREAD_MESSAGE] == []


@pytest.mark.usefixtures("hooks")
def test_an_untagged_exception_takes_the_current_trace(caplog: pytest.LogCaptureFixture) -> None:
    from opentelemetry.sdk.trace import TracerProvider

    caplog.set_level(logging.DEBUG)
    with TracerProvider().get_tracer("test").start_as_current_span("x") as span:
        unraisable = types.SimpleNamespace(
            exc_type=Leaky, exc_value=Leaky(CANARY), exc_traceback=None, err_msg=None, object=None
        )
        logs.unraisable_hook(unraisable)  # type: ignore[arg-type]
        expected = format(span.get_span_context().trace_id, "032x")
    record = _the_record(caplog, logs.UNRAISABLE_MESSAGE)
    assert record.trace_id == expected and CANARY not in _texts(caplog)


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
    assert record.trace_id == TRACE


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
        raise _leaky()

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
    assert record.trace_id == TRACE


def test_the_default_asyncio_handler_logs_the_canary(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    _orphan_task(None)
    assert CANARY in _texts(caplog)


def test_an_asyncio_context_without_an_exception(caplog: pytest.LogCaptureFixture) -> None:
    # asyncio reports some problems with a message and no exception; the message can
    # carry a repr, so it is not logged either.
    caplog.set_level(logging.DEBUG)
    loop = asyncio.new_event_loop()
    try:
        logs.asyncio_exception_handler(loop, {"message": f"unclosed {CANARY}", "transport": CANARY})
    finally:
        loop.close()
    records = [r for r in caplog.records if r.getMessage() == logs.ASYNCIO_MESSAGE]
    assert len(records) == 1 and records[0].exc_type is None
    assert CANARY not in _texts(caplog)


def test_the_entry_point_installs_the_process_hooks(
    monkeypatch: pytest.MonkeyPatch, tmp_path, restore_logging: None
) -> None:
    # Precondition (review finding test-1): no earlier test left the app's hooks behind;
    # tests/conftest.py restores them after every test.
    installed = (logs.thread_excepthook, logs.unraisable_hook, logs.main_excepthook)
    current = (threading.excepthook, sys.unraisablehook, sys.excepthook)
    assert not set(installed) & set(current), current
    assert warnings.showwarning is not logs.showwarning
    monkeypatch.setattr(uvicorn.Config, "bind_socket", lambda config: __import__("socket").socket())
    monkeypatch.setattr(uvicorn.Server, "run", lambda server, sockets=None: None)
    monkeypatch.chdir(tmp_path)
    assert entry.main() == 0
    assert (threading.excepthook, sys.unraisablehook, sys.excepthook) == installed
    assert warnings.showwarning is logs.showwarning


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


# --- after review: the main thread, warnings, and asyncio's own logger -------------------


@pytest.mark.usefixtures("hooks")
def test_an_exception_escaping_the_main_thread_logs_its_type_only(
    caplog: pytest.LogCaptureFixture, capfd: pytest.CaptureFixture[str]
) -> None:
    # Review finding sec-4: sys.excepthook prints what escapes main().
    caplog.set_level(logging.DEBUG)
    exc = _leaky()
    sys.excepthook(Leaky, exc, None)
    out, err = capfd.readouterr()
    assert CANARY not in out + err and CANARY not in _texts(caplog)
    assert _the_record(caplog, logs.MAIN_MESSAGE).trace_id == TRACE
    # Ctrl-C stays quiet.
    sys.excepthook(KeyboardInterrupt, KeyboardInterrupt(CANARY), None)
    assert CANARY not in "".join(capfd.readouterr())


def test_the_default_excepthook_prints_the_canary(capfd: pytest.CaptureFixture[str]) -> None:
    sys.__excepthook__(Leaky, Leaky(CANARY), None)
    assert CANARY in capfd.readouterr().err


def test_a_warning_logs_its_category_only(
    caplog: pytest.LogCaptureFixture, capfd: pytest.CaptureFixture[str]
) -> None:
    # Review finding sec-1: a warning prints its whole message to stderr, and pydantic's
    # serializer warnings quote the value. pytest records warnings per test, so the hook
    # is installed inside a catch_warnings of the test's own.
    caplog.set_level(logging.DEBUG)
    with warnings.catch_warnings():
        warnings.simplefilter("always")
        logs.install_exception_hooks()
        warnings.warn(f"value={CANARY}", UserWarning, stacklevel=1)
    out, err = capfd.readouterr()
    assert CANARY not in out + err and CANARY not in _texts(caplog)
    records = [r for r in caplog.records if r.getMessage() == logs.WARNING_MESSAGE]
    assert len(records) == 1 and records[0].exc_type == "UserWarning", records


def test_the_default_warning_display_carries_the_canary() -> None:
    # Control: pytest records warnings per test, so Python's display does not run here;
    # the warning it would print, and its formatted text, carry the canary.
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        warnings.warn(f"value={CANARY}", UserWarning, stacklevel=1)
    [shown] = caught
    assert CANARY in warnings.formatwarning(shown.message, shown.category, shown.filename, 1)


@pytest.mark.usefixtures("restore_logging")
def test_asyncios_own_handler_logs_no_text_under_the_app_config(
    capfd: pytest.CaptureFixture[str],
) -> None:
    # Review finding sec-3: on a loop without the app's handler (another loop, before
    # the lifespan, or when the handler itself raises), asyncio's default handler logs a
    # message built from reprs. Under the app's log config the "asyncio" logger keeps a
    # fixed message and the type.
    import logging.config

    logging.config.dictConfig(LOG_CONFIG)
    _orphan_task(None)
    loop = asyncio.new_event_loop()
    try:
        loop.call_exception_handler({"message": f"unclosed {CANARY}", "transport": CANARY})
    finally:
        loop.close()
    err = capfd.readouterr().err
    assert CANARY not in err, err
    assert logs.ASYNCIO_MESSAGE in err and f"exc_type={__name__}.Leaky" in err, err


@pytest.mark.usefixtures("restore_logging")
def test_a_raising_handler_falls_back_without_text(capfd: pytest.CaptureFixture[str]) -> None:
    import logging.config

    logging.config.dictConfig(LOG_CONFIG)

    def raising(loop: object, context: dict) -> None:
        raise RuntimeError(CANARY)

    _orphan_task(raising)
    err = capfd.readouterr().err
    assert CANARY not in err, err
