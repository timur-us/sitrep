"""Branding is complete, matches the facts in CLAUDE.md, and appears nowhere else.

This file names no branding string itself: every expected value is built from
app.config.branding, and the name-level facts are checked against CLAUDE.md.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError, fields
from pathlib import Path

import branding_scan
import pytest
import repo_files
from branding_scan import EXCLUDED, SLUG_ALLOWED, matches_any
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

# Staff and the trusted party are under the staff domain; every adversary is
# under the attacker domain.
STAFF_ROLES = {Role.ADVISOR, Role.TRUSTED_PARTY}


def test_branding_values() -> None:
    records = [branding.PRODUCT, branding.BANK, branding.DOMAINS, branding.DEMO_MANDATE]
    for record in [*records, *branding.CAST]:
        for field in fields(record):
            assert getattr(record, field.name), (
                f"branding: {type(record).__name__}.{field.name} is empty"
            )
        with pytest.raises(FrozenInstanceError):
            setattr(record, fields(record)[0].name, "changed")
    assert isinstance(branding.CAST, tuple) and isinstance(branding.ADVISORS, tuple)
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
        domain = domains.staff if person.role in STAFF_ROLES else domains.attacker
        assert person.email == f"{person.id}@{domain}", (
            f"{person.role} e-mail {person.email!r} is not {person.id}@{domain}"
        )

    owner = [person for person in branding.CAST if person.id == branding.DEMO_MANDATE.advisor_id]
    assert len(owner) == 1, "the demo mandate's advisor is not in the cast"
    assert owner[0].role is Role.ADVISOR, "the demo mandate's advisor is not an advisor"


def test_branding_matches_claude_md() -> None:
    """The names in branding are the ones CLAUDE.md documents, the demo mandate's owner included."""
    text = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
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


def test_scanner_planted(tmp_path: Path) -> None:
    assert branding.ADVISORS and branding.BANK.name_stems, "branding is empty"
    repo_files.git(tmp_path, "init", "-q")
    bank, domains = branding.BANK, branding.DOMAINS
    # One line per rule; line numbers follow the list order.
    planted = [
        ("product", f"{branding.PRODUCT.name} draft"),
        ("legal name", bank.legal_name),
        ("display name", bank.display_name),
        ("bank stem", f"{bank.name_stems[0]} cipher"),
        ("bank stem", f"the {bank.name_stems[-1]}"),
        ("domain", f"https://{domains.canary.upper()}/x"),
        ("cast", f"call {branding.ADVISORS[0].name}"),
        ("demo mandate", f"{branding.DEMO_MANDATE.family.lower()} family"),
        ("slug", f"import {branding.SLUG}"),
    ]
    assert {rule for rule, _ in planted} == set(branding_scan.RULE_NAMES), "a rule is not planted"
    body = "".join(f"x = {value!r}\n" for _, value in planted)
    for rel in ("app/planted.py", "docs/x.md", "evals/recorded/run.json", "Makefile"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(body)

    scanned, findings = branding_scan.scan(tmp_path)
    assert set(scanned) == {"app/planted.py", "Makefile"}, f"scanned: {scanned}"
    reported = {(f.path, f.line, f.rule) for f in findings}

    # In code, each planted line is reported under its own rule. The Makefile may
    # carry the slug, and nothing else.
    for line, (rule, _) in enumerate(planted, start=1):
        assert ("app/planted.py", line, rule) in reported, f"line {line} ({rule}) not reported"
        in_makefile = ("Makefile", line, rule) in reported
        assert in_makefile is (rule != "slug"), f"Makefile line {line} ({rule}): {in_makefile}"

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


def _hits(text: str) -> set[str]:
    return {rule.name for rule in branding_scan.rules() if rule.pattern.search(text)}


def test_rule_case_and_word_boundaries() -> None:
    assert branding.ADVISORS and branding.BANK.name_stems, "branding is empty"
    advisor, stem = branding.ADVISORS[0].name, branding.BANK.name_stems[0]
    product, slug, staff = branding.PRODUCT.name, branding.SLUG, branding.DOMAINS.staff
    family = branding.DEMO_MANDATE.family
    cases = [
        # Whole words, any case: the cast, the demo mandate, the slug.
        (advisor.upper(), {"cast"}),
        (f"({advisor.lower()})", {"cast"}),
        (f"{advisor}-id", {"cast"}),
        (family.upper(), {"demo mandate"}),
        (f"{advisor}s", set()),
        (f"x{advisor}", set()),
        (f"{slug}2", set()),
        # The plan's \b: "_" is a word character, so an identifier joined with "_"
        # is not a hit. A known limit, pinned so that changing it is deliberate.
        (f"{advisor}_id", set()),
        (f"{slug}_app", set()),
        # Case-sensitive: the product, the bank names and stems.
        (product, {"product", "slug"}),
        (product.lower(), {"slug"}),
        (stem.lower(), set()),
        (f"{stem}s", set()),
        # Domains: substrings, any case.
        (staff.upper(), {"domain"}),
        (f"mail.{staff}", {"domain"}),
    ]
    wrong = [(text, expected, _hits(text)) for text, expected in cases if _hits(text) != expected]
    assert not wrong, "\n".join(f"{t!r}: expected {e}, got {g}" for t, e, g in wrong)


@pytest.mark.parametrize(
    ("path", "excluded", "slug_allowed"),
    [
        ("docs/x.md", True, False),
        ("docs/plans/a/b.md", True, False),
        ("docs/x.py", False, False),
        ("app/docs/x.md", False, False),
        ("README.md", True, False),
        ("app/README.md", False, False),
        ("redteam/SCOPE.md", True, False),
        ("redteam/attacks/SCOPE.md", False, False),
        (".claude/hooks/x.py", True, False),
        ("app/.claude/x.py", False, False),
        ("evals/recorded/run.json", True, False),
        ("a/b/recorded/x.json", True, False),
        ("recorded_x/a.json", False, False),
        ("evals/recorded.json", False, False),
        ("app/config/branding.py", True, False),
        ("app/config/branding_test.py", False, False),
        ("Makefile", False, True),
        ("app/Makefile", False, False),
        ("db/migrations/0001_schema.sql", False, True),
        ("db/x.py", False, False),
        (".github/workflows/ci.yml", False, True),
        ("app/.github/x.yml", False, False),
    ],
)
def test_globs(path: str, excluded: bool, slug_allowed: bool) -> None:
    assert matches_any(path, EXCLUDED) is excluded
    assert matches_any(path, SLUG_ALLOWED) is slug_allowed


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
