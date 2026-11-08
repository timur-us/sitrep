"""The decision records and the README have the shape the project promises.

ADRs follow .claude/skills/adr/SKILL.md: numbered without gaps, the skill's
sections in its order, a status the skill allows, dated sources, under one page.
docs/decisions/ holds ADRs only: any other file or directory there is reported.
The README says what the project shows and what it does not.
"""

from __future__ import annotations

import re
from datetime import date
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
NOT_AN_ITEM = "Sources line is not a '- ' item or its indented continuation"

README_SECTION = "What this project shows, and what it does not"
# Each item of "Shows" names one of these, with its milestone.
SHOWS = ("harness", "row-level security", "red team")
DOES_NOT_SHOW = ("production", "real data", "legal advice", "EU-only inference", "egress")


def sections(text: str) -> list[tuple[str, str]]:
    """(`## ` heading, body) pairs in file order, duplicates kept. Fences hold no heading."""
    # Blank the fences character by character, so offsets still point into text.
    unfenced = FENCE.sub(lambda match: re.sub(r"[^\n]", " ", match.group(0)), text)
    marks = list(HEADING.finditer(unfenced))
    ends = [mark.start() for mark in marks[1:]] + [len(text)] * bool(marks)
    return [
        (mark.group(1).strip(), text[mark.end() : end].strip())
        for mark, end in zip(marks, ends, strict=True)
    ]


def words(text: str) -> int:
    return len(FENCE.sub("", text).split())


def is_date(text: str) -> bool:
    try:
        date.fromisoformat(text)
    except ValueError:
        return False
    return bool(ISO_DATE.fullmatch(text))


def source_items(body: str) -> tuple[list[str], list[str]]:
    """The Sources bullets, each joined with its indented continuation lines, and
    the lines that are neither."""
    items: list[str] = []
    stray: list[str] = []
    for line in body.splitlines():
        if line.startswith("- "):
            items.append(line)
        elif line.startswith("  ") and line.strip() and items:
            items[-1] += " " + line.strip()
        elif line.strip():
            stray.append(line)
    return items, stray


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
        pairs = sections(text)
        headings = [heading for heading, _ in pairs]
        if headings != list(SECTIONS):
            problems.append(f"{name}: sections {headings}, expected {list(SECTIONS)}")
        found = dict(pairs)
        # A note may follow the status: "Accepted (Timur, 2026-10-28)".
        if not STATUS.match(found.get("Status", "")):
            problems.append(f"{name}: status is not Proposed, Accepted or Superseded by NNNN")
        if not is_date(found.get("Date", "")):
            problems.append(f"{name}: date is not YYYY-MM-DD")
        sources, stray = source_items(found.get("Sources", ""))
        if not sources:
            problems.append(f"{name}: no sources")
        # Known limit: a date inside a URL counts as the source's date.
        problems += [
            f"{name}: undated source: {item[:60]}"
            for item in sources
            if not any(is_date(day) for day in ISO_DATE.findall(item))
        ]
        problems += [f"{name}: {NOT_AN_ITEM}: {line.strip()}" for line in stray]
        body = words(text) - words(found.get("Sources", ""))
        if body > MAX_WORDS:
            problems.append(f"{name}: {body} words, more than one page ({MAX_WORDS})")
        times = text.count(spelled_out)
        if times != 1:
            problems.append(f"{name}: spells out {spelled_out!r} {times} times, expected once")
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


