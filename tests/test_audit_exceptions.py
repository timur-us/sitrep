"""The pip-audit exceptions in the Makefile's `audit` target: recorded, and they expire.

An exception is an `--ignore-vuln <id>` that `make -n audit` hands to pip-audit. Each one:
- is in EXCEPTIONS below, with the package and the first version that fixes it;
- has a dated comment in the Makefile that names it, why it does not apply, and when it
  goes (the fixed version);
- goes as soon as uv.lock holds the fixed version or a later one, or its review-by date
  has passed (a bump can stall: pyproject pins the OpenTelemetry packages exactly):
  then this test fails, so the ignore cannot outlive the bump that makes it needless;
- rests on a reachability test that is named in its record and fails if the vulnerable
  code runs (review sec-5).

The ignores are read from `make -n audit`, through pip-audit's own argument parser, so
an abbreviation (`--ignore`) or another spelling counts as well, and a comment or another
target cannot pass for one (review sec-1). `make -n` runs nothing: no network, no Docker.
"""

from __future__ import annotations

import datetime
import os
import re
import shlex
import subprocess
import tomllib
from typing import Any

import pytest
from fastapi.testclient import TestClient
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from opentelemetry.trace.span import TraceState
from packaging.version import InvalidVersion, Version
from pip_audit import _cli as pip_audit_cli
from repo_files import ROOT

from app.config.settings import Settings
from app.main import create_app

MAKEFILE = ROOT / "Makefile"
LOCK = ROOT / "uv.lock"
# vulnerability id -> (package, the first version that fixes it, the review-by date)
EXCEPTIONS: dict[str, tuple[str, str, datetime.date]] = {
    # Fable's triage (2026-11-15): a ReDoS in TraceContextTextMapPropagator.extract(),
    # not reachable (the global propagator is empty, the model client's propagation is
    # off); the fix, published 2026-11-13, is inside pyproject's 7-day window. The
    # review-by date gives the bump about four weeks after the window ends.
    "CVE-2026-126360": ("opentelemetry-api", "1.45.2", datetime.date(2026, 12, 15)),
}
ISO_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
# vulnerability id -> the test, in this file, that shows the code is not reached.
EVIDENCE = {"CVE-2026-126360": "test_the_propagator_extract_is_not_reached"}
# What the recipe may leave for the shell to expand: the temporary file only.
EXPANDED = re.compile(r"\$(?!req\b)|`")


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
    """The ids the audit recipe hands pip-audit to ignore, as pip-audit's own parser
    reads them: --ignore-vuln, and any abbreviation argparse accepts for it."""
    calls = [line for line in recipe.splitlines() if "pip-audit " in line]
    assert len(calls) == 1, f"expected one pip-audit call, found {len(calls)}"
    command = calls[0].partition("pip-audit ")[2]
    assert not EXPANDED.search(command), "the pip-audit call leaves something to the shell"
    arguments, _ = pip_audit_cli._parser().parse_known_args(shlex.split(command))
    return set(arguments.ignore_vulns or ())


def version(text: str) -> Version | None:
    try:
        return Version(text)
    except InvalidVersion:
        return None


def locked_versions(lock: dict[str, Any]) -> dict[str, str]:
    return {package["name"]: package["version"] for package in lock.get("package", [])}


def expiry_problems(
    ignores: set[str],
    exceptions: dict[str, tuple[str, str, datetime.date]],
    versions: dict[str, str],
    today: datetime.date,
) -> list[str]:
    """Ignores that are not recorded, whose fix the lock already holds, or whose
    review-by date has passed."""
    problems = [
        f"{vuln}: ignored, but not in EXCEPTIONS" for vuln in sorted(ignores - set(exceptions))
    ]
    for vuln in sorted(ignores & set(exceptions)):
        package, fixed, review_by = exceptions[vuln]
        if today > review_by:
            problems.append(
                f"{vuln}: past its review-by date {review_by.isoformat()}; bump {package} to"
                f" {fixed} or later, or review the exception and set a new date"
            )
        locked = versions.get(package)
        if locked is None:
            problems.append(f"{vuln}: {package} is not in the lock; remove the ignore")
            continue
        have, need = version(locked), version(fixed)
        if have is None or need is None:
            problems.append(f"{vuln}: cannot compare {package} {locked} with {fixed}")
        elif have >= need:
            problems.append(
                f"{vuln}: the lock holds {package} {locked}, which fixes it (>= {fixed});"
                " remove the ignore from the Makefile and from EXCEPTIONS"
            )
    return problems


