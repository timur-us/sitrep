"""No secret anywhere in the repository (definition of done, every PR).

Every file git sees is scanned, prose and .claude/ included. The planted
samples are assembled at runtime, so this file carries no secret either.
"""

from __future__ import annotations

import re

from repo_files import ROOT, list_files, read_text
from secret_patterns import ALLOWLIST, PATTERNS, find_secrets

HOOK_TESTS = "tests/guardrails/test_block_secrets.py"


def _join(*pieces: str) -> str:
    return "".join(pieces)


def _pairs(text: str) -> list[tuple]:
    """(line, pattern) of each finding."""
    return [tuple(hit)[:2] for hit in find_secrets(text)]


# One planted positive per pattern. None of them is a live credential.
PLANTED = {
    "anthropic-key": _join("sk", "-ant-", "api03-", "A" * 24),
    "aws-access-key-id": _join("AK", "IA", "Z" * 16),
    "github-token": _join("gh", "p_", "a" * 36),
    "github-fine-grained-token": _join("github", "_pat_", "b" * 40),
    "private-key-block": _join("-----BEGIN ", "OPENSSH ", "PRIVATE", " KEY-----"),
    "azure-account-key": _join("AccountName=x;Account", "Key=", "c" * 86, "=="),
    "url-credentials": _join("postgresql://app:", "hunter2", "@db.example/x"),
    "jwt": _join("ey", "JhbGciOiJIUzI1NiJ9.", "ey", "JzdWIiOiJ4In0.", "c2lnbmF0dXJlLXN0dWI"),
}

# Further shapes each pattern must catch: other lengths and headers, and real
# passwords that sit close to the documentation placeholders.
MORE_POSITIVES = [
    ("github-token", _join("gh", "s_", "d" * 40)),
    ("private-key-block", _join("-----BEGIN ", "PRIVATE", " KEY-----")),
    ("private-key-block", _join("-----BEGIN ", "RSA ", "PRIVATE", " KEY-----")),
    ("private-key-block", _join("-----BEGIN ", "PGP ", "PRIVATE", " KEY BLOCK-----")),
    ("url-credentials", _join("postgresql://app:", "password123", "@h/db")),
    ("url-credentials", _join("postgresql://app:", "pwd1", "@h/db")),
    ("url-credentials", _join("postgresql://app:", "$Tr0ngPw", "@h/db")),
    ("url-credentials", _join("https://u:", "{Tr0ng}", "@h/x")),
    ("url-credentials", _join("redis://:", "s3cretpw", "@127.0.0.1:6379")),
]

# Look like the patterns but carry no secret: prefixes in prose, URLs without a
# password, documentation placeholders.
LOOKALIKES = [
    _join("the ", "sk", "-ant-", " prefix"),
    _join("AK", "IA", "…"),
    _join("ey", "Jhb is how a JWT header starts"),
    "postgresql://app@127.0.0.1:5432/db",
    "https://example.com:8443/path",
    "scheme://user:pass@",
    "postgresql://app:pwd@h/db",
    "postgresql://{user}:{password}@{host}/db",
    "postgresql://app:${DB_PASSWORD}@h/db",
    "postgresql://app:$DB_PASSWORD@h/db",
    "postgresql://app:<password>@h/db",
    "postgresql://app:*****@h/db",
]


def test_planted_positives_are_caught() -> None:
    assert set(PATTERNS) == set(PLANTED), "every pattern needs exactly one planted positive"
    for name, sample in PLANTED.items():
        assert _pairs(sample) == [(1, name)], f"planted {name} not caught"
    for name, sample in MORE_POSITIVES:
        assert _pairs(sample) == [(1, name)], f"{name}: {sample!r} not caught"


def test_lookalikes_are_not_reported() -> None:
    assert PATTERNS, "no secret patterns"
    for text in LOOKALIKES:
        assert find_secrets(text) == [], f"false positive on {text!r}"


def test_findings_carry_no_text() -> None:
    for name, sample in PLANTED.items():
        hits = find_secrets(sample)
        assert hits, f"planted {name} not caught"
        for hit in map(tuple, hits):
            assert len(hit) == 3, f"{name}: a finding is (line, pattern, fingerprint), got {hit}"
            assert re.fullmatch(r"[0-9a-f]{16}", hit[2]), f"{name}: fingerprint {hit[2]!r}"
            assert sample not in repr(hit), f"{name}: the finding carries the matched text"


def test_allowlist_is_exact() -> None:
    assert set(ALLOWLIST) == {HOOK_TESTS}
    for rel, entry in ALLOWLIST.items():
        assert getattr(entry, "reason", "").strip(), f"{rel}: no reason"
        assert getattr(entry, "fingerprints", None), (
            f"{rel}: the allowlist must pin exact (pattern, fingerprint) pairs, not a whole file"
        )


def test_no_secrets() -> None:
    files = list_files(ROOT)
    assert len(files) >= 30, f"only {len(files)} files listed"
    for required in (".env.example", ".claude/settings.json", "docs/plans/m0-foundation.md"):
        assert required in files, f"{required} was not scanned"

    hits = {}
    for rel in files:
        text = read_text(ROOT, rel)
        if text and (found := find_secrets(text)):
            hits[rel] = [tuple(hit) for hit in found]
    # Non-vacuity: the hook tests carry a fake key, and the scan must see it.
    assert HOOK_TESTS in hits, f"the scan misses the fake key in {HOOK_TESTS}"

    leaks, stale = [], []
    for rel in sorted(hits.keys() | ALLOWLIST.keys()):
        allowed = getattr(ALLOWLIST.get(rel), "fingerprints", frozenset())
        found = hits.get(rel, [])
        leaks += [f"{rel}:{hit[0]}: {' '.join(hit[1:])}" for hit in found if hit[1:] not in allowed]
        stale += [f"{rel}: {pair}" for pair in allowed - {hit[1:] for hit in found}]
    assert not leaks, "possible secrets (text not shown):\n" + "\n".join(leaks)
    assert not stale, "allowlist entries that match nothing; remove them:\n" + "\n".join(stale)
