"""The runbook and the Makefile agree, in both directions.

Every public Makefile target has a "##" comment and appears in the runbook, and
every `make <target>` in the runbook's code exists. Prose outside code spans and
fenced blocks is not read.
"""

from __future__ import annotations

import re

from repo_files import ROOT

RUNBOOK = ROOT / "docs" / "runbooks" / "local-setup.md"

# A rule line: "target ...: prerequisites ## comment". Variable assignments
# (`:=`, `::=`, `export X := 1`) and special targets (".PHONY") do not match.
RULE = re.compile(r"^([A-Za-z0-9_][^:=#\n]*?)\s*:(?![:=])(.*)$", re.MULTILINE)
PHONY = re.compile(r"^\.PHONY\s*:(.*)$", re.MULTILINE)
# What `make help` greps for: one lower-case name at the start of a rule line.
HELP_NAME = re.compile(r"[a-z][a-z0-9-]*")
FENCE = re.compile(r"```.*?```", re.DOTALL)
INLINE = re.compile(r"`([^`\n]+)`")
MAKE = re.compile(r"(?<![\w-])make\s+([a-z][\w-]*)")


def rule_lines(text: str) -> list[tuple[list[str], bool]]:
    """(target names, whether the line carries a "## " comment, as `make help` greps it)."""
    return [(match.group(1).split(), "## " in match.group(2)) for match in RULE.finditer(text)]


def makefile_targets(text: str) -> dict[str, bool]:
    """Target name -> whether its rule line carries a "##" comment."""
    return {name: documented for names, documented in rule_lines(text) for name in names}


def runbook_targets(text: str) -> set[str]:
    code = FENCE.findall(text) + INLINE.findall(FENCE.sub("", text))
    return {match.group(1) for snippet in code for match in MAKE.finditer(snippet)}


def drift(makefile: str, runbook: str) -> list[str]:
    targets = makefile_targets(makefile)
    mentioned = runbook_targets(runbook)
    public = {name for name in targets if not name.startswith("_")}
    phony = {name for match in PHONY.finditer(makefile) for name in match.group(1).split()}
    problems = [
        f"{' '.join(names)}: one target per rule line, so that `make help` lists it"
        for names, _ in rule_lines(makefile)
        if len(names) > 1
    ]
    problems += [
        f"{name}: `make help` lists only names matching {HELP_NAME.pattern}"
        for name in public
        if not HELP_NAME.fullmatch(name)
    ]
    problems += [f"{name}: not in .PHONY" for name in public - phony]
    problems += [f"{name}: in .PHONY, no rule" for name in phony - targets.keys()]
    problems += [f"{name}: no ## comment in the Makefile" for name in public if not targets[name]]
    problems += [f"{name}: not in the runbook" for name in public if name not in mentioned]
    problems += [
        f"{name}: in the runbook, not in the Makefile" for name in mentioned - targets.keys()
    ]
    return sorted(problems)


def test_runbook_bidirectional() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    runbook = RUNBOOK.read_text(encoding="utf-8")
    assert "test" in makefile_targets(makefile), "the Makefile has no test target"
    assert "test" in runbook_targets(runbook), "the runbook never says `make test`"
    problems = drift(makefile, runbook)
    assert not problems, "runbook and Makefile drifted:\n" + "\n".join(problems)


def test_drift_is_detected() -> None:
    makefile = "X := 1\n.PHONY: a b\na: ## Documented\n\techo a\nb:\n\techo b\n_internal:\n"
    runbook = "Run `make a` and `make c`; make sure nothing else.\n\n```sh\nmake a\n```\n"
    assert drift(makefile, runbook) == [
        "b: no ## comment in the Makefile",
        "b: not in the runbook",
        "c: in the runbook, not in the Makefile",
    ]


def test_rule_shapes_that_make_help_misses() -> None:
    # `make help` greps one lower-case name per rule line; every public target is phony.
    makefile = ".PHONY: a Up\na: ## A\nc d: ## Both\nUp: ## Upper\ne: ## Not phony\n"
    runbook = "`make a`, `make c`, `make d`, `make e`\n"
    assert drift(makefile, runbook) == [
        "Up: `make help` lists only names matching [a-z][a-z0-9-]*",
        "Up: not in the runbook",
        "c d: one target per rule line, so that `make help` lists it",
        "c: not in .PHONY",
        "d: not in .PHONY",
        "e: not in .PHONY",
    ]


def test_comment_marker_needs_a_space() -> None:
    # `make help` greps for "## " with the space; "##x" is not listed.
    assert drift(".PHONY: f\nf:##no space\n", "`make f`") == ["f: no ## comment in the Makefile"]
