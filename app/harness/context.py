"""The context layer: tool results as untrusted data, and the token budget.

Everything a READ tool returns is data, never instructions (CLAUDE.md rule 9; threat-model
path 1). Untrusted[T] holds such a result and its Provenance, and cannot be rendered by
accident:
- it is not a str; str(), format(), f-strings, %-formatting and + raise TypeError, so it
  cannot be pasted into a prompt, a log line or an answer;
- repr() shows a random reference and the provenance only, so a traceback, a debugger or
  a log call that repr()s its arguments never carries the text (CLAUDE.md rule 10);
- it has no public attribute that returns the value;
- it cannot be subclassed, so no subclass can add a rendering;
- it cannot be pickled, copied or deep-copied.

wrap() is the one way to the text: it renders the value inside an `<untrusted …>`
delimiter that names the tool and the records it came from. The text and the provenance
are escaped, so a document that says `</untrusted>` cannot close the delimiter and write
outside it. The cost: the model reads `&`, `<` and `>` as entities (`R&amp;D`, `&lt;5 %`)
and may quote them so; the number guard compares against the source, not this text.
A str is rendered as it is; anything else as JSON through pydantic. M2 adds
the canary scan and measures the provenance rendering against the red-team corpus; M0
fixes that wrap() is the only way out and that the text stays inside.

Python cannot hide the value: `_value` is reachable. The rule is that code
outside this module never reads it; M1's number guard, the one other reader, gets a
named accessor in its own PR.

TokenBudget is a Protocol; its M1 contract is in its docstring.
"""

from __future__ import annotations

import html
import secrets
from dataclasses import dataclass
from typing import Any, NoReturn, Protocol

from pydantic import TypeAdapter

RENDER_REFUSED = "an Untrusted value is rendered only through app.harness.context.wrap()"
CLOSE = "</untrusted>"
_JSON = TypeAdapter(Any)


@dataclass(frozen=True, slots=True, kw_only=True)
class Provenance:
    """Where an untrusted value came from: the tool, and the records it read, in order."""

    tool: str
    record_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.tool) is not str:
            raise TypeError(f"Provenance.tool is a str, not {type(self.tool).__name__}")
        if not self.tool:
            raise ValueError("Provenance.tool is empty")
        if type(self.record_ids) is not tuple:
            raise TypeError(
                f"Provenance.record_ids is a tuple, not {type(self.record_ids).__name__}"
            )
        for record_id in self.record_ids:
            if type(record_id) is not str:
                raise TypeError(f"a record id is a str, not {type(record_id).__name__}")
            if not record_id:
                raise ValueError("a record id is empty")


class Untrusted[T]:
    """A tool result the model may read, inside wrap(), and nothing else may render."""

    __slots__ = ("_provenance", "_ref", "_value")

    def __init__(self, value: T, provenance: Provenance) -> None:
        if type(provenance) is not Provenance:
            raise TypeError(f"Untrusted takes a Provenance, not {type(provenance).__name__}")
        self._value = value
        self._provenance = provenance
        self._ref = secrets.token_hex(6)

    def __init_subclass__(cls, **kwargs: Any) -> NoReturn:
        raise TypeError("Untrusted cannot be subclassed")

    @property
    def provenance(self) -> Provenance:
        return self._provenance

    @property
    def ref(self) -> str:
        """A random reference for traces and errors; it says nothing about the value."""
        return self._ref

    def __repr__(self) -> str:
        return f"Untrusted(ref={self._ref!r}, provenance={self._provenance!r})"

    def __str__(self) -> NoReturn:
        raise TypeError(RENDER_REFUSED)

    def __format__(self, spec: str) -> NoReturn:
        raise TypeError(RENDER_REFUSED)

    def __bytes__(self) -> NoReturn:
        raise TypeError(RENDER_REFUSED)

    def __add__(self, other: object) -> NoReturn:
        raise TypeError(RENDER_REFUSED)

    def __radd__(self, other: object) -> NoReturn:
        raise TypeError(RENDER_REFUSED)

    # No pickle, copy or deepcopy: a serialised value is out of reach of every rule here
    # (a cache, a queue, a file), and a copy would carry the same reference (Fable's review).
    def __reduce__(self) -> NoReturn:
        raise TypeError(RENDER_REFUSED)

    def __reduce_ex__(self, protocol: object) -> NoReturn:
        raise TypeError(RENDER_REFUSED)


def wrap(item: Untrusted[Any]) -> str:
    """The text of item, for the model's context, inside a delimiter it cannot close."""
    if type(item) is not Untrusted:
        raise TypeError(f"wrap() takes an Untrusted, not {type(item).__name__}")
    value = item._value
    text = value if type(value) is str else _JSON.dump_json(value).decode("utf-8")
    tool = html.escape(item._provenance.tool, quote=True)
    records = html.escape(",".join(item._provenance.record_ids), quote=True)
    body = html.escape(text, quote=False)
    return f'<untrusted tool="{tool}" records="{records}">\n{body}\n{CLOSE}'


class BudgetExceeded(Exception):
    """A request ran out of tokens; the loop stops and says so (M1)."""


class TokenBudget(Protocol):
    """The tokens one advisor request may spend (threat-model path 8).

    M1 contract:
    - one budget per request, set before the first model call and never raised by the
      model or by a tool result;
    - charge() is called with the usage the API reports after every model call, and with
      the size of every wrap() output before it enters the context;
    - charge() raises BudgetExceeded once remaining() would go below zero; the loop then
      ends the turn with a stated reason, and never truncates a tool result silently;
    - remaining() never goes up within a request.
    - The rate limit per identity (path 8, many identities) sits beside it, keyed by the verified
      advisor; it is not this Protocol's.
    """

    def remaining(self) -> int: ...

    def charge(self, tokens: int) -> None: ...
