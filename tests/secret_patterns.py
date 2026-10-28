"""Secret patterns for the repo-wide scan in tests/test_no_secrets.py.

Each pattern is assembled at runtime from split pieces, so this file matches
none of them and push protection does not flag it. Findings carry the line and
the pattern name, never the matched text: a real secret must not reach a log.
"""

from __future__ import annotations

import re


def _join(*pieces: str) -> str:
    return "".join(pieces)


PATTERNS: dict[str, re.Pattern[str]] = {
    "anthropic-key": re.compile(_join("sk", "-ant-") + r"[A-Za-z0-9_-]{8,}"),
    "aws-access-key-id": re.compile(r"\b" + _join("AK", "IA") + r"[0-9A-Z]{16}\b"),
    # Classic tokens: personal (p), OAuth (o), user-to-server (u), server (s), refresh (r).
    "github-token": re.compile(r"\b" + _join("gh", "[pousr]_") + r"[A-Za-z0-9]{36}\b"),
    "github-fine-grained-token": re.compile(r"\b" + _join("github", "_pat_") + r"\w{22,}"),
    # RSA, EC, OPENSSH, ENCRYPTED, PGP ... or none.
    "private-key-block": re.compile(_join("-----BEGIN ", "(?:[A-Z0-9]+ )*", "PRIVATE", " KEY")),
    "azure-account-key": re.compile(_join("Account", "Key=") + r"[A-Za-z0-9+/]{20,}"),
    "url-credentials": re.compile(
        r"\b[a-z][a-z0-9+.-]*://[^\s/:@]+:(?P<password>[^\s/@]+)@", re.IGNORECASE
    ),
}

# Files that may match, each with the reason.
ALLOWLIST: dict[str, str] = {
    "tests/guardrails/test_block_secrets.py": (
        "commands the secret hook must block, including a fake Anthropic key"
    ),
}

# Documentation placeholders in URL credentials: scheme://user:pass@, {password}, $PASSWORD.
PLACEHOLDER_PASSWORD = re.compile(
    r"pass(?:word)?|pw|pwd|x+|\*+|<[^>]*>|\{[^}]*\}|\$\{?\w+\}?", re.IGNORECASE
)


def find_secrets(text: str) -> list[tuple[int, str]]:
    """(line number, pattern name) for each pattern that matches a line."""
    found = []
    for number, line in enumerate(text.splitlines(), start=1):
        for name, pattern in PATTERNS.items():
            for match in pattern.finditer(line):
                password = match.groupdict().get("password")
                if password is not None and PLACEHOLDER_PASSWORD.fullmatch(password):
                    continue
                found.append((number, name))
                break
    return found
