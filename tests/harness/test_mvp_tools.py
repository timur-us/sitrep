"""The four MVP tools are declared as docs/brief.md says (plan PR 6).

search_client_docs and get_portfolio read; draft_email drafts and create_followup has a
side effect, and both need the advisor's approval. The brief does not fix data classes;
all four touch client data, so all four are declared CONFIDENTIAL. Declared only: the
handlers are M1's.
"""

from __future__ import annotations

import re
from typing import get_args, get_origin

import pytest
from pydantic import ValidationError

from app.harness import mvp_tools
from app.harness.context import Untrusted
from app.harness.tools import (
    DataClass,
    Effect,
    RegistrationError,
    RejectedCall,
    ToolRegistry,
    ValidCall,
)

# The plan's identity terms, written out here rather than taken from app/harness/tools.py,
# so this check does not depend on the matcher it backs up.
IDENTITY = re.compile(
    r"advisor|user|principal|tenant|owner|identity|role|on_?behalf|berater|benutzer|mitarbeiter",
    re.IGNORECASE,
)

DECLARED = {
    "search_client_docs": (Effect.READ, False),
    "get_portfolio": (Effect.READ, False),
    "draft_email": (Effect.DRAFT, True),
    "create_followup": (Effect.SIDE_EFFECT, True),
}


def test_mvp_tool_declarations() -> None:
    registry = mvp_tools.build_registry()
    assert registry.names() == tuple(DECLARED)
    declared = {
        name: (registry.spec(name).effect, registry.spec(name).requires_approval)
        for name in registry.names()
    }
    assert declared == DECLARED


def test_mvp_data_classes() -> None:
    registry = mvp_tools.build_registry()
    classes = {name: registry.spec(name).data_class for name in registry.names()}
    assert classes == dict.fromkeys(DECLARED, DataClass.CONFIDENTIAL)


@pytest.mark.parametrize("name", ["search_client_docs", "get_portfolio"])
def test_mvp_read_tools_return_untrusted(name: str) -> None:
    result = mvp_tools.build_registry().spec(name).result
    assert get_origin(result) is Untrusted and get_args(result), result


def property_names(node: object) -> list[str]:
    """Every property name anywhere in a JSON schema."""
    found = []
    stack = [node]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            found += list(item.get("properties", {}))
            stack += list(item.values())
        elif isinstance(item, list):
            stack += item
    return found


@pytest.mark.parametrize("name", list(DECLARED))
def test_mvp_schemas_carry_no_identity(name: str) -> None:
    schema = mvp_tools.build_registry().schema(name)
    names = property_names(schema)
    assert names, f"{name}: no properties found"
    assert [n for n in names if IDENTITY.search(n.replace("-", "_"))] == []
    assert "mandate_id" in names, names


def test_mvp_tools_go_through_the_checks() -> None:
    # build_registry() registers through register(): a second registration is refused.
    registry = mvp_tools.build_registry()
    with pytest.raises(RegistrationError):
        registry.register(mvp_tools.TOOLS[0])
    fresh = ToolRegistry()
    for spec in mvp_tools.TOOLS:
        fresh.register(spec)
    assert fresh.names() == registry.names()


# Review findings of PR 6 (test-8, test-15, sec-5).


def test_build_registry_calls_register_for_each_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    # test-8: the duplicate check above would pass even if build_registry() wrote the
    # specs in directly; a spy on register() shows each tool went through the checks.
    seen = []
    real = ToolRegistry.register

    def spy(self: ToolRegistry, spec: object) -> None:
        seen.append(spec.name)  # type: ignore[attr-defined]
        real(self, spec)  # type: ignore[arg-type]

    monkeypatch.setattr(ToolRegistry, "register", spy)
    registry = mvp_tools.build_registry()
    assert seen == list(DECLARED) == list(registry.names())
    assert all(ToolRegistry().problems(spec) == [] for spec in mvp_tools.TOOLS)


DRAFT = {"mandate_id": "m-1", "to": ["client@example.org"], "subject": "Call", "body": "Hello"}
FOLLOWUP = {"mandate_id": "m-1", "title": "Send the report", "due": "2026-11-20"}
CONTROLS = {
    "search_client_docs": {"query": "x"},
    "get_portfolio": {"mandate_id": "m-1"},
    "draft_email": DRAFT,
    "create_followup": FOLLOWUP,
}


@pytest.mark.parametrize(
    ("tool", "arguments", "kind"),
    [
        ("search_client_docs", {"query": "x", "limit": 21}, "less_than_equal"),
        ("search_client_docs", {"query": "x", "limit": 0}, "greater_than_equal"),
        ("search_client_docs", {"query": ""}, "string_too_short"),
        ("search_client_docs", {"query": "x" * 501}, "string_too_long"),
        ("get_portfolio", {"mandate_id": "x" * 129}, "string_too_long"),
        ("get_portfolio", {}, "missing"),
        ("draft_email", DRAFT | {"to": []}, "too_short"),
        ("draft_email", DRAFT | {"to": ["c@example.org"] * 11}, "too_long"),
        ("draft_email", DRAFT | {"to": ["c"]}, "string_too_short"),
        ("draft_email", DRAFT | {"body": "x" * 20_001}, "string_too_long"),
        ("create_followup", FOLLOWUP | {"due": "next week"}, "date_from_datetime_parsing"),
        ("create_followup", FOLLOWUP | {"notes": "x" * 2_001}, "string_too_long"),
    ],
    ids=lambda value: value if isinstance(value, str) and len(value) < 30 else "",
)
def test_mvp_arguments_are_bounded(tool: str, arguments: dict[str, object], kind: str) -> None:
    # test-15
    registry = mvp_tools.build_registry()
    control = registry.validate_call(tool, CONTROLS[tool])
    assert isinstance(control, ValidCall), f"{tool}: the control call is rejected: {control}"
    outcome = registry.validate_call(tool, arguments)
    assert isinstance(outcome, RejectedCall), f"{tool} accepted {arguments}"
    assert kind in [error.type for error in outcome.errors], outcome.errors


def test_validated_arguments_cannot_change() -> None:
    # test-15 / sec-5: a proposed call is what the advisor approves; nothing changes it
    # after validation, not even a recipient appended to the list.
    outcome = mvp_tools.build_registry().validate_call("draft_email", DRAFT)
    assert isinstance(outcome, ValidCall), outcome
    arguments = outcome.arguments
    with pytest.raises(ValidationError):
        arguments.subject = "Other"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        arguments.to.append("someone@example.org")  # type: ignore[attr-defined]
    assert arguments.to == ("client@example.org",)  # type: ignore[attr-defined]
