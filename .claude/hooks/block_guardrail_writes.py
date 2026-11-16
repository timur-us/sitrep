"""PreToolUse hook for Bash: blocks commands that could write a guardrail file.

The Edit and Write rules in settings.json keep the agent out of CLAUDE.md, .claude/
and .github/ (rule 11), but a shell can write them too: `sed -i`, `tee`, a redirect,
`cp`, `git checkout --`, a Python one-liner. This hook closes that side (the M0
report, item 6; decided by Timur before M1 PR 17, when the first attack text enters
the repository).

Claude Code passes the tool call as JSON on stdin. Exit code 2 blocks the call and
shows the message on stderr to the agent; exit code 0 lets it through. Fail closed:
if the input cannot be read, the call is blocked, and settings.json sets
"onFailure": "block" so that a crash, a timeout or a missing interpreter blocks as
well. Started with `python3 -I`, so nothing from the working directory is imported.

The rule: a command that names a guardrail path passes only if every segment of it
(split on |, ;, &, &&, ||, newline and carriage return) starts with a read-only program
named without a path, and the command holds no redirect (>, >>, <<), no `tee`, no
substitution ($(…), backticks, <(…), >(…)) and no flag that writes or runs a program.
Anything else that names such a path is blocked, whatever it meant. This is a text
heuristic (layer 2): a glob (`.c?aude`), a quote inside a name (`.git'hub'`), a
variable (`d=.github; rm -r $d`) or a command that names no path (`git checkout
<rev> -- .`) passes. The GitHub ruleset and the human merge (layer 3) do not depend on
it. Tests: tests/guardrails/test_block_guardrail_writes.py.
"""

import json
import re
import sys

# A guardrail path named anywhere in the command, as its own path segment.
# Case-insensitive, because the default macOS file system is.
# For the two dot-names nothing may hide the dot: `cp -t.claude` names .claude, and so
# does `foo.claude` (a false positive, the cost of a text check).
GUARDRAIL_PATH = re.compile(
    r"(?:(?<![A-Za-z0-9_.-])CLAUDE\.md|\.(?:claude|github))(?![A-Za-z0-9_-])",
    re.IGNORECASE,
)

# Programs that only read. A segment must start with one of these, plain (no `env`,
# `command`, variable assignment or path in front: `./cat` is any program). Not on the
# list, because they can write or run other programs: find (-delete, -exec), awk
# (system()), sort (-o), uniq (IN OUT), xargs, sed, perl, python. `git` is read-only
# only with the subcommands listed, and never with --output or -O (git grep opens a
# pager program); rg never with --pre. echo and printf stay: they write only through a
# redirect, which is blocked.
READ_ONLY = (
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
    "diff",
    "file",
    "stat",
    "cut",
    "tr",
    "jq",
    "md5",
    "shasum",
    "sha256sum",
    "test",
    "[",
    "echo",
    "printf",
)
GIT_READ_ONLY = (
    "diff",
    "show",
    "log",
    "status",
    "blame",
    "ls-files",
    "grep",
    "cat-file",
    "rev-parse",
)

REDIRECT = re.compile(r">|<<")
# Flags that make a read-only program write or run another: git --output, git grep
# -O / --open-files-in-pager (also inside a cluster such as -Orm), rg --pre.
RUN_FLAG = re.compile(
    r"(?<![\w-])(?:--output|--open-files-in-pager|--pre|-[A-Za-z]*O[A-Za-z]*)(?![\w])"
)
# A substitution runs whatever is inside it, and the segment split does not see it.
SUBSTITUTION = re.compile(r"\$\(|`|<\(|>\(")
# Segments: a pipe, ;, &&, ||, a single & (background), a newline or carriage return.
SEGMENT = re.compile(r"\|\||&&|[|;&\n\r]")
FIRST_WORD = re.compile(r"^\s*([^\s]+)(?:\s+([^\s]+))?")


def block(reason: str) -> None:
    print(f"Blocked by .claude/hooks/block_guardrail_writes.py: {reason}", file=sys.stderr)
    sys.exit(2)


def read_only(segment: str) -> bool:
    """Whether segment starts with a read-only program."""
    found = FIRST_WORD.match(segment)
    if found is None:
        return True  # an empty segment: nothing runs
    program, second = found.group(1), found.group(2)
    if "/" in program:
        return False  # a path names any program, whatever it is called
    if program == "git":
        return second in GIT_READ_ONLY
    return program in READ_ONLY


def main() -> None:
    try:
        command = json.load(sys.stdin)["tool_input"]["command"]
    except Exception:
        block("the tool call could not be read, so it is blocked (fail closed).")
    if not isinstance(command, str):
        block("unexpected tool input, so it is blocked (fail closed).")

    named = GUARDRAIL_PATH.search(command)
    if named is None:
        sys.exit(0)
    where = named.group(0)
    if REDIRECT.search(command):
        block(
            f"the command names {where} and redirects. Guardrail files belong to Timur (rule 11)."
        )
    if re.search(r"(?<![\w-])tee(?![\w-])", command):
        block(f"the command names {where} and uses tee. Guardrail files belong to Timur (rule 11).")
    if RUN_FLAG.search(command):
        block(
            f"the command names {where} with a flag that writes or runs a program. Guardrail "
            "files belong to Timur (rule 11)."
        )
    if SUBSTITUTION.search(command):
        block(
            f"the command names {where} and substitutes a command. Guardrail files belong to "
            "Timur (rule 11)."
        )
    for segment in SEGMENT.split(command):
        if not read_only(segment):
            block(
                f"the command names {where} with a program that may write "
                f"({segment.strip().split(' ')[0] or 'unknown'}). Guardrail files belong to "
                "Timur (rule 11); read them with cat, grep, git diff or git show."
            )
    sys.exit(0)


if __name__ == "__main__":
    main()
