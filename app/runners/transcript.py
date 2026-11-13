"""Recorded runs: the transcript format, the stale check, and reading one back (ADR 0014).

A recording is one JSON file per case, `recorded/<prompt version>/<case id>.json`:

    {"format": 1, "case_id": …,
     "prompt_version": …, "prompt_sha256": …, "tools_sha256": …, "corpus_sha256": …,
     "model": …,
     "samples": [{"date": "YYYY-MM-DD", "system": …, "messages": […], "replies": […],
                  "response": {"status": …, "body": …}}, …]}

- the five STALE_KEYS name what the run ran on: the prompt version and the template's
  sha256 (app/harness/prompt.py), the tools as the model was sent them, the reviewed
  corpus (app/config/corpus.py) and the model. current() gives today's values; a
  recording that differs in any is stale, and the gate fails it;
- a sample is one ask through the app: the date the prompt was filled in with, the
  system prompt as sent, the messages of the last model request (the question, every
  assistant turn and every tool result, as the model read them), every reply in order,
  and the route's status and JSON body.

sources() rebuilds the tool results the loop handed the number guard: each tool_result
that is not an error is the text wrap() wrote (app/harness/context.py), so its tool, its
record ids and its value come back exactly (wrap() escapes &, < and > only, and renders a
value that is not a str as JSON). The scorers call grounding.check() on them.

Nothing here prints a recording's text: problems name the file's path in the JSON and
the rule (corpus text and model output are untrusted, CLAUDE.md rule 9).
"""

from __future__ import annotations

import datetime
import hashlib
import html
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from app.config.corpus import MANIFEST_SHA256
from app.config.settings import DEFAULT_MODEL
from app.harness import loop
from app.harness.context import Provenance, Untrusted
from app.harness.prompt import PROMPT_SHA256, PROMPT_VERSION, system_prompt

FORMAT = 1
STALE_KEYS = ("prompt_version", "prompt_sha256", "tools_sha256", "corpus_sha256", "model")
TOP_KEYS = frozenset({"format", "case_id", *STALE_KEYS, "samples"})
SAMPLE_KEYS = frozenset({"date", "system", "messages", "replies", "response"})
RESPONSE_KEYS = frozenset({"status", "body"})
# wrap()'s output, exactly: the tool, the record ids, the escaped text.
_WRAPPED = re.compile(
    r'<untrusted tool="([^"]*)" records="([^"]*)">\n(.*)\n</untrusted>', re.DOTALL
)


def canonical_sha256(value: Any) -> str:
    """The sha256 of value as sorted-key, compact JSON (UTF-8, non-ASCII kept)."""
    text = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def tools_sha256(tools: Sequence[Mapping[str, Any]]) -> str:
    return canonical_sha256(list(tools))


def current() -> dict[str, str]:
    """What a fresh recording would carry under STALE_KEYS today."""
    return {
        "prompt_version": PROMPT_VERSION,
        "prompt_sha256": PROMPT_SHA256,
        "tools_sha256": tools_sha256(loop.tool_definitions()),
        "corpus_sha256": MANIFEST_SHA256,
        "model": DEFAULT_MODEL,
    }


def stale(recording: Mapping[str, Any]) -> list[str]:
    """The STALE_KEYS on which recording differs from current(), by name."""
    now = current()
    return [key for key in STALE_KEYS if recording.get(key) != now[key]]


def _is_text_block(block: object) -> bool:
    return isinstance(block, dict) and block.get("type") == "text"


def reply_text(reply: Mapping[str, Any]) -> str:
    """The text of a reply's text blocks, joined, as the loop takes an answer."""
    content = reply.get("content")
    if not isinstance(content, list):
        return ""
    return "".join(str(block.get("text", "")) for block in content if _is_text_block(block))


def tool_calls(replies: Sequence[Mapping[str, Any]]) -> list[str]:
    """The names of the tools the model called, in order, over every reply."""
    names = []
    for reply in replies:
        content = reply.get("content")
        if isinstance(content, list):
            names += [
                str(block.get("name"))
                for block in content
                if isinstance(block, dict) and block.get("type") == "tool_use"
            ]
    return names


