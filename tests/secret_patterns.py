"""Secret patterns for the repo-wide scan in tests/test_no_secrets.py.

Each pattern is assembled at runtime from split pieces, so this file matches
none of them and push protection does not flag it. A finding carries the line,
the pattern name and a fingerprint (the first 16 hex digits of the match's
sha256), never the matched text: a real secret must not reach a log. The
fingerprint only matches findings against the allowlist; report() never prints
it, because the fingerprint of a URL password can be reversed with a dictionary.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import NamedTuple


def _join(*pieces: str) -> str:
    return "".join(pieces)


PATTERNS: dict[str, re.Pattern[str]] = {
    "anthropic-key": re.compile(_join("sk", "-ant-") + r"[A-Za-z0-9_-]{8,}"),
    "aws-access-key-id": re.compile(r"\b" + _join("AK", "IA") + r"[0-9A-Z]{16}\b"),
    # Classic tokens: personal (p), OAuth (o), user-to-server (u), server (s), refresh (r).
    "github-token": re.compile(r"\b" + _join("gh", "[pousr]_") + r"[A-Za-z0-9]{36,}"),
    "github-fine-grained-token": re.compile(r"\b" + _join("github", "_pat_") + r"\w{22,}"),
    # RSA, EC, OPENSSH, ENCRYPTED, PGP ... or none.
    "private-key-block": re.compile(_join("-----BEGIN ", "(?:[A-Z0-9]+ )*", "PRIVATE", " KEY")),
    "azure-account-key": re.compile(_join("Account", "Key=") + r"[A-Za-z0-9+/]{20,}"),
    # The user may be empty (redis://:password@host).
    "url-credentials": re.compile(
        r"\b[a-z][a-z0-9+.-]*://[^\s/:@]*:(?P<password>[^\s/@]+)@", re.IGNORECASE
    ),
    # Header and payload are base64url JSON objects, so both start with "eyJ". No
    # leading \b: URL-encoded forms put a word character in front ("Bearer%20eyJ").
    "jwt": re.compile(
        _join("ey", "J") + r"[A-Za-z0-9_-]{8,}\." + _join("ey", "J") + r"[A-Za-z0-9_-]{8,}\."
    ),
}

# Documentation placeholders for the password in URL credentials. Anything else,
# however weak, is reported.
PLACEHOLDER_PASSWORD = re.compile(
    r"(?i:pass(?:word)?|pwd?)"  # scheme://user:pass@
    r"|\*+"  # redacted
    r"|\$\{?[A-Z_][A-Z0-9_]*\}?"  # an environment variable: $DB_PASSWORD, ${DB_PASSWORD}
    r"|\{[a-z_]+\}"  # a template field: {password}
    r"|<[a-z_ -]+>"  # <password>
)


class Hit(NamedTuple):
    line: int
    pattern: str
    fingerprint: str


@dataclass(frozen=True)
class Allowed:
    reason: str
    fingerprints: frozenset[tuple[str, str]]  # (pattern, fingerprint) pairs


# Exact matches that may stay, by file. Any other match in the same file is reported.
ALLOWLIST: dict[str, Allowed] = {
    "tests/guardrails/test_block_secrets.py": Allowed(
        reason="commands the secret hook must block, including a fake Anthropic key",
        fingerprints=frozenset({("anthropic-key", "76441baab4079dc5")}),
    ),
}


def fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def find_secrets(text: str) -> list[Hit]:
    """One hit per distinct match: a second key on a line is its own finding."""
    found: dict[Hit, None] = {}
    for number, line in enumerate(text.splitlines(), start=1):
        for name, pattern in PATTERNS.items():
            for match in pattern.finditer(line):
                password = match.groupdict().get("password")
                if password is not None and PLACEHOLDER_PASSWORD.fullmatch(password):
                    continue
                found[Hit(number, name, fingerprint(match.group(0)))] = None
    return list(found)


def report(hits: dict[str, list[Hit]]) -> tuple[list[str], list[str]]:
    """Leaks as "<file>:<line>: <pattern>" and stale allowlist entries as
    "<file>: <pattern>". No fingerprint is printed."""
    leaks: list[str] = []
    stale: list[str] = []
    for rel in sorted(hits.keys() | ALLOWLIST.keys()):
        allowed = ALLOWLIST[rel].fingerprints if rel in ALLOWLIST else frozenset()
        found = hits.get(rel, [])
        seen = {(hit.pattern, hit.fingerprint) for hit in found}
        leaks += [
            f"{rel}:{hit.line}: {hit.pattern}"
            for hit in found
            if (hit.pattern, hit.fingerprint) not in allowed
        ]
        stale += [f"{rel}: {pattern}" for pattern, _ in sorted(allowed - seen)]
    return leaks, stale
