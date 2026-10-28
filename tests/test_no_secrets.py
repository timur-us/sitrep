"""No secret anywhere in the repository (definition of done, every PR).

Every file git sees is scanned, prose and .claude/ included. The planted
samples are assembled at runtime, so this file carries no secret either.
"""

from __future__ import annotations

from repo_files import ROOT, list_files, read_text
from secret_patterns import ALLOWLIST, PATTERNS, find_secrets

HOOK_TESTS = "tests/guardrails/test_block_secrets.py"


def _join(*pieces: str) -> str:
    return "".join(pieces)


# One planted positive per pattern. None of them is a live credential.
PLANTED = {
    "anthropic-key": _join("sk", "-ant-", "api03-", "A" * 24),
    "aws-access-key-id": _join("AK", "IA", "Z" * 16),
    "github-token": _join("gh", "p_", "a" * 36),
    "github-fine-grained-token": _join("github", "_pat_", "b" * 40),
    "private-key-block": _join("-----BEGIN ", "OPENSSH ", "PRIVATE", " KEY-----"),
    "azure-account-key": _join("AccountName=x;Account", "Key=", "c" * 86, "=="),
    "url-credentials": _join("postgresql://app:", "hunter2", "@db.example/x"),
}

# Look like the patterns but carry no secret: prefixes in prose, URLs without a
# password, documentation placeholders.
LOOKALIKES = [
    _join("the ", "sk", "-ant-", " prefix"),
    _join("AK", "IA", "…"),
    "postgresql://app@127.0.0.1:5432/db",
    "scheme://user:pass@",
    "postgresql://{user}:{password}@{host}/db",
    "https://example.com:8443/path",
]


def test_planted_positives_are_caught() -> None:
    assert set(PATTERNS) == set(PLANTED), "every pattern needs exactly one planted positive"
    for name, sample in PLANTED.items():
        assert find_secrets(sample) == [(1, name)], f"planted {name} not caught"


def test_lookalikes_are_not_reported() -> None:
    assert PATTERNS, "no secret patterns"
    for text in LOOKALIKES:
        assert find_secrets(text) == [], f"false positive on {text!r}"


def test_allowlist_is_exact() -> None:
    assert set(ALLOWLIST) == {HOOK_TESTS}
    assert all(reason.strip() for reason in ALLOWLIST.values()), "an allowlist entry has no reason"


def test_no_secrets() -> None:
    files = list_files(ROOT)
    assert len(files) >= 30, f"only {len(files)} files listed"
    for required in (".env.example", ".claude/settings.json", "docs/plans/m0-foundation.md"):
        assert required in files, f"{required} was not scanned"

    hits = {}
    for rel in files:
        text = read_text(ROOT, rel)
        if text and (found := find_secrets(text)):
            hits[rel] = found
    # Non-vacuity: the hook tests carry a fake key, and the scan must see it.
    assert HOOK_TESTS in hits, f"the scan misses the fake key in {HOOK_TESTS}"
    for rel in ALLOWLIST:
        assert rel in hits, f"allowlist entry {rel} matches nothing; remove it"
    leaks = [
        f"{rel}:{line}: {name}"
        for rel, found in hits.items()
        if rel not in ALLOWLIST
        for line, name in found
    ]
    assert not leaks, "possible secrets (text not shown):\n" + "\n".join(leaks)
