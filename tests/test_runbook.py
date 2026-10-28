"""The runbook and the Makefile agree, in both directions.

Every public Makefile target has a "##" comment and appears in the runbook, and
every `make <target>` in the runbook's code exists. Prose outside code spans and
fenced blocks is not read.
"""

from __future__ import annotations

import re

from repo_files import ROOT

RUNBOOK = ROOT / "docs" / "runbooks" / "local-setup.md"

# A rule line: "target: prerequisites ## comment". Variable assignments
# (`:=`, `::=`) and special targets (".PHONY") do not match.
RULE = re.compile(r"^([A-Za-z0-9][\w.-]*)\s*:(?![:=])(.*)$", re.MULTILINE)
FENCE = re.compile(r"```.*?```", re.DOTALL)
INLINE = re.compile(r"`([^`\n]+)`")
MAKE = re.compile(r"(?<![\w-])make\s+([a-z][\w-]*)")


def makefile_targets(text: str) -> dict[str, bool]:
    """Target name -> whether its rule line carries a "##" comment."""
    return {match.group(1): "##" in match.group(2) for match in RULE.finditer(text)}


def runbook_targets(text: str) -> set[str]:
    code = FENCE.findall(text) + INLINE.findall(FENCE.sub("", text))
    return {match.group(1) for snippet in code for match in MAKE.finditer(snippet)}


def drift(makefile: str, runbook: str) -> list[str]:
    targets = makefile_targets(makefile)
    mentioned = runbook_targets(runbook)
    public = {name for name in targets if not name.startswith("_")}
    problems = [f"{name}: no ## comment in the Makefile" for name in public if not targets[name]]
    problems += [f"{name}: not in the runbook" for name in public if name not in mentioned]
    problems += [
        f"{name}: in the runbook, not in the Makefile" for name in mentioned - targets.keys()
    ]
    return sorted(problems)


def test_runbook_bidirectional() -> None:
    makefile = (ROOT / "Makefile").read_text()
    runbook = RUNBOOK.read_text()
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
