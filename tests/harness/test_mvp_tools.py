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

from app.harness import mvp_tools
from app.harness.context import Untrusted
from app.harness.tools import DataClass, Effect, RegistrationError, ToolRegistry

# The plan's identity terms, written out here rather than taken from app/harness/tools.py,
# so this check does not depend on the matcher it backs up.
IDENTITY = re.compile(
    r"advisor|user|principal|tenant|owner|identity|role|on_?behalf|berater|benutzer",
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
