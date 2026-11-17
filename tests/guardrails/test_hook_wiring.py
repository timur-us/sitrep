"""Pins the guardrail wiring in .claude/settings.json.

The hooks only guard if Claude Code runs them: both Bash hooks must be wired to the
Bash matcher with `python3 -I`, a short timeout and `"onFailure": "block"`, meant
to make a crash, a timeout or a missing interpreter block the call (whether Claude
Code honours that key is checked by hand, not here). The recorded eval and red-team
runs are evidence: the Edit tool may not change them (the Edit deny rules cover
Write too, per Timur's 4460840); a Bash write to them is outside the hook's paths.
The rule-11 rules keep the file tools off the guardrail files.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

SETTINGS = Path(__file__).resolve().parents[2] / ".claude" / "settings.json"

BASH_HOOKS = ["block_secrets.py", "block_guardrail_writes.py"]

DENY = [
    # The recorded runs.
    "Edit(evals/recorded/**)",
    "Edit(redteam/recorded/**)",
    # Rule 11: never changed by the agent.
    "Edit(.claude/settings*.json)",
    "Edit(.claude/hooks/**)",
]

ASK = [
    # Rule 11: changed only after Timur's yes to that single edit.
    "Edit(CLAUDE.md)",
    "Edit(.claude/agents/**)",
    "Edit(.claude/skills/**)",
    "Edit(.github/**)",
    # Live model calls and the runners cost money.
    "Bash(*--live*)",
    "Bash(*app.runners*)",
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
    assert 0 < wired[0]["timeout"] <= 10


@pytest.mark.parametrize("script", BASH_HOOKS)
def test_hook_script_exists(script: str) -> None:
    assert (SETTINGS.parent / "hooks" / script).is_file()


@pytest.mark.parametrize("rule", DENY)
def test_denied(rule: str) -> None:
    assert rule in settings()["permissions"]["deny"]


@pytest.mark.parametrize("rule", ASK)
def test_asked(rule: str) -> None:
    assert rule in settings()["permissions"]["ask"]


@pytest.mark.parametrize("mode", ["disableBypassPermissionsMode", "disableAutoMode"])
def test_permission_modes_stay_disabled(mode: str) -> None:
    assert settings()["permissions"][mode] == "disable"
