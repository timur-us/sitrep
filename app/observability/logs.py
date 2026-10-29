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
    "filters": {"redact": {"()": "app.observability.logs.RedactFilter"}},
    "formatters": {"safe": {"()": "app.observability.logs.SafeFormatter"}},
    "handlers": {
        "stderr": {
            "class": "app.observability.logs.StderrHandler",
            "formatter": "safe",
            "filters": ["redact"],
        }
    },
    "loggers": {name: {"level": "INFO", "filters": ["redact"]} for name in REDACTED_LOGGERS},
    "root": {"level": "INFO", "handlers": ["stderr"]},
}
