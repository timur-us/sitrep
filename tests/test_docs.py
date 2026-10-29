"""The decision records and the README have the shape the project promises.

ADRs follow .claude/skills/adr/SKILL.md: numbered without gaps, the skill's
sections in its order, a status the skill allows, dated sources, under one page.
The README says what the project shows and what it does not.
"""

from __future__ import annotations

import re
from pathlib import Path

from repo_files import ROOT

from app.config import branding

DECISIONS = ROOT / "docs" / "decisions"
README = ROOT / "README.md"

MIN_ADRS = 6
SECTIONS = ("Status", "Date", "Context", "Options", "Decision", "Consequences", "Sources")
# One page: words outside fenced blocks and outside the Sources section.
MAX_WORDS = 600

ADR_NAME = re.compile(r"(\d{4})-[a-z0-9]+(?:-[a-z0-9]+)*\.md")
STATUS = re.compile(r"(?:Proposed|Accepted|Superseded by \d{4})\b")
ISO_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
FENCE = re.compile(r"^```.*?^```", re.DOTALL | re.MULTILINE)
HEADING = re.compile(r"^## (.+)$", re.MULTILINE)
MILESTONE = re.compile(r"\bM[0-4]\b")

README_SECTION = "What this project shows, and what it does not"
# Each item of "Shows" names one of these, with its milestone.
SHOWS = ("harness", "row-level security", "red team")
DOES_NOT_SHOW = ("production", "real data", "legal advice", "EU-only inference", "egress")


def sections(text: str) -> dict[str, str]:
    """`## ` heading -> body, in file order. Fenced blocks never hold a heading."""
    # Blank the fences character by character, so offsets still point into text.
    unfenced = FENCE.sub(lambda match: re.sub(r"[^\n]", " ", match.group(0)), text)
    marks = list(HEADING.finditer(unfenced))
    ends = [mark.start() for mark in marks[1:]] + [len(text)] * bool(marks)
    return {
        mark.group(1).strip(): text[mark.end() : end].strip()
        for mark, end in zip(marks, ends, strict=True)
    }


def words(text: str) -> int:
    return len(FENCE.sub("", text).split())


def adr_problems(directory: Path) -> list[str]:
    """One line per way the ADRs in directory break the skill's format."""
    paths = sorted(directory.iterdir()) if directory.is_dir() else []
    problems = [f"{path.name}: not NNNN-short-title.md" for path in paths]
    adrs = {}
    for path in paths:
        if match := ADR_NAME.fullmatch(path.name):
            problems.remove(f"{path.name}: not NNNN-short-title.md")
            adrs.setdefault(int(match.group(1)), []).append(path)
    if len(adrs) < MIN_ADRS:
        problems.append(f"found {len(adrs)} ADRs, expected at least {MIN_ADRS}")
    problems += [
        f"{number:04d}: {len(found)} files" for number, found in adrs.items() if len(found) > 1
    ]
    problems += [
        f"{number:04d}: missing, numbering has a gap"
        for number in range(1, max(adrs, default=0) + 1)
        if number not in adrs
    ]
    spelled_out = f"{branding.PRODUCT.name} ({branding.PRODUCT.expansion})"
    for number, (path, *_) in sorted(adrs.items()):
        text = path.read_text(encoding="utf-8")
        name = path.name
        if not text.startswith(f"# ADR {number:04d}: "):
            problems.append(f"{name}: first line is not '# ADR {number:04d}: <title>'")
        found = sections(text)
        if tuple(found) != SECTIONS:
            problems.append(f"{name}: sections {list(found)}, expected {list(SECTIONS)}")
        if not STATUS.match(found.get("Status", "")):
            problems.append(f"{name}: status is not Proposed, Accepted or Superseded by NNNN")
        if not ISO_DATE.fullmatch(found.get("Date", "")):
            problems.append(f"{name}: date is not YYYY-MM-DD")
        sources = [line for line in found.get("Sources", "").splitlines() if line.startswith("- ")]
        if not sources:
            problems.append(f"{name}: no sources")
        problems += [
            f"{name}: undated source: {line[:60]}" for line in sources if not ISO_DATE.search(line)
        ]
        body = words(text) - words(found.get("Sources", ""))
        if body > MAX_WORDS:
            problems.append(f"{name}: {body} words, more than one page ({MAX_WORDS})")
        if spelled_out not in text:
            problems.append(f"{name}: never spells out {spelled_out!r}")
    return problems