def test_planted_adr_shapes(tmp_path: Path) -> None:
    decisions = tmp_path / "decisions"
    spelled_out = f"{branding.PRODUCT.name} ({branding.PRODUCT.expansion})"
    wrapped = "- A long source,\n  continued on the next line (2026-10-28)."
    _write(decisions, 1, _adr(1).replace("# ADR 0001: ", "# 0001 ", 1))
    _write(decisions, 2, _adr(2).replace(spelled_out, "It"))
    _write(decisions, 3, _adr(3).replace("needs a decision.", f"needs {spelled_out}.", 1))
    _write(decisions, 4, _adr(4, source=""))
    _write(decisions, 5, _adr(5) + "\n## Sources\n\n" + SOURCE + "\n")
    _write(decisions, 6, _adr(6).replace("2026-10-29", "2026-99-99"))
    _write(decisions, 7, _adr(7, source=wrapped))
    _write(decisions, 8, _adr(8, source=SOURCE + "\n* Undated star item."))
    _write(decisions, 9, _adr(9, source=SOURCE + "\n1. Undated numbered item."))
    assert adr_problems(decisions) == [
        "0001-title.md: first line is not '# ADR 0001: <title>'",
        f"0002-title.md: spells out {spelled_out!r} 0 times, expected once",
        f"0003-title.md: spells out {spelled_out!r} 2 times, expected once",
        "0004-title.md: no sources",
        f"0005-title.md: sections {[*SECTIONS, 'Sources']}, expected {list(SECTIONS)}",
        "0006-title.md: date is not YYYY-MM-DD",
        f"0008-title.md: {NOT_AN_ITEM}: * Undated star item.",
        f"0009-title.md: {NOT_AN_ITEM}: 1. Undated numbered item.",
    ]


def test_words_skips_fences() -> None:
    assert words("a b\n```yaml\nx y z\n```\nc") == 3
    assert words("") == 0


def test_word_cap_boundary(tmp_path: Path) -> None:
    decisions = tmp_path / "decisions"
    base = words(_adr(1)) - words(SOURCE)

    def padded(number: int, extra: int) -> str:
        return _adr(number).replace("One.\n", "One. " + "w " * extra + "\n", 1)

    _write(decisions, 1, padded(1, MAX_WORDS - base))
    _write(decisions, 2, padded(2, MAX_WORDS - base + 1))
    fence = "```text\n" + "w " * (2 * MAX_WORDS) + "\n```\n"
    _write(decisions, 3, _adr(3).replace("One.\n", "One.\n\n" + fence, 1))
    for number in range(4, MIN_ADRS + 1):
        _write(decisions, number, _adr(number))
    assert adr_problems(decisions) == [
        f"0002-title.md: {MAX_WORDS + 1} words, more than one page ({MAX_WORDS})"
    ]


def test_planted_adr_heading_inside_a_fence_is_ignored(tmp_path: Path) -> None:
    decisions = tmp_path / "decisions"
    for number in range(1, MIN_ADRS + 1):
        _write(decisions, number, _adr(number))
    fenced = _adr(1).replace("One.\n", "One.\n\n```yaml\n## Sources\n```\n", 1)
    _write(decisions, 1, fenced)
    assert adr_problems(decisions) == []


PLAN = ROOT / "docs" / "plans" / "m0-foundation.md"
RUNBOOK = ROOT / "docs" / "runbooks" / "local-setup.md"
REPORT = ROOT / "docs" / "reports" / "m0.md"

# The plan's "Verification at the end of M0": one line of `make` commands joined by "&&".
VERIFICATION = re.compile(r"^- \*\*Fresh clone:\*\* `([^`\n]+)`", re.MULTILINE)
FRESH_CLONE = "From a fresh clone"
SH_BLOCK = re.compile(r"^```sh\n(.*?)^```", re.DOTALL | re.MULTILINE)
# The plan's sequence, pinned here too, so that an edit to the plan alone fails.
SEQUENCE = (
    "make up",
    "make migrate",
    "make test",
    "make evals",
    "make redteam",
    "make data",
    "make docker-build",
    "make web-test",
)


def read(path: Path) -> str:
    """The file's text, or "" when it does not exist, so a missing file fails an assertion."""
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def verification_sequence(plan: str) -> list[str]:
    """The plan's fresh-clone commands, in order; [] when the line is missing."""
    match = VERIFICATION.search(plan)
    return [command.strip() for command in match.group(1).split("&&")] if match else []


