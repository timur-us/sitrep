"""The pip-audit exceptions in the Makefile's `audit` target: recorded, and they expire.

An exception is an `--ignore-vuln <id>` that `make -n audit` hands to pip-audit. Each one:
- is in EXCEPTIONS below, with the package and the first version that fixes it;
- has a dated comment in the Makefile that names it, why it does not apply, and when it
  goes (the fixed version);
- goes as soon as uv.lock holds the fixed version or a later one: then this test fails,
  so the ignore cannot outlive the bump that makes it needless.

The ignores are read from `make -n audit`, so a comment or another target cannot pass
for one. `make -n` runs nothing: no network, no Docker.
"""

from __future__ import annotations

import os
import re
import subprocess
import tomllib
from typing import Any

import pytest
from repo_files import ROOT

MAKEFILE = ROOT / "Makefile"
LOCK = ROOT / "uv.lock"
# vulnerability id -> (package, the first version that fixes it)
EXCEPTIONS: dict[str, tuple[str, str]] = {
    # Fable's triage (2026-11-15): a ReDoS in TraceContextTextMapPropagator.extract(),
    # not reachable (the global propagator is empty, the model client's propagation is
    # off); the fix, published 2026-11-13, is inside pyproject's 7-day window.
    "CVE-2026-126360": ("opentelemetry-api", "1.45.2"),
}
IGNORE = re.compile(r"--ignore-vuln[ =](\S+)")
ISO_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
RELEASE = re.compile(r"(\d+(?:\.\d+)*)")


