"""Pins the guardrail wiring in .claude/settings.json.

The hooks only guard if Claude Code runs them: both Bash hooks must be wired to the
Bash matcher with `python3 -I` and `"onFailure": "block"`, so that a crash, a
timeout or a missing interpreter blocks the call. The recorded eval and red-team
runs are evidence and stay read-only: the Edit deny rules cover Write too.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

SETTINGS = Path(__file__).resolve().parents[2] / ".claude" / "settings.json"

BASH_HOOKS = ["block_secrets.py", "block_guardrail_writes.py"]

# Edit(...) deny rules cover Write as well (Claude Code reports Write(...) rules
# under them as never matching), so only the Edit rules are listed.
RECORDED_DENIES = [
    "Edit(evals/recorded/**)",
    "Edit(redteam/recorded/**)",
]


def settings() -> dict:
    return json.loads(SETTINGS.read_text(encoding="utf-8"))


def bash_hooks() -> list[dict]:
    entries = settings()["hooks"]["PreToolUse"]
    return [hook for entry in entries if entry.get("matcher") == "Bash" for hook in entry["hooks"]]


@pytest.mark.parametrize("script", BASH_HOOKS)
def test_bash_hook_is_wired_and_fails_closed(script: str) -> None:
    expected = f'python3 -I "${{CLAUDE_PROJECT_DIR}}/.claude/hooks/{script}"'
    wired = [hook for hook in bash_hooks() if hook.get("command") == expected]
    assert len(wired) == 1, f"{script} is not wired exactly once on Bash"
    assert wired[0]["type"] == "command"
    assert wired[0]["onFailure"] == "block"


@pytest.mark.parametrize("script", BASH_HOOKS)
def test_hook_script_exists(script: str) -> None:
    assert (SETTINGS.parent / "hooks" / script).is_file()


@pytest.mark.parametrize("rule", RECORDED_DENIES)
def test_recorded_runs_are_denied(rule: str) -> None:
    assert rule in settings()["permissions"]["deny"]