def fresh_clone_problems(runbook: str, sequence: list[str]) -> list[str]:
    """One line per way the runbook's fresh-clone block differs from the sequence."""
    found = [body for heading, body in sections(runbook) if heading == FRESH_CLONE]
    if len(found) != 1:
        return [f"{len(found)} '## {FRESH_CLONE}' sections, expected 1"]
    blocks = SH_BLOCK.findall(found[0])
    if len(blocks) != 1:
        return [f"'## {FRESH_CLONE}': {len(blocks)} sh blocks, expected 1"]
    # A trailing "# comment" says what the line shows; the command is what runs.
    lines = [line.split("#", 1)[0].strip() for line in blocks[0].splitlines()]
    commands = [line for line in lines if line]
    if commands != sequence:
        return [f"'## {FRESH_CLONE}' runs {commands}, the plan says {sequence}"]
    return []


def test_fresh_clone_block() -> None:
    sequence = verification_sequence(read(PLAN))
    assert sequence == list(SEQUENCE), f"the plan's fresh-clone line is {sequence}"
    problems = fresh_clone_problems(read(RUNBOOK), sequence)
    assert not problems, "runbook:\n" + "\n".join(problems)


def test_planted_fresh_clone_blocks() -> None:
    sequence = list(SEQUENCE)
    good = "\n".join(f"{command}  # note" for command in sequence)

    def runbook(*blocks: str, heading: str = FRESH_CLONE) -> str:
        fenced = "".join(f"```sh\n{block}\n```\n\n" for block in blocks)
        return f"# Local setup\n\n## {heading}\n\nNo `.env`.\n\n{fenced}## Next\n\nx\n"

    assert fresh_clone_problems(runbook(good), sequence) == []
    swapped = "\n".join([sequence[1], sequence[0], *sequence[2:]])
    shorter = "\n".join(sequence[:-1])
    extra = good + "\nmake down"
    reordered = [sequence[1], sequence[0], *sequence[2:]]
    assert fresh_clone_problems(runbook(swapped), sequence) == [
        f"'## {FRESH_CLONE}' runs {reordered}, the plan says {sequence}"
    ]
    assert fresh_clone_problems(runbook(shorter), sequence) == [
        f"'## {FRESH_CLONE}' runs {sequence[:-1]}, the plan says {sequence}"
    ]
    assert fresh_clone_problems(runbook(extra), sequence) == [
        f"'## {FRESH_CLONE}' runs {[*sequence, 'make down']}, the plan says {sequence}"
    ]
    assert fresh_clone_problems(runbook(good, good), sequence) == [
        f"'## {FRESH_CLONE}': 2 sh blocks, expected 1"
    ]
    assert fresh_clone_problems(runbook(good, heading="Fresh"), sequence) == [
        f"0 '## {FRESH_CLONE}' sections, expected 1"
    ]
    assert verification_sequence("- **Fresh clone:** `make a && make b` all pass") == [
        "make a",
        "make b",
    ]
    assert verification_sequence("- **Fresh:** `make a`") == []


# docs/reports/m0.md: CLAUDE.md rule 12's three parts, after the measured facts and the
# handovers, in this order.
REPORT_SECTIONS = (
    "Measured",
    "M1 handovers",
    "What is shaky",
    "What was left out",
    "What Timur has to do",
)
# Every M1 handover PRs 5b to 8 recorded, by the PR and the finding's id. PR 8's
# sec-1 to sec-8 are all listed, the fixed ones too, so the report says what is left.
HANDOVER_IDS = (
    "PR 5b sec-5",
    "PR 5b hnsw.iterative_scan",
    "PR 5b product sheets",
    "PR 5b GIN index",
    "PR 6 sec-2",
    "PR 6 sec-11",
    "PR 6 test-14",
    "PR 6 forged-argument handler",
    "PR 6 approval tokens",
    "PR 6 error locations",
    "PR 6 red-team cases",
    "PR 7 sec-1",
    "PR 7 sec-5",
    "PR 7 question 1",
    "PR 7 cross-advisor cases",
    *(f"PR 8 sec-{number}" for number in range(1, 9)),
    "PR 8 spec-4",
)
# Phrases the handovers must carry: Timur's answer for the loader ADR, and the
# forged-argument test's handler, which is M0's and not M1's.
HANDOVER_PHRASES = ("runtime-written text is always untrusted", "loader ADR", "test-local")
HANDOVER_ROW = re.compile(r"^\| ([^|\n]+?) \|", re.MULTILINE)


