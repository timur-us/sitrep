"""A scripted model client for the route tests (M1 PR 12): no socket, no key.

EchoModel stands in for the Messages API behind POST /api/ask. Its first reply asks for
one search_client_docs call; its second reads the record ids the loop put on that tool
result (wrap()'s `records` attribute) and cites the first few of them. So an answer cites
exactly what the advisor's own search returned: a test of the boundary needs no ids
chosen by hand, and an id of another advisor could only reach the answer through the
tool result.

It records every request it receives, and runs an optional hook at the start of each
call, from the thread the loop runs in: the pool's state, a nested request or the clock
can be checked or moved there. The tests in tests/web start it in the app's own process
(tests/web/ask_server.py), so it imports nothing but app/ and the standard library.
"""

from __future__ import annotations

import copy
import html
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from app.config.settings import DEFAULT_MODEL

SEARCH = "search_client_docs"
RECORDS = re.compile(r'<untrusted tool="([^"]*)" records="([^"]*)">')
USAGE = {"input_tokens": 1_200, "output_tokens": 80}


def reply(*content: dict[str, Any], stop: str = "end_turn", model: str = DEFAULT_MODEL) -> dict:
    """A Messages API response as the model client returns it (a mapping)."""
    return {
        "id": "msg_scripted",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": list(content),
        "stop_reason": stop,
        "stop_sequence": None,
        "usage": dict(USAGE),
    }


def text(value: str) -> dict[str, Any]:
    return {"type": "text", "text": value, "citations": None}


def tool_use(block_id: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {"type": "tool_use", "id": block_id, "name": name, "input": arguments}


def tool_results(messages: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """The tool_result blocks of the last message, if it is the loop's user turn."""
    last = messages[-1]
    content = last.get("content")
    if last.get("role") != "user" or not isinstance(content, list):
        return []
    return [block for block in content if block.get("type") == "tool_result"]


def records(block: Mapping[str, Any]) -> list[str]:
    """The record ids wrap() names on one tool result, in order."""
    found = RECORDS.match(str(block.get("content", "")))
    if found is None:
        return []
    return [record for record in html.unescape(found.group(2)).split(",") if record]


class EchoModel:
    """Searches for `query`, then cites up to `cite` of the records the search returned.

    `arguments` adds to the search call's (a mandate id, a limit); `answer` is the text before the citations; `on_call(model)` runs at the start of
    every call. `script`, when given, replaces the behaviour: the replies (or exceptions)
    in order.
    """

    def __init__(
        self,
        query: str,
        *,
        answer: str = "Briefing before the call.",
        cite: int = 3,
        arguments: Mapping[str, Any] | None = None,
        model: str = DEFAULT_MODEL,
        on_call: Callable[[EchoModel], None] | None = None,
        script: Sequence[object] | None = None,
    ) -> None:
        self.query = query
        self.answer = answer
        self.cite = cite
        self.arguments = dict(arguments or {})
        self.model = model
        self.on_call = on_call
        self.script = list(script) if script is not None else None
        self.calls: list[dict[str, Any]] = []

    def create(self, **request: Any) -> dict[str, Any]:
        self.calls.append(copy.deepcopy(request))
        if self.on_call is not None:
            self.on_call(self)
        if self.script is not None:
            if not self.script:
                raise AssertionError("the loop called the model more often than scripted")
            item = self.script.pop(0)
            if isinstance(item, BaseException):
                raise item
            return copy.deepcopy(item)  # type: ignore[arg-type]
        results = tool_results(request["messages"])
        if not results:
            search = {"query": self.query, **self.arguments}
            return reply(tool_use("tu_1", SEARCH, search), stop="tool_use")
        cited = [record for block in results for record in records(block)][: self.cite]
        sources = " ".join(f"[source: {record}]" for record in cited)
        return reply(text(f"{self.answer} {sources}".strip()), model=self.model)

    def seen(self) -> str:
        """Everything the model was sent, as one text (for boundary assertions)."""
        return repr(self.calls)
