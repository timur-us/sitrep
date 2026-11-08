"""The decision records and the README have the shape the project promises.

ADRs follow .claude/skills/adr/SKILL.md: numbered without gaps, the skill's
sections in its order, a status the skill allows, dated sources, under one page.
docs/decisions/ holds ADRs only: any other file or directory there is reported.
The README says what the project shows and what it does not.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from datetime import date
from pathlib import Path

import yaml
from repo_files import ROOT

from app.config import branding
from data.synthetic import generate

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


# A fenced block, as CommonMark opens it: up to three spaces, then ``` and a language.
ANY_FENCE = re.compile(r"^ {0,3}```([^\n`]*)\n(.*?)^ {0,3}```", re.DOTALL | re.MULTILINE)
COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


def blank(text: str, pattern: re.Pattern[str]) -> str:
    """text with every match of pattern blanked character by character, newlines kept,
    so offsets still point into text."""
    return pattern.sub(lambda match: re.sub(r"[^\n]", " ", match.group(0)), text)


def sections(text: str) -> list[tuple[str, str]]:
    """(`## ` heading, body) pairs in file order, duplicates kept. Fences and HTML
    comments hold no heading; the bodies are the raw text."""
    marks = list(HEADING.finditer(blank(blank(text, ANY_FENCE), COMMENT)))
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
    # Every fenced block counts, in any language; a block inside a comment does not.
    blocks = ANY_FENCE.findall(blank(found[0], COMMENT))
    if len(blocks) != 1:
        return [f"'## {FRESH_CLONE}': {len(blocks)} fenced blocks, expected 1"]
    language, body = blocks[0]
    if language.strip() != "sh":
        return [f"'## {FRESH_CLONE}': the block is {language.strip()!r}, expected 'sh'"]
    # A trailing "# comment" says what the line shows; the command is what runs.
    lines = [line.split("#", 1)[0].strip() for line in body.splitlines()]
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

    def runbook(*blocks: str, heading: str = FRESH_CLONE, language: str = "sh") -> str:
        fenced = "".join(f"```{language}\n{block}\n```\n\n" for block in blocks)
        return f"# Local setup\n\n## {heading}\n\nNo `.env`.\n\n{fenced}## Next\n\nx\n"

    def runs(commands: list[str]) -> list[str]:
        return [f"'## {FRESH_CLONE}' runs {commands}, the plan says {sequence}"]

    def problems(text: str) -> list[str]:
        return fresh_clone_problems(text, sequence)

    assert problems(runbook(good)) == []
    # The commands: order, length, an extra step, a step commented out, an empty block.
    reordered = [sequence[1], sequence[0], *sequence[2:]]
    assert problems(runbook("\n".join(reordered))) == runs(reordered)
    assert problems(runbook("\n".join(sequence[:-1]))) == runs(sequence[:-1])
    assert problems(runbook(good + "\nmake down")) == runs([*sequence, "make down"])
    assert problems(runbook(good.replace("make evals", "# make evals"))) == runs(
        [command for command in sequence if command != "make evals"]
    )
    assert problems(runbook("")) == runs([])
    # The blocks: exactly one, in sh; a second block in any language or indented counts.
    two = f"'## {FRESH_CLONE}': 2 fenced blocks, expected 1"
    assert problems(runbook(good, good)) == [two]
    beside = runbook(good).replace("## Next", "```bash\nmake down\n```\n\n## Next")
    assert problems(beside) == [two]
    indented = runbook(good).replace("## Next", "   ```console\nmake down\n   ```\n\n## Next")
    assert problems(indented) == [two]
    assert problems(runbook()) == [f"'## {FRESH_CLONE}': 0 fenced blocks, expected 1"]
    assert problems(runbook(good, language="bash")) == [
        f"'## {FRESH_CLONE}': the block is 'bash', expected 'sh'"
    ]
    # A block inside a comment is not read: the visible one is.
    hidden = runbook("make up").replace("No `.env`.", f"<!--\n```sh\n{good}\n```\n-->")
    assert problems(hidden) == runs(["make up"])
    # The heading: exactly one, and one inside a fence or a comment does not count.
    none = [f"0 '## {FRESH_CLONE}' sections, expected 1"]
    assert problems(runbook(good, heading="Fresh")) == none
    assert problems(runbook(good) + f"\n## {FRESH_CLONE}\n\nAgain.\n") == [
        f"2 '## {FRESH_CLONE}' sections, expected 1"
    ]
    fenced_heading = f"```text\n## {FRESH_CLONE}\n```\n"
    assert problems(runbook(good, heading="Fresh") + fenced_heading) == none
    assert problems(runbook(good, heading="Fresh") + f"<!-- ## {FRESH_CLONE} -->\n") == none
    commented_heading = runbook(good).replace(f"## {FRESH_CLONE}", f"<!--\n## {FRESH_CLONE}\n-->")
    assert problems(commented_heading) == none
    # The plan's line.
    assert verification_sequence("- **Fresh clone:** `make a && make b` all pass") == [
        "make a",
        "make b",
    ]
    assert verification_sequence("- **Fresh:** `make a`") == []


# docs/reports/m0.md: the measured facts and the handovers, then CLAUDE.md rule 12's
# three parts, in this order. The three parts each hold at least one list item.
RULE_12 = ("What is shaky", "What was left out", "What Timur has to do")
REPORT_SECTIONS = ("Measured", "M1 handovers", *RULE_12)
LIST_ITEM = re.compile(r"^(?:- |\d+\. )\S", re.MULTILINE)
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
# What a handover's own row must say: Timur's answer for the loader ADR, and that the
# forged-argument test's handler is M0's, not M1's.
HANDOVER_PHRASES = {
    "PR 8 sec-1": ("runtime-written text is always untrusted", "loader ADR"),
    "PR 6 forged-argument handler": ("test-local",),
}
# Each row: the id, what M1 has to do, the source.
HANDOVER_CELLS = 3
TABLE_ROW = re.compile(r"^\|(.*)\|[ \t]*$", re.MULTILINE)
SEPARATOR = re.compile(r"[\s:|-]+")


def table_rows(text: str) -> list[list[str]]:
    """The visible table rows, as stripped cells; separator rows, fences and comments not."""
    visible = blank(blank(text, ANY_FENCE), COMMENT)
    return [
        [cell.strip() for cell in match.group(1).split("|")]
        for match in TABLE_ROW.finditer(visible)
        if not SEPARATOR.fullmatch(match.group(0))
    ]


def report_problems(text: str) -> list[str]:
    """One line per way the M0 report misses its sections or its handovers."""
    if not text:
        return ["docs/reports/m0.md is missing or empty"]
    problems = []
    spelled_out = f"{branding.PRODUCT.name} ({branding.PRODUCT.expansion})"
    if (times := blank(blank(text, ANY_FENCE), COMMENT).count(spelled_out)) != 1:
        problems.append(f"spells out {spelled_out!r} {times} times, expected once")
    pairs = sections(text)
    headings = [heading for heading, _ in pairs]
    if headings != list(REPORT_SECTIONS):
        problems.append(f"sections {headings}, expected {list(REPORT_SECTIONS)}")
    found = dict(pairs)
    problems += [f"'## {name}' is empty" for name in REPORT_SECTIONS if found.get(name) == ""]
    problems += [
        f"'## {name}' has no list item"
        for name in RULE_12
        if found.get(name) and not LIST_ITEM.search(blank(found[name], ANY_FENCE))
    ]
    rows: dict[str, list[list[str]]] = {}
    for row in table_rows(found.get("M1 handovers", "")):
        rows.setdefault(row[0], []).append(row)
    for item in HANDOVER_IDS:
        mine = rows.get(item, [])
        if not mine:
            problems.append(f"no handover row {item!r}")
            continue
        if len(mine) > 1:
            problems.append(f"handover row {item!r} appears {len(mine)} times")
        row = mine[0]
        if len(row) != HANDOVER_CELLS or not all(row):
            problems.append(f"handover row {item!r} has an empty or missing cell")
        problems += [
            f"handover row {item!r} never says {phrase!r}"
            for phrase in HANDOVER_PHRASES.get(item, ())
            if phrase not in " ".join(row)
        ]
    return problems


def test_m0_report_sections() -> None:
    problems = report_problems(read(REPORT))
    assert not problems, "docs/reports/m0.md:\n" + "\n".join(problems)


def test_planted_m0_reports() -> None:
    spelled_out = f"{branding.PRODUCT.name} ({branding.PRODUCT.expansion})"

    def row(item: str) -> str:
        says = " ".join(HANDOVER_PHRASES.get(item, ("Do it.",)))
        return f"| {item} | {says} | #1 |"

    rows = "\n".join(row(item) for item in HANDOVER_IDS)
    handovers = f"| Id | What | Source |\n|---|---|---|\n{rows}\n\nThe loader ADR comes first."
    bodies = {name: "- Item." for name in REPORT_SECTIONS} | {"M1 handovers": handovers}

    def report(bodies: dict[str, str] = bodies, intro: str = spelled_out) -> str:
        parts = [f"# M0 report\n\n{intro} closes M0."]
        parts += [f"## {name}\n\n{body}" for name, body in bodies.items()]
        return "\n\n".join(parts) + "\n"

    def with_handovers(text: str) -> str:
        return report(bodies | {"M1 handovers": text})

    assert report_problems(report()) == []
    assert report_problems("") == ["docs/reports/m0.md is missing or empty"]

    # Sections: missing, reordered, duplicated, inside a fence; empty; no list item.
    expected = list(REPORT_SECTIONS)
    missing = {name: body for name, body in bodies.items() if name != "What is shaky"}
    assert report_problems(report(missing)) == [f"sections {list(missing)}, expected {expected}"]
    swapped = dict(reversed(list(bodies.items())))
    assert report_problems(report(swapped)) == [f"sections {list(swapped)}, expected {expected}"]
    doubled = report() + "\n## What is shaky\n\n- Again.\n"
    assert report_problems(doubled) == [
        f"sections {[*expected, 'What is shaky']}, expected {expected}"
    ]
    fenced = report(missing) + "\n```text\n## What is shaky\n\n- Item.\n```\n"
    assert report_problems(fenced) == [f"sections {list(missing)}, expected {expected}"]
    assert report_problems(report(bodies | {"What was left out": ""})) == [
        "'## What was left out' is empty"
    ]
    assert report_problems(report(bodies | {"What is shaky": "TODO"})) == [
        "'## What is shaky' has no list item"
    ]
    assert report_problems(report(bodies | {"What is shaky": "```\n- x\n```"})) == [
        "'## What is shaky' has no list item"
    ]

    # The spelled-out name: once, outside fences and comments.
    zero = [f"spells out {spelled_out!r} 0 times, expected once"]
    assert report_problems(report(intro="It")) == zero
    assert report_problems(report(intro=f"<!-- {spelled_out} -->")) == zero
    assert report_problems(report(intro=f"It.\n\n```\n{spelled_out}\n```")) == zero

    # Rows: dropped, in prose only, in a fence, in a comment, doubled, with an empty cell.
    sec_4 = row("PR 8 sec-4") + "\n"
    assert report_problems(with_handovers(handovers.replace(sec_4, ""))) == [
        "no handover row 'PR 8 sec-4'"
    ]
    assert report_problems(with_handovers(handovers.replace(sec_4, "") + " PR 8 sec-4")) == [
        "no handover row 'PR 8 sec-4'"
    ]
    in_fence = handovers.replace(sec_4, "") + f"\n\n```\n{sec_4}```\n"
    assert report_problems(with_handovers(in_fence)) == ["no handover row 'PR 8 sec-4'"]
    in_comment = handovers.replace(sec_4, f"<!--\n{sec_4}-->\n")
    assert report_problems(with_handovers(in_comment)) == ["no handover row 'PR 8 sec-4'"]
    assert report_problems(with_handovers(handovers.replace(sec_4, sec_4 + sec_4))) == [
        "handover row 'PR 8 sec-4' appears 2 times"
    ]
    for hollow in ("| PR 8 sec-4 |  | #1 |", "| PR 8 sec-4 | Do it. |  |", "| PR 8 sec-4 |"):
        assert report_problems(with_handovers(handovers.replace(sec_4, hollow + "\n"))) == [
            "handover row 'PR 8 sec-4' has an empty or missing cell"
        ], hollow

    # Phrases: in their own row, not elsewhere in the section.
    handler = row("PR 6 forged-argument handler")
    moved = handovers.replace(handler, "| PR 6 forged-argument handler | Retarget. | #14 |")
    moved += " The handler is test-local."
    assert report_problems(with_handovers(moved)) == [
        "handover row 'PR 6 forged-argument handler' never says 'test-local'"
    ]
    sec_1 = row("PR 8 sec-1")
    elsewhere = handovers.replace(sec_1, "| PR 8 sec-1 | Untrusted. | #18 |")
    elsewhere = elsewhere.replace(
        "| Do it. | #1 |", "| runtime-written text is always untrusted | #1 |", 1
    )
    assert report_problems(with_handovers(elsewhere)) == [
        "handover row 'PR 8 sec-1' never says 'runtime-written text is always untrusted'",
        "handover row 'PR 8 sec-1' never says 'loader ADR'",
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


# ADR 0010 (M1 PR 1): the origins, their trust, the tables that hold them and the
# attack-id prefix, as the ADR's one yaml block. The generator is pinned to the block
# here; the schema is pinned to it from PR 7 on.
ADR_0010_KEYS = ("corpus_origins", "runtime_origins", "tables", "attack_id_prefix", "attack_origin")
# Every table that holds document text (the M1 plan, PR 1).
TEXT_TABLES = ("documents", "product_sheets", "drafts", "followups")
TRUST = ("internal", "untrusted")
# The id fields of the generated records: none may start with the attack-id prefix.
ID_FIELDS = ("id", "advisor_id", "mandate_id")
Corpus = Mapping[str, list[dict[str, object]]]


class DuplicateKey(yaml.YAMLError):
    """A yaml mapping that names one key twice."""


class UniqueKeyLoader(yaml.SafeLoader):
    """SafeLoader that refuses a mapping with two equal keys: PyYAML keeps the last one
    without a warning, while a reader of the ADR sees the first."""


def _unique_mapping(loader: UniqueKeyLoader, node: yaml.MappingNode) -> dict[object, object]:
    keys = [loader.construct_object(key, deep=True) for key, _ in node.value]
    for index, key in enumerate(keys):
        if key in keys[:index]:
            raise DuplicateKey(f"duplicate key {key!r}")
    return loader.construct_mapping(node, deep=True)


UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping)


def load_block(block: str) -> tuple[object, str | None]:
    """The yaml block's value and None, or None and the problem."""
    try:
        return yaml.load(block, Loader=UniqueKeyLoader), None  # noqa: S506 (a SafeLoader)
    except DuplicateKey as error:
        return None, f"the yaml block repeats a key: {error}"
    except yaml.YAMLError:
        return None, "the yaml block is not yaml"