def report_problems(text: str) -> list[str]:
    """One line per way the M0 report misses its sections or its handovers."""
    if not text:
        return ["docs/reports/m0.md is missing or empty"]
    problems = []
    spelled_out = f"{branding.PRODUCT.name} ({branding.PRODUCT.expansion})"
    if (times := text.count(spelled_out)) != 1:
        problems.append(f"spells out {spelled_out!r} {times} times, expected once")
    pairs = sections(text)
    headings = [heading for heading, _ in pairs]
    if headings != list(REPORT_SECTIONS):
        problems.append(f"sections {headings}, expected {list(REPORT_SECTIONS)}")
    found = dict(pairs)
    problems += [f"'## {name}' is empty" for name in REPORT_SECTIONS if found.get(name) == ""]
    handovers = found.get("M1 handovers", "")
    rows = set(HANDOVER_ROW.findall(handovers))
    problems += [f"no handover row {item!r}" for item in HANDOVER_IDS if item not in rows]
    problems += [
        f"the handovers never say {phrase!r}"
        for phrase in HANDOVER_PHRASES
        if phrase not in handovers
    ]
    return problems


def test_m0_report_sections() -> None:
    problems = report_problems(read(REPORT))
    assert not problems, "docs/reports/m0.md:\n" + "\n".join(problems)


def test_planted_m0_reports() -> None:
    spelled_out = f"{branding.PRODUCT.name} ({branding.PRODUCT.expansion})"
    rows = "\n".join(f"| {item} | M1 |" for item in HANDOVER_IDS)
    handovers = f"| Id | To |\n|---|---|\n{rows}\n\n" + " ".join(HANDOVER_PHRASES)
    bodies = {name: "Text." for name in REPORT_SECTIONS} | {"M1 handovers": handovers}

    def report(bodies: dict[str, str], intro: str = spelled_out) -> str:
        parts = [f"# M0 report\n\n{intro} closes M0."]
        parts += [f"## {name}\n\n{body}" for name, body in bodies.items()]
        return "\n\n".join(parts) + "\n"

    assert report_problems(report(bodies)) == []
    assert report_problems("") == ["docs/reports/m0.md is missing or empty"]

    missing = {name: body for name, body in bodies.items() if name != "What is shaky"}
    assert report_problems(report(missing)) == [
        f"sections {list(missing)}, expected {list(REPORT_SECTIONS)}"
    ]
    assert report_problems(report(bodies | {"What was left out": ""})) == [
        "'## What was left out' is empty"
    ]
    assert report_problems(report(bodies, intro="It")) == [
        f"spells out {spelled_out!r} 0 times, expected once"
    ]
    dropped = handovers.replace("| PR 8 sec-4 | M1 |\n", "").replace("test-local", "local")
    assert report_problems(report(bodies | {"M1 handovers": dropped})) == [
        "no handover row 'PR 8 sec-4'",
        "the handovers never say 'test-local'",
    ]
    # An id in prose, not in a row's first cell, does not count.
    in_prose = handovers.replace("| PR 7 sec-1 | M1 |\n", "") + " PR 7 sec-1"
    assert report_problems(report(bodies | {"M1 handovers": in_prose})) == [
        "no handover row 'PR 7 sec-1'"
    ]


def test_readme_has_shows_and_does_not() -> None:
    found = dict(sections(README.read_text(encoding="utf-8")))
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
