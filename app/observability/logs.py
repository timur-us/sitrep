"""Log records without plaintext (CLAUDE.md rule 10, ADR 0007).

RedactFilter rewrites a record in place: the message template stays, its arguments go;
an exception becomes its type name, without message or traceback; the trace id is added.
It sits on uvicorn's loggers, so the record is clean for every handler, and on the
handler, as the backstop for records from other loggers. SafeFormatter prints only those
fields. StderrHandler writes them and never prints a traceback of its own.

The message template is trusted to be a constant. A library that logs an f-string with
request content would get through; uvicorn's f-strings carry process ids only.
"""

from __future__ import annotations

import errno
import logging
import sys
import threading
import warnings

from app.observability.tracing import current_trace_id

# Set on an exception by the app's 500 handler, while the request's span is current;
# uvicorn logs the exception later, after the span has ended.
TRACE_ID_ATTRIBUTE = "_trace_id"
NO_TRACE = "-"

# Loggers whose records are redacted at the source. A filter on a logger does not apply
# to its children's records, so each name is listed.
REDACTED_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access", "uvicorn.asgi", "app")


def exception_type(cls: type) -> str:
    """The type as OpenTelemetry writes exception.type, so a log line and its span match."""
    module = cls.__module__
    return cls.__qualname__ if module == "builtins" else f"{module}.{cls.__qualname__}"


def tag_trace_id(exc: BaseException) -> None:
    trace_id = current_trace_id()
    if trace_id is None:
        return
    # An exception may refuse new attributes (a frozen dataclass exception); the 500
    # still goes out with its headers, and the log line falls back to "-".
    try:
        setattr(exc, TRACE_ID_ATTRIBUTE, trace_id)
    except (AttributeError, TypeError):
        return


class RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        exc = record.exc_info[1] if record.exc_info else None
        if not isinstance(record.msg, str):
            # logger.error(exc) logs an object; its str() is the exception message.
            exc = exc or (record.msg if isinstance(record.msg, BaseException) else None)
            record.msg = exception_type(type(record.msg))
        if exc is not None:
            record.exc_type = exception_type(type(exc))
            # The symbolic errno (EADDRINUSE) says why a bind failed; it is a code, not text.
            code = getattr(exc, "errno", None) if isinstance(exc, OSError) else None
            record.exc_errno = errno.errorcode.get(code) if isinstance(code, int) else None
            record.trace_id = getattr(exc, TRACE_ID_ATTRIBUTE, None) or current_trace_id()
        record.args = ()
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        if getattr(record, "trace_id", None) is None:
            record.trace_id = current_trace_id() or NO_TRACE
        for name in ("exc_type", "exc_errno"):
            if not hasattr(record, name):
                setattr(record, name, None)
        return True


class AsyncioFilter(logging.Filter):
    """On the "asyncio" logger: its default exception handler builds the message from
    the context's reprs (the task, its coroutine, the exception's text). It runs on any
    loop without the app's handler, and when that handler raises. The record keeps a
    fixed message and the exception's type (RedactFilter then drops the rest)."""

    def filter(self, record: logging.LogRecord) -> bool:
        # A report without an exception (a slow callback, an unclosed transport) keeps a
        # message of its own and its level (review finding sec-r4).
        record.msg = ASYNCIO_MESSAGE if record.exc_info else ASYNCIO_REPORT
        record.args = ()
        return True


class SafeFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        RedactFilter().filter(record)
        parts = [self.formatTime(record), record.levelname, record.name, record.msg]
        if record.exc_type:
            parts.append(f"exc_type={record.exc_type}")
        if record.exc_errno:
            parts.append(f"errno={record.exc_errno}")
        parts.append(f"trace_id={record.trace_id}")
        return " ".join(parts)


class StderrHandler(logging.StreamHandler):
    """Writes to the sys.stderr of the moment, not the one at configuration time.

    A write that fails is dropped with a fixed line. logging's default handleError
    prints a traceback, and its context is the exception being logged, message included.
    """

    def __init__(self, level: int = logging.NOTSET) -> None:
        logging.Handler.__init__(self, level)

    @property
    def stream(self):
        return sys.stderr

    def handleError(self, record: logging.LogRecord) -> None:
        try:
            sys.stderr.write("logging error: a log line was dropped\n")
        except (OSError, ValueError):
            return