def yaml_blocks(text: str) -> list[str]:
    """The bodies of the visible ```yaml blocks; one inside an HTML comment is not read."""
    return [
        body for language, body in ANY_FENCE.findall(blank(text, COMMENT)) if language == "yaml"
    ]


def corpus_ids(corpus: Corpus) -> list[str]:
    """Every id field's value in the generated records, as file:field=value."""
    return [
        f"{name}:{field}={record[field]}"
        for name, records in sorted(corpus.items())
        for record in records
        for field in ID_FIELDS
        if isinstance(record.get(field), str)
    ]


def adr_0010_problems(
    adr: str, origin: Mapping[str, str], trust_for: Callable[[str], str], corpus: Corpus
) -> list[str]:
    """One line per way ADR 0010's block disagrees with the generator or with itself."""
    blocks = yaml_blocks(adr)
    if len(blocks) != 1:
        return [f"expected one yaml block, found {len(blocks)}"]
    facts, problem = load_block(blocks[0])
    if problem:
        return [problem]
    if not isinstance(facts, dict):
        return ["the yaml block is not a mapping"]
    problems = [f"{key}: not in the block" for key in ADR_0010_KEYS if key not in facts]
    problems += [f"{key}: in the block, not expected" for key in facts if key not in ADR_0010_KEYS]
    if problems:
        return problems
    for key in ("corpus_origins", "runtime_origins", "tables"):
        if not isinstance(facts[key], dict) or not facts[key]:
            problems.append(f"{key}: not a non-empty mapping")
    for key in ("attack_id_prefix", "attack_origin"):
        if not isinstance(facts[key], str) or not facts[key]:
            problems.append(f"{key}: not a non-empty string")
    if problems:
        return problems
    corpus_origins, runtime = facts["corpus_origins"], facts["runtime_origins"]
    prefix = facts["attack_id_prefix"]

    # The corpus origins and their trust are the generator's, one origin per type.
    if len(set(origin.values())) != len(origin):
        problems.append("the generator gives two document types one origin")
    expected = {origin[doc_type]: trust_for(doc_type) for doc_type in origin}
    if corpus_origins != expected:
        problems.append(f"corpus_origins: {corpus_origins}, the generator says {expected}")
    every = {**corpus_origins, **runtime}
    problems += [
        f"origin {name!r}: trust {trust!r}, not one of {list(TRUST)}"
        for name, trust in every.items()
        if trust not in TRUST
    ]
    problems += [
        f"runtime origin {name!r}: {trust!r}, not 'untrusted'"
        for name, trust in runtime.items()
        if trust != "untrusted"
    ]
    problems += [
        f"origin {name!r}: both a corpus and a runtime origin"
        for name in sorted(set(corpus_origins) & set(runtime))
    ]

    # Every origin lives in exactly one of the text tables.
    tables = facts["tables"]
    if sorted(tables) != sorted(TEXT_TABLES):
        problems.append(f"tables: {sorted(tables)}, expected {sorted(TEXT_TABLES)}")
    placed: dict[str, list[str]] = {}
    for table, names in tables.items():
        if not isinstance(names, list) or not names:
            problems.append(f"tables.{table}: not a non-empty list")
            continue
        for name in names:
            placed.setdefault(str(name), []).append(table)
    problems += [
        f"origin {name!r}: in {homes or 'no table'}, expected exactly one"
        for name, homes in ((name, placed.get(name, [])) for name in every)
        if len(homes) != 1
    ]
    problems += [f"tables: {name!r} is no origin" for name in placed if name not in every]

    # The generated records: each origin in its table, each trust the block's.
    homes = {"documents.jsonl": "documents", "product_sheets.jsonl": "product_sheets"}
    for name, table in homes.items():
        for record in corpus.get(name, []):
            found, trust = record.get("origin"), record.get("trust")
            if found not in tables.get(table, []):
                problems.append(f"{name}: {record.get('id')}: origin {found!r} not in {table}")
            if corpus_origins.get(found) != trust:
                problems.append(f"{name}: {record.get('id')}: trust {trust!r} for {found!r}")

    # Attack documents: an untrusted corpus origin, and a prefix no generated id uses.
    if corpus_origins.get(facts["attack_origin"]) != "untrusted":
        problems.append(f"attack_origin {facts['attack_origin']!r}: not an untrusted corpus origin")
    if facts["attack_origin"] not in tables.get("documents", []):
        problems.append(f"attack_origin {facts['attack_origin']!r}: not a documents origin")
    if prefix != prefix.casefold() or not prefix.endswith("-"):
        problems.append(f"attack_id_prefix {prefix!r}: not lower case, or no trailing '-'")
    problems += [
        f"{item}: starts with the attack-id prefix {prefix!r}"
        for item in corpus_ids(corpus)
        if item.split("=", 1)[1].casefold().startswith(prefix.casefold())
    ]
    return problems


