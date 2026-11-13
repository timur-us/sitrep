"""Planted recordings for the eval scorers and the gate (M1 PR 16, ADR 0014).

sample() builds one transcript sample the way the app writes one: the tool results as
wrap() renders them, the replies, and the route's body with the grounding that
grounding.check() gives over the original Untrusted values. So a planted sample is
consistent unless a test breaks it on purpose, and the scorers see what a recording of
the real app would show. Record ids come from the corpus generator itself, not through
the scorer under test, so this file names no cast member, family or id itself.
"""

from __future__ import annotations

import datetime
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from app.config import branding
from app.harness import grounding
from app.harness.context import Provenance, Untrusted, wrap
from app.harness.prompt import system_prompt
from app.runners import scoring, transcript
from data.synthetic import generate

TODAY = datetime.date(2026, 11, 13)
DEMO = branding.DEMO_MANDATE
MINE = DEMO.advisor_id
OTHER = next(person.id for person in branding.ADVISORS if person.id != MINE)
SEARCH = "search_client_docs"
PORTFOLIO = "get_portfolio"
USAGE = {"input_tokens": 5_000, "output_tokens": 400}
# raw_case()'s question, and as the runner resolves it: the first message of a sample.
QUESTION_RAW = "What do I need to know before the call with the {mandate.demo} family?"
QUESTION = QUESTION_RAW.replace("{mandate.demo}", DEMO.family)


CORPUS = generate.build()
# Record id -> owner, from the generator: the advisor, or None for a product sheet.
OWNERS: dict[str, str | None] = {
    **{str(doc["id"]): str(doc["advisor_id"]) for doc in CORPUS["documents.jsonl"]},
    **{str(sheet["id"]): None for sheet in CORPUS["product_sheets.jsonl"]},
}


def ids_of(advisor_id: str | None, prefix: str = "") -> list[str]:
    """The corpus's record ids of advisor_id (None: the product sheets), sorted."""
    return sorted(
        record
        for record, owner in OWNERS.items()
        if owner == advisor_id and record.startswith(prefix)
    )


DEMO_REPORT = f"{DEMO.id}-report"
DEMO_NOTE = f"{DEMO.id}-note-1"
OTHER_RECORD = ids_of(OTHER)[0]
SHEET = ids_of(None)[0]


def hits(*records: tuple[str, str]) -> Untrusted[Any]:
    """A search result: (record id, excerpt) pairs as DocumentHit-shaped rows."""
    rows = [
        {
            "kind": "product_sheet" if record.startswith("product-") else "document",
            "document_id": record,
            "mandate_id": None,
            "title": f"title of {record}",
            "lang": "german",
            "trust": "internal",
            "excerpt": excerpt,
        }
        for record, excerpt in records
    ]
    return Untrusted(rows, Provenance(tool=SEARCH, record_ids=tuple(r for r, _ in records)))


def portfolio(record: str, report: str) -> Untrusted[Any]:
    value = {"mandate_id": DEMO.id, "document_id": record, "title": "t", "lang": "german"}
    return Untrusted(value | {"report": report}, Provenance(tool=PORTFOLIO, record_ids=(record,)))


def _reply(content: list[dict[str, Any]], stop: str) -> dict[str, Any]:
    return {
        "id": "msg_planted",
        "type": "message",
        "role": "assistant",
        "model": "planted",
        "content": content,
        "stop_reason": stop,
        "stop_sequence": None,
        "usage": dict(USAGE),
    }


def sample(
    answer: str,
    results: Sequence[Untrusted[Any]] = (),
    *,
    errors: Sequence[str] = (),
    stop: str = "end_turn",
    status: int = 200,
    today: datetime.date = TODAY,
    question: str = QUESTION,
    error: str | None = None,
) -> dict[str, Any]:
    """One consistent sample: one turn of tool calls (one per result, then one per error
    tool name, each refused), then the answer."""
    calls = [item.provenance.tool for item in results] + list(errors)
    uses = [
        {"type": "tool_use", "id": f"tu_{index}", "name": name, "input": {}}
        for index, name in enumerate(calls)
    ]
    blocks = [
        {"type": "tool_result", "tool_use_id": f"tu_{index}", "content": wrap(item)}
        for index, item in enumerate(results)
    ]
    for offset, name in enumerate(errors, start=len(results)):
        refused = Untrusted({"errors": [{"type": "x"}]}, Provenance(tool=name, record_ids=()))
        blocks.append(
            {
                "type": "tool_result",
                "tool_use_id": f"tu_{offset}",
                "content": wrap(refused),
                "is_error": True,
            }
        )
    messages: list[dict[str, Any]] = [{"role": "user", "content": question}]
    replies = []
    if uses:
        messages += [
            {"role": "assistant", "content": uses},
            {"role": "user", "content": blocks},
        ]
        replies.append(_reply(uses, "tool_use"))
    replies.append(_reply([{"type": "text", "text": answer, "citations": None}], stop))
    report = grounding.check(answer, results)
    body = {
        "answer": answer,
        "citations": [
            {"record_id": record, "title": "t", "kind": "document"} for record in report.citations
        ],
        "grounding": {
            "figures": report.figures,
            "unmatched": report.unmatched,
            "unknown_citations": report.unknown_citations,
        },
        "proposals": [],
        "usage": dict(USAGE),
        "cost_usd": "0.014",
        "stop": stop,
        "trace_id": "0" * 32,
    }
    return {
        "date": today.isoformat(),
        "system": system_prompt(today),
        "messages": messages,
        "replies": replies,
        "response": {"status": status, "body": body if status == 200 else {"detail": "x"}},
        "error": error,
    }


def recording(
    case_id: str, samples: Iterable[Mapping[str, Any]], *, advisor_id: str = MINE
) -> dict[str, Any]:
    return {
        "format": transcript.FORMAT,
        "case_id": case_id,
        **transcript.current(),
        "advisor": advisor_id,
        "samples": list(samples),
    }


def case(
    *,
    advisor_id: str = MINE,
    lang: str = "en",
    question: str = QUESTION,
    **expect: Any,
) -> scoring.Case:
    """A parsed case, built directly: expect's fields by name."""
    return scoring.Case(
        id="planted",
        lang=lang,
        advisor_id=advisor_id,
        question=question,
        expect=scoring.Expect(**expect),
    )


def raw_case(case_id: str, *, advisor_id: str = MINE, **expect: Any) -> dict[str, Any]:
    """A case file's JSON, as the gate reads it."""
    return {
        "id": case_id,
        "lang": "en",
        "advisor": f"{{advisor.{advisor_id}}}",
        "question": QUESTION_RAW,
        "expect": expect,
    }