def record_problems(
    makefile: str,
    vuln: str,
    package: str,
    fixed: str,
    review_by: datetime.date | None = None,
    evidence: str | None = None,
) -> list[str]:
    """What the Makefile's comment for vuln lacks: a date, the reason, the removal, and,
    when given, the review-by date and the reachability test."""
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
    if review_by is not None and review_by.isoformat() not in comment:
        problems.append(f"{vuln}: the comment does not name its review-by date")
    if evidence is not None and evidence not in comment:
        problems.append(f"{vuln}: the comment does not name its reachability test")
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

TODAY = datetime.date.today()


def test_the_audit_ignores_exactly_the_recorded_exceptions() -> None:
    recipe = dry_run_audit()
    assert "pip-audit -r" in recipe, "control: the dry run is the audit recipe"
    assert ignored(recipe) == set(EXCEPTIONS), sorted(ignored(recipe) ^ set(EXCEPTIONS))


@pytest.mark.parametrize("vuln", sorted(EXCEPTIONS))
def test_each_exception_is_recorded_in_the_makefile(vuln: str) -> None:
    package, fixed, review_by = EXCEPTIONS[vuln]
    makefile = MAKEFILE.read_text(encoding="utf-8")
    assert record_problems(makefile, vuln, package, fixed, review_by, EVIDENCE[vuln]) == []


def test_every_exception_names_a_reachability_test_that_exists() -> None:
    assert set(EVIDENCE) == set(EXCEPTIONS)
    for name in EVIDENCE.values():
        assert callable(globals().get(name)), f"{name} is not a test in this file"


def test_no_exception_outlives_its_fix() -> None:
    # Today's control: the ignore is active and the lock holds opentelemetry-api below
    # the fix (1.45.1), so nothing is reported. Once the lock holds 1.45.2 or later, or
    # after the review-by date, this fails and says what to do (review spec-1: no
    # exact-version pin hides that message).
    ignores = ignored(dry_run_audit())
    assert "CVE-2026-126360" in ignores, "control: the exception is active"
    versions = locked_versions(tomllib.loads(LOCK.read_text(encoding="utf-8")))
    assert "opentelemetry-api" in versions, "control: the package is in the lock"
    assert expiry_problems(ignores, EXCEPTIONS, versions, TODAY) == []


def test_the_propagator_extract_is_not_reached() -> None:
    # The reachability evidence for CVE-2026-126360, named in the Makefile's record
    # (review sec-5): the app, as create_app() builds it, answers a request that carries
    # W3C trace context without calling the vulnerable code. If a later setup (M3's
    # Application Insights, say) puts a W3C propagator back, this fails, and with it
    # the exception's premise.
    called: list[str] = []

    def refuse(name: str) -> Any:
        def raising(*args: Any, **kwargs: Any) -> Any:
            called.append(name)
            raise AssertionError(f"{name} was called")

        return raising

    patch = pytest.MonkeyPatch()
    patch.setattr(TraceContextTextMapPropagator, "extract", refuse("extract"))
    patch.setattr(TraceState, "from_header", classmethod(refuse("from_header")))
    try:
        app = create_app(Settings(_env_file=None))
        with TestClient(app, base_url="http://127.0.0.1:8000") as client:
            response = client.get(
                "/health",
                headers={
                    "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
                    "tracestate": "vendor=" + "a" * 200,
                },
            )
        # Control: the patch is live, so a W3C propagator would have tripped it.
        with pytest.raises(AssertionError, match="extract was called"):
            TraceContextTextMapPropagator().extract({"traceparent": "00-x"})
    finally:
        patch.undo()
    assert response.status_code == 200, response.status_code
    assert called == ["extract"], called  # the control's call only


# Planted inputs: each rule, reported.


BEFORE = datetime.date(2026, 11, 15)
PLANTED = {"CVE-2026-126360": ("opentelemetry-api", "1.45.2", datetime.date(2026, 12, 15))}


