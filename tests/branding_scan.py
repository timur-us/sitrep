"""Branding scanner: finds branding strings outside app/config/branding.py.

The rules are built from branding at runtime, so this file names no product,
bank, domain or cast member itself.

- Case-sensitive: product, legal name, display name, and the bank's name stems
  as whole words.
- Substrings, case-insensitive: the domains.
- Whole words, case-insensitive: the cast, the demo mandate and the slug.

Eval cases and attacks name the cast and the demo mandate through placeholders that the
runner resolves from branding (`{advisor.<id>}`, `{mandate.demo}`; app/runners/corpus.py,
which refuses a literal). In those two directories only, a placeholder the runner knows is
blanked before the rules run; any literal around it is still found (M1 PR 16).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from repo_files import ROOT, list_files, read_text

from app.config import branding
from app.config.settings import ENV_PREFIX, Settings, env_name
from app.runners.corpus import PLACEHOLDER, PLACEHOLDERS

# Never scanned. Prose must spell out the name (CLAUDE.md scopes the single source
# to generator, prompts, allowlists, tests and UI); recordings are generated data.
EXCLUDED = (
    "docs/**/*.md",
    "README.md",
    "CLAUDE.md",
    "redteam/SCOPE.md",
    ".claude/**",
    "**/recorded/**",
    "app/config/branding.py",
)

# Case files, where the runner's placeholders stand in for the names (see the docstring).
PLACEHOLDER_DIRS = ("evals/cases/**", "redteam/attacks/**")


def blank_placeholders(line: str) -> str:
    """line with every placeholder the runner resolves replaced by spaces."""

    def known(match: re.Match[str]) -> str:
        key = f"{match.group(1)}.{match.group(2)}"
        return " " * len(match.group(0)) if key in PLACEHOLDERS else match.group(0)

    return PLACEHOLDER.sub(known, line)


# Infrastructure files that cannot import Python: the slug is allowed there, and
# only the slug.
SLUG_ALLOWED = (
    "pyproject.toml",
    "uv.lock",
    "compose.yaml",
    "Dockerfile",
    "Makefile",
    ".env.example",
    "db/**/*.sql",
    ".github/**",
)

# The settings' own variables (ADR 0009), and no other token that merely starts like one
# (review finding test-12): the names come from app/config/settings.py.
SETTINGS_NAMES = sorted(
    name for field in Settings.model_fields if (name := env_name(field)).startswith(ENV_PREFIX)
)
SETTINGS_VARIABLE = re.compile(rf"\b(?:{'|'.join(map(re.escape, SETTINGS_NAMES))})\b")

RULE_NAMES = (
    "product",
    "legal name",
    "display name",
    "bank stem",
    "domain",
    "cast",
    "demo mandate",
    "slug",
)


@dataclass(frozen=True)
class Rule:
    name: str
    pattern: re.Pattern[str]


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    rule: str
    text: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.rule}: {self.text!r}"


def glob_regex(pattern: str) -> re.Pattern[str]:
    """A glob over repo-relative POSIX paths.

    `**` as a whole segment matches any number of segments; `*` and `?` stay
    inside one segment.
    """
    parts = pattern.split("/")
    regex = []
    for index, part in enumerate(parts):
        last = index == len(parts) - 1
        if part == "**":
            regex.append(".*" if last else "(?:[^/]+/)*")
            continue
        segment = "".join(
            "[^/]*" if char == "*" else "[^/]" if char == "?" else re.escape(char) for char in part
        )
        regex.append(segment if last else segment + "/")
    return re.compile("".join(regex))


def matches_any(path: str, patterns: tuple[str, ...]) -> bool:
    return any(glob_regex(pattern).fullmatch(path) for pattern in patterns)


def _words(values: list[str]) -> list[str]:
    """Non-empty values, deduplicated without regard to case."""
    return list({value.lower(): value for value in values if value}.values())


def rules() -> list[Rule]:
    found = []

    def add(name: str, value: str, flags: int = 0, whole_word: bool = False) -> None:
        if value:
            body = re.escape(value)
            found.append(Rule(name, re.compile(rf"\b{body}\b" if whole_word else body, flags)))

    add("product", branding.PRODUCT.name)
    add("legal name", branding.BANK.legal_name)
    add("display name", branding.BANK.display_name)
    for stem in branding.BANK.name_stems:
        add("bank stem", stem, whole_word=True)
    domains = branding.DOMAINS
    for domain in (domains.staff, domains.attacker, domains.canary):
        add("domain", domain, re.IGNORECASE)
    for word in _words([value for person in branding.CAST for value in (person.name, person.id)]):
        add("cast", word, re.IGNORECASE, whole_word=True)
    mandate = branding.DEMO_MANDATE
    for word in _words([mandate.family, mandate.id]):
        add("demo mandate", word, re.IGNORECASE, whole_word=True)
    add("slug", branding.SLUG, re.IGNORECASE, whole_word=True)
    return found


def scan(root: Path = ROOT) -> tuple[list[str], list[Finding]]:
    """Scan every file git sees under root. Returns the scanned paths and the findings."""
    active = rules()
    scanned: list[str] = []
    findings: list[Finding] = []
    for rel in list_files(root):
        if matches_any(rel, EXCLUDED):
            continue
        text = read_text(root, rel)
        if text is None:
            continue
        scanned.append(rel)
        slug_allowed = matches_any(rel, SLUG_ALLOWED)
        placeholders = matches_any(rel, PLACEHOLDER_DIRS)
        for number, line in enumerate(text.splitlines(), start=1):
            if placeholders:
                line = blank_placeholders(line)
            if slug_allowed:
                # The settings' variables (ADR 0009) carry the slug in capitals; there
                # they are the slug, not the product name.
                line = SETTINGS_VARIABLE.sub("", line)
            for rule in active:
                if rule.name == "slug" and slug_allowed:
                    continue
                findings.extend(
                    Finding(rel, number, rule.name, match.group(0))
                    for match in rule.pattern.finditer(line)
                )
    return scanned, findings
