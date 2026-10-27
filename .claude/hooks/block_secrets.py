"""PreToolUse hook for Bash: blocks commands that would print or carry a secret.

Claude Code passes the tool call as JSON on stdin. Exit code 2 blocks the call
and shows the message on stderr to the agent; exit code 0 lets it through.

Fail closed: if the input cannot be read, the call is blocked, and settings.json
sets "onFailure": "block" so that a crash, a timeout or a missing interpreter
blocks as well. The hook is started with `python3 -I`, so nothing from the
working directory (for example a planted json.py) is imported.

This is a text heuristic (layer 2). It stops accidents, not a determined
attacker; the server-side controls (layer 3) do not depend on it. Tests:
tests/guardrails/test_block_secrets.py.
"""

import json
import re
import sys

# A literal Anthropic key anywhere in the command.
ANTHROPIC_KEY = re.compile(r"sk-ant-[A-Za-z0-9_-]{8,}")

# Setting the key to a value, or expanding it. `ANTHROPIC_API_KEY= make test`
# (empty, to force an offline run) stays allowed.
KEY_VARIABLE = re.compile(
    r"ANTHROPIC_API_KEY=(?![\s;&|)]|$|\"\"|'')|\$\{?ANTHROPIC_API_KEY|printenv\s+ANTHROPIC_API_KEY"
)

# A dotenv file named as its own path segment: .env, .env.local, app/.env, ".env"
# and so on. .env.example is the only one allowed. Case-insensitive, because the
# default macOS file system is.
DOTENV = re.compile(r"(?<![A-Za-z0-9_.-])\.env(?:\.[A-Za-z0-9_-]+)*(?![A-Za-z0-9_])", re.IGNORECASE)

# Commands that print secret values from Azure or GitHub.
SHOW_SECRET = re.compile(r"--show-values|secret\s+show|secrets?\s+list[^|]*--show")


def block(reason: str) -> None:
    print(f"Blocked by .claude/hooks/block_secrets.py: {reason}", file=sys.stderr)
    sys.exit(2)


def main() -> None:
    try:
        command = json.load(sys.stdin)["tool_input"]["command"]
    except Exception:
        block("the tool call could not be read, so it is blocked (fail closed).")
    if not isinstance(command, str):
        block("unexpected tool input, so it is blocked (fail closed).")

    if ANTHROPIC_KEY.search(command):
        block("the command contains what looks like an Anthropic API key.")
    if KEY_VARIABLE.search(command):
        block("the command sets or reads ANTHROPIC_API_KEY. The agent never handles the key.")
    for match in DOTENV.finditer(command):
        if match.group(0).lower() != ".env.example":
            block(
                f"the command names {match.group(0)}. Dotenv files are read by the app, "
                "never by the shell. Use .env.example for the variable names."
            )
    if SHOW_SECRET.search(command):
        block("this command would print secret values.")
    sys.exit(0)


if __name__ == "__main__":
    main()