def _tool_result_blocks(messages: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    blocks = []
    for message in messages:
        content = message.get("content")
        if message.get("role") == "user" and isinstance(content, list):
            blocks += [
                block
                for block in content
                if isinstance(block, dict) and block.get("type") == "tool_result"
            ]
    return blocks


class TranscriptError(ValueError):
    """A recorded tool result that wrap() did not write. Names the rule, not the text."""


def unwrap(text: str) -> Untrusted[Any]:
    """The Untrusted value wrap() rendered as text: its tool, record ids and value."""
    found = _WRAPPED.fullmatch(text)
    if found is None:
        raise TranscriptError("a tool result is not in wrap()'s form")
    tool = html.unescape(found.group(1))
    records = tuple(record for record in html.unescape(found.group(2)).split(",") if record)
    body = html.unescape(found.group(3))
    try:
        value: Any = json.loads(body)
    except ValueError:
        value = body
    return Untrusted(value, Provenance(tool=tool, record_ids=records))


def sources(messages: Sequence[Mapping[str, Any]]) -> list[Untrusted[Any]]:
    """The tool results the loop gave the number guard: every one that is not an error."""
    found = []
    for block in _tool_result_blocks(messages):
        if block.get("is_error") is True:
            continue
        content = block.get("content")
        if not isinstance(content, str):
            raise TranscriptError("a tool result's content is not one text")
        found.append(unwrap(content))
    return found


def tool_texts(messages: Sequence[Mapping[str, Any]]) -> list[str]:
    """Every tool result's text as the model read it, errors included."""
    return [str(block.get("content", "")) for block in _tool_result_blocks(messages)]


def _date(value: object) -> datetime.date | None:
    if not isinstance(value, str):
        return None
    try:
        day = datetime.date.fromisoformat(value)
    except ValueError:
        return None
    return day if day.isoformat() == value else None


def check(recording: object, case_id: str, samples: int) -> list[str]:
    """One line per way recording breaks the format or is stale (path and rule only)."""
    if not isinstance(recording, dict):
        return ["$: not a JSON object"]
    problems = []
    if set(recording) != TOP_KEYS:
        return [f"$: keys are not exactly {sorted(TOP_KEYS)}"]
    if recording["format"] != FORMAT:
        problems.append(f"$.format: not {FORMAT}")
    if recording["case_id"] != case_id:
        problems.append("$.case_id: not the file's case")
    problems += [f"$.{key}: stale (not the current {key})" for key in stale(recording)]
    items = recording["samples"]
    if not isinstance(items, list) or len(items) != samples:
        problems.append(f"$.samples: not a list of {samples} (gate.toml samples)")
        return problems
    for index, sample in enumerate(items):
        problems += [f"$.samples[{index}]{rule}" for rule in _sample_problems(sample)]
    return problems


def _sample_problems(sample: object) -> list[str]:
    if not isinstance(sample, dict) or set(sample) != SAMPLE_KEYS:
        return [f": keys are not exactly {sorted(SAMPLE_KEYS)}"]
    problems = []
    day = _date(sample["date"])
    if day is None:
        problems.append(".date: not an ISO date")
    elif sample["system"] != system_prompt(day):
        problems.append(".system: not the current prompt on the sample's date (stale)")
    messages, replies, response = sample["messages"], sample["replies"], sample["response"]
    if not isinstance(messages, list) or not all(isinstance(m, dict) for m in messages):
        problems.append(".messages: not a list of objects")
    else:
        try:
            sources(messages)
        except TranscriptError as error:
            problems.append(f".messages: {error}")
    if not isinstance(replies, list) or not all(isinstance(r, dict) for r in replies):
        problems.append(".replies: not a list of objects")
    if not isinstance(response, dict) or set(response) != RESPONSE_KEYS:
        problems.append(f".response: keys are not exactly {sorted(RESPONSE_KEYS)}")
    elif type(response["status"]) is not int:
        problems.append(".response.status: not an integer")
    elif response["status"] == 200 and not _body_ok(response["body"]):
        problems.append(".response.body: not the API's answer shape")
    return problems


_COUNTS = ("figures", "unmatched", "unknown_citations")


def _body_ok(body: object) -> bool:
    if not isinstance(body, dict):
        return False
    grounding, citations, usage = body.get("grounding"), body.get("citations"), body.get("usage")
    return (
        isinstance(body.get("answer"), str)
        and isinstance(body.get("stop"), str)
        and isinstance(grounding, dict)
        and all(type(grounding.get(key)) is int for key in _COUNTS)
        and isinstance(citations, list)
        and all(
            isinstance(item, dict) and isinstance(item.get("record_id"), str) for item in citations
        )
        and isinstance(usage, dict)
        and all(type(usage.get(key)) is int for key in ("input_tokens", "output_tokens"))
    )


def sample_date(sample: Mapping[str, Any]) -> datetime.date:
    """The sample's date; call after check() found no problem."""
    day = _date(sample["date"])
    if day is None:
        raise TranscriptError("the sample's date is not an ISO date")
    return day