@pytest.mark.parametrize(
    ("locked", "expired"),
    [
        ("1.45.1", False),
        ("1.45.2", True),
        ("1.46.0", True),
        ("2.0", True),
        ("1.44.9", False),
        ("1.45.2rc1", False),
        ("1!1.0", True),
        ("1.45.2+local", True),
    ],
)
def test_planted_lock_versions(locked: str, expired: bool) -> None:
    problems = expiry_problems({"CVE-2026-126360"}, PLANTED, {"opentelemetry-api": locked}, BEFORE)
    assert bool(problems) is expired, problems
    if expired:
        expected = (
            f"CVE-2026-126360: the lock holds opentelemetry-api {locked}, which fixes it"
            " (>= 1.45.2); remove the ignore from the Makefile and from EXCEPTIONS"
        )
        assert problems == [expected]


@pytest.mark.parametrize(
    ("today", "expired"),
    [
        (datetime.date(2026, 12, 15), False),
        (datetime.date(2026, 12, 16), True),
    ],
    ids=["on the date", "the day after"],
)
def test_planted_review_by_dates(today: datetime.date, expired: bool) -> None:
    problems = expiry_problems({"CVE-2026-126360"}, PLANTED, {"opentelemetry-api": "1.45.1"}, today)
    message = (
        "CVE-2026-126360: past its review-by date 2026-12-15; bump opentelemetry-api to"
        " 1.45.2 or later, or review the exception and set a new date"
    )
    assert problems == ([message] if expired else [])


def test_planted_other_shapes() -> None:
    fixed = {"opentelemetry-api": "1.45.2"}
    # Without the ignore, the fixed lock is fine: nothing to remove.
    assert expiry_problems(set(), PLANTED, fixed, BEFORE) == []
    # An ignore nobody recorded.
    assert expiry_problems({"CVE-2026-1"}, PLANTED, fixed, BEFORE) == [
        "CVE-2026-1: ignored, but not in EXCEPTIONS"
    ]
    # The package left the lock.
    assert expiry_problems({"CVE-2026-126360"}, PLANTED, {}, BEFORE) == [
        "CVE-2026-126360: opentelemetry-api is not in the lock; remove the ignore"
    ]
    assert expiry_problems({"CVE-2026-126360"}, PLANTED, {"opentelemetry-api": "dev"}, BEFORE) == [
        "CVE-2026-126360: cannot compare opentelemetry-api dev with 1.45.2"
    ]


RECIPE = 'uv run pip-audit -r "$req" --require-hashes --disable-pip {}\n'


@pytest.mark.parametrize(
    ("flags", "ids"),
    [
        ("--ignore-vuln CVE-2026-126360 --ignore-vuln=GHSA-x", {"CVE-2026-126360", "GHSA-x"}),
        ("--ignore CVE-2026-1", {"CVE-2026-1"}),
        ("--ignore-v=CVE-2026-2", {"CVE-2026-2"}),
        ("--progress-spinner off", set()),
    ],
    ids=["spelled out", "abbreviated", "abbreviated with =", "none"],
)
def test_planted_recipes(flags: str, ids: set[str]) -> None:
    # Review sec-1: pip-audit accepts any unambiguous abbreviation of --ignore-vuln.
    assert ignored(RECIPE.format(flags)) == ids


@pytest.mark.parametrize(
    "flags",
    ["--ignore-vuln $$(cat ids)", "--ignore-vuln `cat ids`", "--ignore-vuln $IDS"],
    ids=["command substitution", "backticks", "a variable"],
)
def test_a_recipe_that_leaves_ids_to_the_shell_is_refused(flags: str) -> None:
    with pytest.raises(AssertionError, match="leaves something to the shell"):
        ignored(RECIPE.format(flags))


def test_a_second_pip_audit_call_is_refused() -> None:
    with pytest.raises(AssertionError, match="expected one pip-audit call, found 2"):
        ignored(RECIPE.format("") + RECIPE.format("--ignore X"))


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
    later = (*args, datetime.date(2026, 12, 15), "test_reach")
    assert record_problems(GOOD_RECORD, *later) == [
        "CVE-2026-126360: the comment does not name its review-by date",
        "CVE-2026-126360: the comment does not name its reachability test",
    ]
    full = GOOD_RECORD.replace("1.45.2.", "1.45.2, by 2026-12-15 (test_reach).")
    assert record_problems(full, *later) == []