def test_adr_structure() -> None:
    problems = adr_problems(DECISIONS)
    assert not problems, "ADRs:\n" + "\n".join(problems)


SOURCE = "- The plan, `docs/plans/m0-foundation.md` (2026-10-28)."


def _adr(number: int, *, drop: str = "", status: str = "Accepted", source: str = SOURCE) -> str:
    spelled_out = f"{branding.PRODUCT.name} ({branding.PRODUCT.expansion})"
    body = {
        "Status": status,
        "Date": "2026-10-29",
        "Context": f"{spelled_out} needs a decision.",
        "Options": "- One.\n- Two.",
        "Decision": "One.",
        "Consequences": "- Easier: this. Harder: that.",
        "Sources": source,
    }
    parts = [f"# ADR {number:04d}: Title"]
    parts += [f"## {name}\n\n{text}" for name, text in body.items() if name != drop]
    return "\n\n".join(parts) + "\n"


def _write(directory: Path, number: int, text: str, slug: str = "title") -> None:
    directory.mkdir(exist_ok=True)
    (directory / f"{number:04d}-{slug}.md").write_text(text, encoding="utf-8")


def test_planted_adrs_are_checked(tmp_path: Path) -> None:
    decisions = tmp_path / "decisions"
    for number in range(1, MIN_ADRS + 1):
        _write(decisions, number, _adr(number))
    assert adr_problems(decisions) == []

    _write(decisions, 2, _adr(2, drop="Consequences"))
    _write(decisions, 3, _adr(3, status="Rejected"))
    _write(decisions, 4, _adr(4, source="- https://example.org/undated"))
    _write(decisions, 5, _adr(5).replace("2026-10-29", "29 October"))
    long = _adr(6).replace("One.\n", "One. " + "word " * MAX_WORDS + "\n", 1)
    _write(decisions, 6, long)
    _write(decisions, 8, _adr(8))
    _write(decisions, 8, _adr(8), slug="twin")
    (decisions / "notes.md").write_text("x\n", encoding="utf-8")
    assert adr_problems(decisions) == [
        "notes.md: not NNNN-short-title.md",
        "0008: 2 files",
        "0007: missing, numbering has a gap",
        f"0002-title.md: sections {[s for s in SECTIONS if s != 'Consequences']}, expected "
        + str(list(SECTIONS)),
        "0003-title.md: status is not Proposed, Accepted or Superseded by NNNN",
        "0004-title.md: undated source: - https://example.org/undated",
        "0005-title.md: date is not YYYY-MM-DD",
        f"0006-title.md: {words(long) - words(SOURCE)} words, more than one page ({MAX_WORDS})",
    ]
    assert words(long) - words(SOURCE) > MAX_WORDS >= words(_adr(6)) - words(SOURCE)


def test_planted_adr_heading_inside_a_fence_is_ignored(tmp_path: Path) -> None:
    decisions = tmp_path / "decisions"
    for number in range(1, MIN_ADRS + 1):
        _write(decisions, number, _adr(number))
    fenced = _adr(1).replace("One.\n", "One.\n\n```yaml\n## Sources\n```\n", 1)
    _write(decisions, 1, fenced)
    assert adr_problems(decisions) == []


def test_readme_has_shows_and_does_not() -> None:
    found = sections(README.read_text(encoding="utf-8"))
    assert README_SECTION in found, f"README has no '## {README_SECTION}'"
    section = found[README_SECTION]
    parts = dict(re.findall(r"^### (.+?)\n(.*?)(?=^### |\Z)", section, re.DOTALL | re.MULTILINE))
    assert set(parts) == {"Shows", "Does not show"}, f"subsections: {sorted(parts)}"

    shows = [line for line in parts["Shows"].splitlines() if line.startswith("- ")]
    for topic in SHOWS:
        items = [line for line in shows if topic.lower() in line.lower()]
        assert items, f"'Shows' has no item about {topic!r}"
        assert all(MILESTONE.search(line) for line in items), f"{topic!r}: no milestone named"
    assert all(MILESTONE.search(line) for line in shows), "every 'Shows' item names a milestone"

    does_not = parts["Does not show"].lower()
    missing = [topic for topic in DOES_NOT_SHOW if topic.lower() not in does_not]
    assert not missing, f"'Does not show' misses: {missing}"