def _adr_0010() -> str:
    found = sorted(DECISIONS.glob("0010-*.md")) if DECISIONS.is_dir() else []
    assert len(found) == 1, f"expected one ADR 0010 in docs/decisions, found {len(found)}"
    return found[0].read_text(encoding="utf-8")


def test_adr_0010_vocabulary() -> None:
    corpus = generate.build()
    # Non-vacuity: the prefix check reads every advisor, mandate, document and sheet id.
    counts = {name: len(records) for name, records in corpus.items()}
    assert counts["documents.jsonl"] and counts["product_sheets.jsonl"], counts
    without = [
        f"{name}: {record}"
        for name, records in corpus.items()
        for record in records
        if not any(isinstance(record.get(field), str) for field in ID_FIELDS)
    ]
    assert not without, f"records with no id field: {without[:3]}"
    problems = adr_0010_problems(_adr_0010(), generate.ORIGIN, generate.trust_for, corpus)
    assert not problems, "ADR 0010 and data/synthetic/generate.py:\n" + "\n".join(problems)


GOOD_0010 = """\
```yaml
corpus_origins:
  kyc_register: internal
  portfolio_system: internal
  call_notes: internal
  mailbox: untrusted
  product_catalogue: internal
runtime_origins:
  agent_draft: untrusted
  agent_followup: untrusted
tables:
  documents: [kyc_register, portfolio_system, call_notes, mailbox]
  product_sheets: [product_catalogue]
  drafts: [agent_draft]
  followups: [agent_followup]
attack_id_prefix: "rt-"
attack_origin: mailbox
```
"""


