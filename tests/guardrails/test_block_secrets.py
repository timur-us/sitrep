"""Tests for the secret hook in .claude/hooks/block_secrets.py.

The hook is a guardrail, so it is tested like any other code. Each test runs the
script the way Claude Code does: `python3 -I`, the tool call as JSON on stdin.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[2] / ".claude" / "hooks" / "block_secrets.py"

BLOCKED = [
    "cat .env",
    "cat -n .env",
    "head -c 200 .env",
    "grep ANTHROPIC .env",
    'cat ".env"',
    "cat .env.local",
    "cat < .env",
    "cat\t.env",
    "cat app/.env",
    "cat .ENV",
    "python3 -c \"print(open('.env').read())\"",
    "docker run --rm --env-file .env alpine env",
    "docker run --rm --env-file=.env alpine env",
    "echo sk-ant-api03-AAAAAAAAAAAAAAAA",
    "ANTHROPIC_API_KEY=abc make evals",
    'echo "$ANTHROPIC_API_KEY"',
    "printenv ANTHROPIC_API_KEY",
    "az keyvault secret show --name k --vault-name v",
    "az containerapp secret list -n a -g b --show-values",
]

ALLOWED = [
    "git status",
    "cat .env.example",
    "git add .env.example .gitignore",
    "python -m venv .venv",
    "source .venv/bin/activate",
    "ruff check .",
    "pytest -q tests/test_environment.py",
    "ANTHROPIC_API_KEY= make test",
    "gh secret list",
    "docker compose up -d db",
]


def run(command: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
    return subprocess.run(
        [sys.executable, "-I", str(HOOK)],
        input=payload,
        text=True,
        capture_output=True,
        cwd=cwd,
        check=False,
    )


@pytest.mark.parametrize("command", BLOCKED)
def test_blocks(command: str) -> None:
    assert run(command).returncode == 2


@pytest.mark.parametrize("command", ALLOWED)
def test_allows(command: str) -> None:
    assert run(command).returncode == 0


def test_unreadable_input_is_blocked() -> None:
    result = subprocess.run(
        [sys.executable, "-I", str(HOOK)],
        input="not json",
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 2


def test_json_module_in_working_directory_is_ignored(tmp_path: Path) -> None:
    (tmp_path / "json.py").write_text("def load(f):\n    return {}\n")
    assert run("cat .env", cwd=tmp_path).returncode == 2