LOG_CONFIG: dict = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "redact": {"()": "app.observability.logs.RedactFilter"},
        "asyncio": {"()": "app.observability.logs.AsyncioFilter"},
    },
    "formatters": {"safe": {"()": "app.observability.logs.SafeFormatter"}},
    "handlers": {
        "stderr": {
            "class": "app.observability.logs.StderrHandler",
            "formatter": "safe",
            "filters": ["redact"],
        }
    },
    "loggers": {
        **{name: {"level": "INFO", "filters": ["redact"]} for name in REDACTED_LOGGERS},
        "asyncio": {"level": "INFO", "filters": ["asyncio", "redact"]},
    },
    "root": {"level": "INFO", "handlers": ["stderr"]},
}


# Python's own hooks print an exception whole, message and traceback, to stderr: one in a
# thread, one that cannot be raised (a __del__, a finaliser), one in an asyncio task that
# nobody retrieved. These replace them (ADR 0007's Watch item for M1, M1 PR 5): a fixed
# message on the "app" logger, which RedactFilter guards, with the exception's type and
# the trace id as fields. No message, no traceback, no exception object on the record,
# and nothing of the thread, the object or the task (their reprs can carry data).
HOOK_LOGGER = "app"
MAIN_MESSAGE = "an unhandled exception in the main thread"
THREAD_MESSAGE = "an unhandled exception in a thread"
UNRAISABLE_MESSAGE = "an exception that could not be raised"
ASYNCIO_MESSAGE = "an unhandled exception in an asyncio task"
ASYNCIO_REPORT = "an asyncio report"
WARNING_MESSAGE = "a warning"
# Written straight to stderr when a hook cannot log: never the exception itself.
HOOK_FAILED = "logging error: an unhandled exception could not be logged\n"


def _log(message: str, extra: dict, level: int) -> None:
    logging.getLogger(HOOK_LOGGER).log(level, message, extra=extra)


def _log_unhandled(
    message: str,
    exc: BaseException | None,
    kind: type | None = None,
    level: int = logging.ERROR,
) -> None:
    # A hook that raises makes Python print the original exception after its own, so
    # nothing escapes here (review finding sec-r2 of M1 PR 5).
    try:
        trace_id = getattr(exc, TRACE_ID_ATTRIBUTE, None) if exc is not None else None
        cls = kind if kind is not None else (type(exc) if exc is not None else None)
        extra = {
            "exc_type": exception_type(cls) if cls is not None else None,
            "trace_id": trace_id if isinstance(trace_id, str) else current_trace_id() or NO_TRACE,
        }
        _log(message, extra, level)
    except BaseException:  # noqa: BLE001 - the exception being reported must not escape
        try:
            sys.stderr.write(HOOK_FAILED)
        except BaseException:  # noqa: BLE001, S110 - nowhere left to report to
            pass


def main_excepthook(cls: type[BaseException], exc: BaseException, tb: object) -> None:
    """For sys.excepthook: what escapes the main thread. Ctrl-C stays quiet."""
    if issubclass(cls, KeyboardInterrupt):
        return
    _log_unhandled(MAIN_MESSAGE, exc)


def showwarning(
    message: Warning | str,
    category: type[Warning],
    filename: str,
    lineno: int,
    file: object = None,
    line: str | None = None,
) -> None:
    """For warnings.showwarning: the category only. The text can quote a value, as
    pydantic's serializer warnings do (review finding sec-1 of M1 PR 5)."""
    _log_unhandled(WARNING_MESSAGE, None, kind=category, level=logging.WARNING)


def thread_excepthook(args: threading.ExceptHookArgs) -> None:
    # SystemExit in a thread ends it quietly, as threading's own hook does.
    if args.exc_type is SystemExit:
        return
    _log_unhandled(THREAD_MESSAGE, args.exc_value)


def unraisable_hook(unraisable: sys.UnraisableHookArgs) -> None:
    _log_unhandled(UNRAISABLE_MESSAGE, unraisable.exc_value)


def asyncio_exception_handler(loop: object, context: dict) -> None:
    """For loop.set_exception_handler(): the context's message, task and transport are
    left out; they are reprs."""
    exc = context.get("exception")
    # A raise here would make asyncio fall back to its default handler with the context's
    # reprs (review finding sec-3); AsyncioFilter covers that path too.
    try:
        _log_unhandled(ASYNCIO_MESSAGE, exc if isinstance(exc, BaseException) else None)
    except Exception:  # noqa: BLE001, S110 - nothing of the context may reach stderr
        pass


def install_exception_hooks() -> None:
    """The process-wide hooks; the asyncio handler is per loop (app/main.py's lifespan)."""
    threading.excepthook = thread_excepthook
    sys.unraisablehook = unraisable_hook
    sys.excepthook = main_excepthook
    warnings.showwarning = showwarning
