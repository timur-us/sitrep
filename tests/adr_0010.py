"""ADR 0010's yaml block, for the tests that pin the loader and the schema to it.

tests/test_docs.py checks the block itself (one block, no repeated key, the generator's
origins and trust); this helper only reads it.
"""

from __future__ import annotations

import re
from typing import Any

import yaml
from repo_files import ROOT

FENCE = re.compile(r"^```yaml\n(.*?)^```", re.DOTALL | re.MULTILINE)


def block() -> dict[str, Any]:
    found = sorted((ROOT / "docs" / "decisions").glob("0010-*.md"))
    assert len(found) == 1, f"expected one ADR 0010, found {len(found)}"
    blocks = FENCE.findall(found[0].read_text(encoding="utf-8"))
    assert len(blocks) == 1, f"expected one yaml block in ADR 0010, found {len(blocks)}"
    facts = yaml.safe_load(blocks[0])
    assert isinstance(facts, dict), "ADR 0010's yaml block is not a mapping"
    return facts


def trust_of() -> dict[str, str]:
    """Every origin of the block, corpus and runtime, with its trust."""
    facts = block()
    return {**facts["corpus_origins"], **facts["runtime_origins"]}
