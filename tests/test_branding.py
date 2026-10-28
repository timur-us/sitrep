"""Branding is complete, matches the facts in CLAUDE.md, and appears nowhere else.

This file names no branding string itself: every expected value is built from
app.config.branding, and the name-level facts are checked against CLAUDE.md.
"""

from __future__ import annotations

import subprocess
from dataclasses import fields
from pathlib import Path

import branding_scan
from repo_files import ROOT

from app.config import branding
from app.config.branding import Role

# How CLAUDE.md describes each non-advisor role.
ROLE_PHRASES = {
    Role.ATTACKER: "{} is the active attacker",
    Role.PASSIVE_READER: "{} is the passive reader",
    Role.MANY_IDENTITIES: "{} has many identities",
    Role.TRUSTED_PARTY: "{} is the trusted party",
}


def test_branding_values() -> None:
    records = [branding.PRODUCT, branding.BANK, branding.DOMAINS, branding.DEMO_MANDATE]
    for record in [*records, *branding.CAST]:
        for field in fields(record):
            assert getattr(record, field.name), (
                f"branding: {type(record).__name__}.{field.name} is empty"
            )
    assert branding.SLUG == branding.PRODUCT.slug

    domains = branding.DOMAINS
    for domain in (domains.staff, domains.attacker, domains.canary):
        assert domain == domain.lower(), f"domain {domain!r} is not lowercase"
        assert domain.split(".")[-1] == "example", f"domain {domain!r} is not under .example"

    assert {person.role for person in branding.CAST} == set(Role), "a role has no cast member"
    ids = [person.id for person in branding.CAST]
    assert len(set(ids)) == len(ids), f"duplicate cast ids: {ids}"
    assert len(branding.ADVISORS) == 3, f"expected three advisors, got {len(branding.ADVISORS)}"
    for advisor in branding.ADVISORS:
        assert advisor.email.rpartition("@")[2] == domains.staff, (
            f"advisor e-mail {advisor.email!r} is not under the staff domain"
        )
    for person in branding.CAST:
        assert person.email.rpartition("@")[2] in {domains.staff, domains.attacker}, (
            f"cast e-mail {person.email!r} is under no cast domain"
        )

    owner = [person for person in branding.CAST if person.id == branding.DEMO_MANDATE.advisor_id]
    assert len(owner) == 1, "the demo mandate's advisor is not in the cast"
    assert owner[0].role is Role.ADVISOR, "the demo mandate's advisor is not an advisor"


def test_branding_matches_claude_md() -> None:
    """The names in branding are the ones CLAUDE.md documents (Alice owns the demo mandate)."""
    text = (ROOT / "CLAUDE.md").read_text()
    product, bank, domains = branding.PRODUCT, branding.BANK, branding.DOMAINS
    owners = [p.name for p in branding.CAST if p.id == branding.DEMO_MANDATE.advisor_id]
    owner = owners[0] if owners else "<no advisor>"
    advisors = ", ".join(advisor.name for advisor in branding.ADVISORS)
    expected = [
        f"**{product.name}** in prose and UI, `{product.slug}` in code",
        f'"{product.name} ({product.expansion})"',
        f"Tagline: *{product.tagline}*",
        f"**{bank.legal_name}** (legal name), **{bank.display_name}** (UI, English)",
        (
            f"staff and clients under `{domains.staff}`; the attacker under `{domains.attacker}`; "
            f"the canary under `{domains.canary}`"
        ),
        f"{advisors} are advisors with their own mandates",
        f"Demo mandate: the {branding.DEMO_MANDATE.family} family, advised by {owner}.",
    ]
    for role, phrase in ROLE_PHRASES.items():
        expected += [phrase.format(p.name) for p in branding.CAST if p.role is role]
    missing = [sentence for sentence in expected if sentence not in text]
    assert not missing, f"CLAUDE.md does not say: {missing}"
    names = bank.legal_name.split() + bank.display_name.split()
    for stem in bank.name_stems:
        assert stem in names, f"bank stem {stem!r} is not a word of the bank's names"


def _git_repo(path: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True)
    return path


def test_scanner_planted(tmp_path: Path) -> None:
    assert branding.ADVISORS and branding.BANK.name_stems, "branding is empty"
    root = _git_repo(tmp_path)
    planted = [
        (1, "display name", branding.BANK.display_name),
        (2, "bank stem", f"{branding.BANK.name_stems[0]} cipher"),
        (3, "cast", f"call {branding.ADVISORS[0].name}"),
        (4, "slug", f"import {branding.SLUG}"),
    ]
    body = "".join(f"x = {value!r}\n" for _, _, value in planted)
    for rel in ("app/planted.py", "docs/x.md", "evals/recorded/run.json", "Makefile"):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(body)

    scanned, findings = branding_scan.scan(root)
    assert set(scanned) == {"app/planted.py", "Makefile"}, f"scanned: {scanned}"
    reported = {(f.path, f.line, f.rule) for f in findings}

    # In code, each planted line is reported under its own rule.
    for line, rule, _ in planted:
        assert ("app/planted.py", line, rule) in reported, f"line {line} ({rule}) not reported"

    # The Makefile may carry the slug, and nothing else.
    for line, rule, _ in planted[:3]:
        assert ("Makefile", line, rule) in reported, f"Makefile line {line} ({rule}) not reported"
    assert ("Makefile", 4, "slug") not in reported, "slug reported in the Makefile"

    assert set(branding_scan.EXCLUDED) == {
        "docs/**/*.md",
        "README.md",
        "CLAUDE.md",
        "redteam/SCOPE.md",
        ".claude/**",
        "**/recorded/**",
        "app/config/branding.py",
    }
    assert set(branding_scan.SLUG_ALLOWED) == {
        "pyproject.toml",
        "uv.lock",
        "compose.yaml",
        "Dockerfile",
        "Makefile",
        ".env.example",
        "db/**/*.sql",
        ".github/**",
    }


def test_branding_only_in_branding_py() -> None:
    names = {rule.name for rule in branding_scan.rules()}
    assert names == set(branding_scan.RULE_NAMES), (
        f"scanner has no rule for: {set(branding_scan.RULE_NAMES) - names}"
    )
    scanned, findings = branding_scan.scan(ROOT)
    assert len(scanned) >= 15, f"only {len(scanned)} files scanned"
    for required in ("app/__init__.py", "Makefile", "tests/test_branding.py", "pyproject.toml"):
        assert required in scanned, f"{required} was not scanned"
    assert "app/config/branding.py" not in scanned
    assert not findings, "branding strings outside app/config/branding.py:\n" + "\n".join(
        map(str, findings)
    )