def _planted_corpus(*, mail_id: str = "m1-mail-1", mandate_id: str = "m1") -> Corpus:
    def doc(doc_id: str, doc_type: str, **more: object) -> dict[str, object]:
        record = {"id": doc_id, "origin": generate.ORIGIN[doc_type]}
        return record | {"trust": generate.trust_for(doc_type)} | more

    return {
        "advisors.jsonl": [{"advisor_id": "a1"}],
        "mandates.jsonl": [{"advisor_id": "a1", "id": mandate_id}],
        "documents.jsonl": [
            doc("m1-kyc", "kyc", advisor_id="a1", mandate_id=mandate_id),
            doc(mail_id, "email", advisor_id="a1", mandate_id=mandate_id),
        ],
        "product_sheets.jsonl": [doc("product-x", "product_sheet")],
    }


def _problems_0010(adr: str, corpus: Corpus | None = None) -> list[str]:
    planted = corpus if corpus is not None else _planted_corpus()
    return adr_0010_problems(adr, generate.ORIGIN, generate.trust_for, planted)


def test_planted_adr_0010_blocks() -> None:
    assert _problems_0010(GOOD_0010) == []
    # The block: exactly one visible yaml block, yaml, a mapping, the five keys.
    assert _problems_0010("no block\n") == ["expected one yaml block, found 0"]
    assert _problems_0010(GOOD_0010 + GOOD_0010) == ["expected one yaml block, found 2"]
    assert _problems_0010(f"<!--\n{GOOD_0010}-->\n") == ["expected one yaml block, found 0"]
    assert _problems_0010(GOOD_0010.replace("```yaml", "```text")) == [
        "expected one yaml block, found 0"
    ]
    assert _problems_0010("```yaml\nx: [1\n```\n") == ["the yaml block is not yaml"]
    assert _problems_0010("```yaml\n- a\n```\n") == ["the yaml block is not a mapping"]
    assert _problems_0010(GOOD_0010.replace('attack_id_prefix: "rt-"\n', "")) == [
        "attack_id_prefix: not in the block"
    ]
    assert _problems_0010(GOOD_0010.replace("attack_origin:", "extra: 1\nattack_origin:")) == [
        "extra: in the block, not expected"
    ]
    assert _problems_0010(GOOD_0010.replace('"rt-"', '""')) == [
        "attack_id_prefix: not a non-empty string"
    ]
    runtime = "runtime_origins:\n  agent_draft: untrusted\n  agent_followup: untrusted\n"
    assert _problems_0010(GOOD_0010.replace(runtime, "runtime_origins: []\n")) == [
        "runtime_origins: not a non-empty mapping"
    ]

    # Trust: the generator's for the corpus, untrusted for every runtime origin.
    mail_internal = GOOD_0010.replace("mailbox: untrusted", "mailbox: internal")
    assert _problems_0010(mail_internal)[0].startswith("corpus_origins: {")
    assert "documents.jsonl: m1-mail-1: trust 'untrusted' for 'mailbox'" in _problems_0010(
        mail_internal
    )
    assert _problems_0010(GOOD_0010.replace("agent_draft: untrusted", "agent_draft: internal")) == [
        "runtime origin 'agent_draft': 'internal', not 'untrusted'"
    ]
    assert _problems_0010(GOOD_0010.replace("agent_draft: untrusted", "agent_draft: trusted")) == [
        f"origin 'agent_draft': trust 'trusted', not one of {list(TRUST)}",
        "runtime origin 'agent_draft': 'trusted', not 'untrusted'",
    ]
    both = GOOD_0010.replace(
        "  agent_followup: untrusted\n", "  agent_followup: untrusted\n  mailbox: untrusted\n", 1
    )
    assert "origin 'mailbox': both a corpus and a runtime origin" in _problems_0010(both)
    # A generator that gives two types one origin is reported, not silently merged.
    shared = generate.ORIGIN | {"call_note": "mailbox"}
    assert "the generator gives two document types one origin" in adr_0010_problems(
        GOOD_0010, shared, generate.trust_for, _planted_corpus()
    )

    # Tables: the four text tables, each origin in exactly one.
    three = sorted(["documents", "product_sheets", "drafts"])
    assert _problems_0010(GOOD_0010.replace("  followups: [agent_followup]\n", "")) == [
        f"tables: {three}, expected {sorted(TEXT_TABLES)}",
        "origin 'agent_followup': in no table, expected exactly one",
    ]
    twice = GOOD_0010.replace("drafts: [agent_draft]", "drafts: [agent_draft, mailbox]")
    assert _problems_0010(twice) == [
        "origin 'mailbox': in ['documents', 'drafts'], expected exactly one"
    ]
    assert _problems_0010(
        GOOD_0010.replace("drafts: [agent_draft]", "drafts: [agent_draft, x]")
    ) == ["tables: 'x' is no origin"]
    assert _problems_0010(GOOD_0010.replace("drafts: [agent_draft]", "drafts: agent_draft")) == [
        "tables.drafts: not a non-empty list",
        "origin 'agent_draft': in no table, expected exactly one",
    ]
    # A generated sheet whose origin the documents table would hold.
    moved = GOOD_0010.replace(
        "documents: [kyc_register, portfolio_system, call_notes, mailbox]",
        "documents: [kyc_register, portfolio_system, call_notes, mailbox, product_catalogue]",
    ).replace("product_sheets: [product_catalogue]", "product_sheets: [agent_draft]")
    assert "product_sheets.jsonl: product-x: origin 'product_catalogue' not in product_sheets" in (
        _problems_0010(moved)
    )

    # Attack documents: an untrusted origin, and a prefix no generated id uses.
    assert _problems_0010(
        GOOD_0010.replace("attack_origin: mailbox", "attack_origin: kyc_register")
    ) == ["attack_origin 'kyc_register': not an untrusted corpus origin"]
    assert _problems_0010(GOOD_0010.replace('"rt-"', '"RT-"')) == [
        "attack_id_prefix 'RT-': not lower case, or no trailing '-'"
    ]
    # The attack origin's documents table: an untrusted origin that only sheets hold.
    sheet_mail = GOOD_0010.replace(
        "documents: [kyc_register, portfolio_system, call_notes, mailbox]",
        "documents: [kyc_register, portfolio_system, call_notes]",
    ).replace("product_sheets: [product_catalogue]", "product_sheets: [product_catalogue, mailbox]")
    assert "attack_origin 'mailbox': not a documents origin" in _problems_0010(sheet_mail)
    # A repeated key is refused, wherever it sits: PyYAML alone would keep the last.
    assert _problems_0010(
        GOOD_0010.replace(
            "attack_origin: mailbox", "attack_origin: mailbox\nattack_origin: call_notes"
        )
    ) == ["the yaml block repeats a key: duplicate key 'attack_origin'"]
    assert _problems_0010(
        GOOD_0010.replace(
            "  mailbox: untrusted\n", "  mailbox: untrusted\n  mailbox: internal\n", 1
        )
    ) == ["the yaml block repeats a key: duplicate key 'mailbox'"]
    prefixed = "starts with the attack-id prefix 'rt-'"
    assert _problems_0010(GOOD_0010, _planted_corpus(mail_id="rt-m1-mail-1")) == [
        f"documents.jsonl:id=rt-m1-mail-1: {prefixed}"
    ]
    assert _problems_0010(GOOD_0010, _planted_corpus(mail_id="RT-m1-mail-1")) == [
        f"documents.jsonl:id=RT-m1-mail-1: {prefixed}"
    ]
    assert _problems_0010(GOOD_0010, _planted_corpus(mandate_id="rt-x")) == [
        f"documents.jsonl:mandate_id=rt-x: {prefixed}",
        f"documents.jsonl:mandate_id=rt-x: {prefixed}",
        f"mandates.jsonl:id=rt-x: {prefixed}",
    ]