def dry_run_audit() -> str:
    env = {
        key: value for key, value in os.environ.items() if not key.startswith(("MAKE", "MFLAGS"))
    }
    result = subprocess.run(
        ["make", "--no-print-directory", "-n", "audit"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def ignored(recipe: str) -> set[str]:
    """The ids the audit recipe hands to pip-audit as --ignore-vuln."""
    return set(IGNORE.findall(recipe))


def release(version: str) -> tuple[int, ...] | None:
    """A version's numeric release, (1, 45, 2); None when it does not start with one."""
    found = RELEASE.match(version)
    return tuple(int(part) for part in found.group(1).split(".")) if found else None


def locked_versions(lock: dict[str, Any]) -> dict[str, str]:
    return {package["name"]: package["version"] for package in lock.get("package", [])}


def expiry_problems(
    ignores: set[str], exceptions: dict[str, tuple[str, str]], versions: dict[str, str]
) -> list[str]:
    """Ignores that are not recorded, or whose fix the lock already holds."""
    problems = [
        f"{vuln}: ignored, but not in EXCEPTIONS" for vuln in sorted(ignores - set(exceptions))
    ]
    for vuln in sorted(ignores & set(exceptions)):
        package, fixed = exceptions[vuln]
        locked = versions.get(package)
        if locked is None:
            problems.append(f"{vuln}: {package} is not in the lock; remove the ignore")
            continue
        have, need = release(locked), release(fixed)
        if have is None or need is None:
            problems.append(f"{vuln}: cannot compare {package} {locked} with {fixed}")
        elif have >= need:
            problems.append(
                f"{vuln}: the lock holds {package} {locked}, which fixes it (>= {fixed});"
                " remove the ignore from the Makefile and from EXCEPTIONS"
            )
    return problems


def record_problems(makefile: str, vuln: str, package: str, fixed: str) -> list[str]:
    """What the Makefile's comment for vuln lacks: a date, the reason, the removal."""
    comment = " ".join(line.lstrip("#").strip() for line in _comment_block(makefile, vuln))
    if not comment:
        return [f"{vuln}: no comment in the Makefile names it"]
    problems = []
    if not ISO_DATE.search(comment):
        problems.append(f"{vuln}: the comment has no date")
    if "not reachable" not in comment.lower():
        problems.append(f"{vuln}: the comment does not say why it does not apply")
    if not re.search(
        rf"\bremove\b.*\b{re.escape(package)}\b.*\b{re.escape(fixed)}\b", comment, re.IGNORECASE
    ):
        problems.append(f"{vuln}: the comment does not say it goes with {package} {fixed}")
    return problems


def _comment_block(makefile: str, vuln: str) -> list[str]:
    """The run of comment lines that starts with the one naming vuln."""
    lines = makefile.splitlines()
    for index, line in enumerate(lines):
        if line.startswith("#") and vuln in line:
            block = []
            for following in lines[index:]:
                if not following.startswith("#"):
                    break
                block.append(following)
            return block
    return []


# The repository.


def test_the_audit_ignores_exactly_the_recorded_exceptions() -> None:
    recipe = dry_run_audit()
    assert "pip-audit -r" in recipe, "control: the dry run is the audit recipe"
    assert ignored(recipe) == set(EXCEPTIONS), sorted(ignored(recipe) ^ set(EXCEPTIONS))


@pytest.mark.parametrize("vuln", sorted(EXCEPTIONS))
def test_each_exception_is_recorded_in_the_makefile(vuln: str) -> None:
    package, fixed = EXCEPTIONS[vuln]
    makefile = MAKEFILE.read_text(encoding="utf-8")
    assert record_problems(makefile, vuln, package, fixed) == []


def test_no_exception_outlives_its_fix() -> None:
    # The control for today: the lock holds opentelemetry-api 1.45.1, below the fix.
    versions = locked_versions(tomllib.loads(LOCK.read_text(encoding="utf-8")))
    assert versions.get("opentelemetry-api") == "1.45.1", "control: the lock this PR expects"
    assert expiry_problems(ignored(dry_run_audit()), EXCEPTIONS, versions) == []


# Planted inputs: each rule, reported.


PLANTED = {"CVE-2026-126360": ("opentelemetry-api", "1.45.2")}


@pytest.mark.parametrize(
    ("locked", "expired"),
    [("1.45.1", False), ("1.45.2", True), ("1.46.0", True), ("2.0", True), ("1.44.9", False)],
)
def test_planted_lock_versions(locked: str, expired: bool) -> None:
    problems = expiry_problems({"CVE-2026-126360"}, PLANTED, {"opentelemetry-api": locked})
    assert bool(problems) is expired, problems
    if expired:
        expected = (
            f"CVE-2026-126360: the lock holds opentelemetry-api {locked}, which fixes it"
            " (>= 1.45.2); remove the ignore from the Makefile and from EXCEPTIONS"
        )
        assert problems == [expected]


def test_planted_other_shapes() -> None:
    fixed = {"opentelemetry-api": "1.45.2"}
    # Without the ignore, the fixed lock is fine: nothing to remove.
    assert expiry_problems(set(), PLANTED, fixed) == []
    # An ignore nobody recorded.
    assert expiry_problems({"CVE-2026-1"}, PLANTED, fixed) == [
        "CVE-2026-1: ignored, but not in EXCEPTIONS"
    ]
    # The package left the lock.
    assert expiry_problems({"CVE-2026-126360"}, PLANTED, {}) == [
        "CVE-2026-126360: opentelemetry-api is not in the lock; remove the ignore"
    ]
    assert expiry_problems({"CVE-2026-126360"}, PLANTED, {"opentelemetry-api": "dev"}) == [
        "CVE-2026-126360: cannot compare opentelemetry-api dev with 1.45.2"
    ]


def test_planted_recipes() -> None:
    assert ignored('pip-audit -r "$req" --ignore-vuln CVE-2026-126360 --ignore-vuln=GHSA-x') == {
        "CVE-2026-126360",
        "GHSA-x",
    }
    assert ignored('pip-audit -r "$req" --require-hashes') == set()


GOOD_RECORD = (
    "# CVE-2026-126360 (2026-11-15): a ReDoS in opentelemetry-api. Not reachable: the\n"
    "# global propagator is empty. Remove when uv.lock holds opentelemetry-api 1.45.2.\n"
    "AUDIT_IGNORE := --ignore-vuln CVE-2026-126360\n"
)


def test_planted_records() -> None:
    args = ("CVE-2026-126360", "opentelemetry-api", "1.45.2")
    assert record_problems(GOOD_RECORD, *args) == []
    assert record_problems("AUDIT_IGNORE := --ignore-vuln CVE-2026-126360\n", *args) == [
        "CVE-2026-126360: no comment in the Makefile names it"
    ]
    assert record_problems(GOOD_RECORD.replace(" (2026-11-15)", ""), *args) == [
        "CVE-2026-126360: the comment has no date"
    ]
    assert record_problems(GOOD_RECORD.replace("Not reachable", "Harmless"), *args) == [
        "CVE-2026-126360: the comment does not say why it does not apply"
    ]
    assert record_problems(GOOD_RECORD.replace("1.45.2", "a later version"), *args) == [
        "CVE-2026-126360: the comment does not say it goes with opentelemetry-api 1.45.2"
    ]
