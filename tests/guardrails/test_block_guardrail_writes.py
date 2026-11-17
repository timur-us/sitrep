"""Tests for the guardrail-write hook in .claude/hooks/block_guardrail_writes.py.

The hook is a guardrail, so it is tested like any other code. Each test runs the
script the way Claude Code does: `python3 -I`, the tool call as JSON on stdin.
Read-only commands that name CLAUDE.md, .claude/ or .github/ pass; anything that
could write one of them is blocked with exit 2 and the rule-11 message; commands
that name none of them are not the hook's business.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[2] / ".claude" / "hooks" / "block_guardrail_writes.py"

RULE_11 = "Guardrail files belong to Timur (rule 11)"

# The read-only programs and git subcommands, listed here rather than imported from
# the hook, so that taking one off is a visible change to this file. uniq left the
# hook's list in 71ab9b4 (`uniq IN OUT` writes OUT) and is blocked in WRITES.
READ_ONLY_PROGRAMS = [
    "cat",
    "head",
    "tail",
    "less",
    "more",
    "grep",
    "rg",
    "egrep",
    "fgrep",
    "ls",
    "wc",
    "file",
    "stat",
    "cut",
    "tr",
    "jq",
    "md5",
    "shasum",
    "sha256sum",
    "echo",
    "printf",
]
READ_ONLY_GIT = [
    "diff",
    "show",
    "log",
    "status",
    "blame",
    "ls-files",
    "grep",
    "cat-file",
    "rev-parse",
]

READ_ONLY = [
    "cat CLAUDE.md",
    "cat .claude/settings.json",
    "grep -n onFailure .claude/settings.json",
    "grep -rn pytest .github/workflows",
    "git diff main -- CLAUDE.md .claude/ .github/",
    "git show HEAD:.claude/hooks/block_guardrail_writes.py",
    "git show HEAD -- .github/workflows/ci.yml",
    "git log --oneline -- .claude/hooks",
    "ls .claude/hooks",
    "ls -la .github",
    "cat CLAUDE.md | head -20",
    "git log -p -- CLAUDE.md | head -50",
    "diff CLAUDE.md .claude/settings.json",
    "test -f CLAUDE.md",
    "[ -f .claude/settings.json ]",
    # -o is not --output: grep prints only the match, ls prints the long format.
    "grep -o onFailure .claude/settings.json",
    "grep -rno 'python3 -I' .claude/settings.json",
    "ls -o .github",
    "ls -lo .claude/hooks",
    # A run flag must stand on its own: --pretty is not --pre, -OSX inside a name is
    # not git grep's -O.
    "git log --pretty=oneline -- CLAUDE.md",
    "cat .github/workflows/build-OSX.yml",
    # Empty segments run nothing.
    "ls .github || echo none",
    "cat CLAUDE.md ;",
]

WRITES = [
    "sed -i '' 's/never/always/' CLAUDE.md",
    "sed -i 's/block/allow/' .claude/settings.json",
    "echo '# ok' > CLAUDE.md",
    "echo '{}' >> .claude/settings.json",
    "echo x | tee .github/workflows/ci.yml",
    "tee -a CLAUDE.md < /dev/null",
    "cp /tmp/settings.json .claude/settings.json",
    "mv .claude/hooks/block_secrets.py /tmp/",
    "rm .github/workflows/ci.yml",
    "rm -rf .claude/hooks",
    "install -m 644 /tmp/x .claude/hooks/x.py",
    "git checkout -- .claude/settings.json",
    "git checkout main -- CLAUDE.md",
    "git add CLAUDE.md",
    "git add .github/workflows/ci.yml",
    "git restore .claude",
    "git rm CLAUDE.md",
    "git reset --hard -- .github",
    "git apply /tmp/x.patch .claude",
    "git -C . diff CLAUDE.md",
    "python3 - <<'EOF'\nopen('CLAUDE.md', 'w').write('')\nEOF",
    "python3 -c \"open('.claude/settings.json', 'w').write('{}')\"",
    "python CLAUDE.md",
    "perl -pi -e 's/a/b/' CLAUDE.md",
    "bash -c 'rm CLAUDE.md'",
    # A segment must start with the program itself: no prefix in front of it.
    "env rm CLAUDE.md",
    "command rm CLAUDE.md",
    "sudo rm CLAUDE.md",
    "FOO=1 cat CLAUDE.md",
    "cd .github && rm -rf workflows",
    "cd .github ; rm workflows/ci.yml",
    "ls .claude/hooks && rm .claude/hooks/block_secrets.py",
    "ls .claude/hooks | xargs rm",
    "cat CLAUDE.md || rm CLAUDE.md",
    "cat CLAUDE.md; rm CLAUDE.md",
    # An unquoted heredoc runs $(...) in its body, even for a read-only program.
    "cat <<EOF\n$(rm CLAUDE.md)\nEOF",
    "find .claude -exec tee -a {} +",
    # tee as an argument of a read-only program: only the tee check sees it.
    "cat .claude/settings.json tee",
    # Formerly measured bypasses (ddc2b89), closed in 4460840.
    # A shell separator right after a bare directory name.
    "cd .github;rm workflows/ci.yml",
    "cd .github&&rm -r workflows",
    "cd .claude||rm settings.json",
    "(cd .github)",
    # Programs that can write or run other programs, no longer on the read-only list.
    "find .github -delete",
    "find .claude -name '*.py' -exec rm {} +",
    "awk 'BEGIN{system(\"rm x\")}' .claude/settings.json",
    "sort -o CLAUDE.md CLAUDE.md",
    # git's --output writes a file, whatever the subcommand.
    "git diff --output=CLAUDE.md",
    "git log --output=.claude/settings.json",
    "git diff --output CLAUDE.md",
    # The path after = or ~/ is still a path (~/.claude holds the user settings).
    "rm --x=.claude/a",
    "rm ~/.claude/settings.json",
    # The default macOS file system ignores case, so other spellings are the same file.
    "sed -i '' 's/never/always/' Claude.MD",
    "rm claude.md",
    "rm -rf .CLAUDE/hooks",
    "rm .GitHub/workflows/ci.yml",
    # The thirteen bypasses measured in the PR #40 review (strict xfail in 45dc830),
    # closed in 71ab9b4.
    # Separators the segment split did not see: newline, carriage return, single &.
    "cat CLAUDE.md\nrm CLAUDE.md",
    "cat CLAUDE.md\rrm CLAUDE.md",
    "cat CLAUDE.md & rm CLAUDE.md",
    # Substitutions run whatever is inside them.
    "cat $(rm CLAUDE.md)",
    "cat `rm CLAUDE.md`",
    "cat <(sed -i '' 's/never/always/' CLAUDE.md)",
    # Programs that write (uniq IN OUT) or run a program through a flag.
    "uniq /tmp/s.json .claude/settings.json",
    "rg --pre rm x CLAUDE.md",
    "git grep -Orm -e . -- CLAUDE.md",
    "git grep --open-files-in-pager=rm -e . -- CLAUDE.md",
    # A program named with a path is any program.
    "./cat CLAUDE.md",
    "scratch/bin/cat CLAUDE.md",
    # A letter right before the dot no longer hides the name (GNU cp -t DIR).
    "cp /tmp/x -t.claude/hooks",
    # The same flags in other spellings: -O alone, inside a cluster, rg's --pre=.
    "git grep -O -e x -- CLAUDE.md",
    "git grep -inOrm -e x -- CLAUDE.md",
    "rg --pre=rm x CLAUDE.md",
    "rg --pre-glob '*' --pre rm x CLAUDE.md",
    # A single & right after a name, and a newline at the start of a segment.
    "ls .github&rm -r .github",
    "ls .claude\n  rm -r .claude",
]

# Commands that cannot write a guardrail file but are blocked anyway: the checks
# look at the text, not at what the shell does with it. Pinned so that a change
# here is a decision, not an accident.
BLOCKED_READS = [
    "cat .claude/settings.json 2>/dev/null",
    "grep '=>' .github/workflows/ci.yml",
    "grep tee .github/workflows/ci.yml",
    "git diff --output-indicator-new=+ -- CLAUDE.md",
    "(cat CLAUDE.md)",
    # Any << blocks, a here-string too (only the redirect check sees it).
    "cat <<< CLAUDE.md",
    # Nothing may hide the dot before .claude or .github (71ab9b4), so a name that
    # only ends in one counts as one.
    "rm foo.claude",
]

UNRELATED = [
    "rm -rf build",
    "sed -i '' 's/a/b/' app/main.py",
    "echo hello > notes.txt",
    "echo x | tee out.log",
    "cp a.txt b.txt",
    "git checkout -- app/main.py",
    "git add app/main.py tests/test_main.py",
    "python3 - <<'EOF'\nprint(1)\nEOF",
    "cd docs && rm -rf old",
    "uv run pytest -q tests/guardrails",
    # A guardrail name only counts as its own path segment.
    "rm -rf .claude-old .github_bak",
    "rm docs/oldCLAUDE.md",
    "rm .claudex .github2",
]

# One command per check that only that check blocks, with the part of its message
# that names it.
SINGLE_CHECK = [
    ("echo x > CLAUDE.md", "and redirects"),
    ("cat .claude/settings.json tee", "and uses tee"),
    ("git diff --output=CLAUDE.md", "with a flag that writes or runs a program"),
    ("cat $(echo CLAUDE.md)", "and substitutes a command"),
    ("rm CLAUDE.md", "with a program that may write (rm)"),
    ("./cat CLAUDE.md", "with a program that may write (./cat)"),
]


def run_payload(
    payload: str,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    isolated: bool = True,
) -> subprocess.CompletedProcess:
    flags = ["-I"] if isolated else []
    return subprocess.run(
        [sys.executable, *flags, str(HOOK)],
        input=payload,
        text=True,
        capture_output=True,
        cwd=cwd,
        env=env,
        check=False,
    )


def bash(command: object) -> str:
    return json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})


def run(command: str) -> subprocess.CompletedProcess:
    return run_payload(bash(command))


@pytest.mark.parametrize("command", READ_ONLY)
def test_read_only_commands_pass(command: str) -> None:
    result = run(command)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("program", READ_ONLY_PROGRAMS)
def test_each_read_only_program_passes(program: str) -> None:
    result = run(f"{program} CLAUDE.md")
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("subcommand", READ_ONLY_GIT)
def test_each_read_only_git_subcommand_passes(subcommand: str) -> None:
    result = run(f"git {subcommand} -- .claude")
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("command", WRITES)
def test_writes_are_blocked_with_the_rule_11_message(command: str) -> None:
    result = run(command)
    assert result.returncode == 2
    assert RULE_11 in result.stderr


@pytest.mark.parametrize("command", BLOCKED_READS)
def test_some_reads_are_blocked_as_the_cost_of_a_text_check(command: str) -> None:
    assert run(command).returncode == 2


@pytest.mark.parametrize(("command", "check"), SINGLE_CHECK)
def test_each_check_blocks_on_its_own(command: str, check: str) -> None:
    result = run(command)
    assert result.returncode == 2
    assert check in result.stderr


@pytest.mark.parametrize("command", UNRELATED)
def test_commands_naming_no_guardrail_path_pass(command: str) -> None:
    result = run(command)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "payload",
    [
        "not json",
        "",
        "{}",
        json.dumps({"tool_name": "Bash", "tool_input": {}}),
        json.dumps({"tool_name": "Bash", "tool_input": "rm CLAUDE.md"}),
    ],
)
def test_unreadable_input_is_blocked(payload: str) -> None:
    result = run_payload(payload)
    assert result.returncode == 2
    assert "could not be read" in result.stderr


@pytest.mark.parametrize("command", [["rm", "CLAUDE.md"], None, 0, {"command": "ls"}])
def test_non_string_command_is_blocked(command: object) -> None:
    result = run_payload(bash(command))
    assert result.returncode == 2
    assert "unexpected tool input" in result.stderr


def test_planted_json_module_is_ignored_with_dash_i(tmp_path: Path) -> None:
    # `python script.py` never puts the working directory on sys.path, so a json.py
    # there proves nothing; PYTHONPATH is what -I ignores and a plain run honours.
    (tmp_path / "json.py").write_text(
        "def load(f):\n    return {'tool_input': {'command': 'ls'}}\n"
    )
    env = dict(os.environ, PYTHONPATH=str(tmp_path))
    payload = bash("rm CLAUDE.md")
    # Non-vacuity: without -I the planted module is imported and the write passes.
    assert run_payload(payload, cwd=tmp_path, env=env, isolated=False).returncode == 0
    assert run_payload(payload, cwd=tmp_path, env=env).returncode == 2
