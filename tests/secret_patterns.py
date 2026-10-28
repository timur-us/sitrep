"""Secret patterns for the repo-wide scan in tests/test_no_secrets.py.

Each pattern is assembled at runtime from split pieces, so this file matches
none of them and push protection does not flag it. Findings carry the line and
the pattern name, never the matched text: a real secret must not reach a log.
"""

from __future__ import annotations

import re

PATTERNS: dict[str, re.Pattern[str]] = {}

# Files that may match, each with the reason.
ALLOWLIST: dict[str, str] = {}

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
