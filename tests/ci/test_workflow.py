"""The CI workflow and the Dependabot config, read statically.

Six jobs run on every pull request and on every push to main. Each one is a pinned
checkout, the exact uv, and one make target, so a gate runs the same way locally.
The workflow holds no secret, no cloud login and no write permission, and no step can
be skipped or allowed to fail. The required checks in the ruleset are the job ids.
"""

from __future__ import annotations

import copy
import re
import tomllib
from typing import Any

import pytest
import yaml
from repo_files import ROOT

WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
DEPENDABOT = ROOT / ".github" / "dependabot.yml"

JOB_TARGETS = {
    "lint-test": "make lint-test",
    "pip-audit": "make audit",
    "docker-build": "make docker-build",
    "web-test": "make web-test",
    "evals": "make evals",
    "redteam": "make redteam",
}
TIMEOUTS = {
    "lint-test": 15,
    "pip-audit": 10,
    "docker-build": 15,
    "web-test": 10,
    "evals": 10,
    "redteam": 10,
}
CONCURRENCY = {
    "group": "ci-${{ github.ref }}",
    "cancel-in-progress": "${{ github.event_name == 'pull_request' }}",
}
TOP_LEVEL = {"name", "on", "permissions", "concurrency", "jobs"}
JOB_KEYS = {"runs-on", "timeout-minutes", "steps"}
STEP_KEYS = {"uses", "with", "run"}
PINNED = re.compile(r"(?P<action>[\w.-]+/[\w.-]+)@[0-9a-f]{40}")
USES = re.compile(r"uses:\s*(?P<ref>\S+)")
VERSION_COMMENT = re.compile(r"uses:\s*\S+\s+#\s*v\d+\.\d+\.\d+\s*$")
BANNED_TEXT = (
    "secrets.",
    "id-token",
    "azure/",
    "aws-actions/",
    "google-github-actions/",
    "az login",
    "gcloud auth",
    "pull_request_target",
    "workflow_run",
    "issue_comment",
)
BANNED_KEY = re.compile(r".*_KEY|.*_TOKEN|.*SECRET.*|ANTHROPIC.*", re.IGNORECASE)
# The only container ever allowed, and only on web-test: the fallback for a Chromium
# that cannot start on the runner (plan, PR 4).
PLAYWRIGHT_IMAGE = re.compile(
    r"mcr\.microsoft\.com/playwright/python:v(?P<version>\d+\.\d+\.\d+)-noble@sha256:[0-9a-f]{64}"
)


def load(text: str) -> dict[str, Any]:
    return yaml.safe_load(text)


def triggers(workflow: dict[str, Any]) -> Any:
    # YAML 1.1 reads a bare `on` as the boolean True.
    return workflow.get("on", workflow.get(True))


def uv_image_version() -> str:
    match = re.search(
        r"ghcr\.io/astral-sh/uv:(\d+\.\d+\.\d+)@", (ROOT / "Dockerfile").read_text("utf-8")
    )
    assert match, "the Dockerfile names no uv image"
    return match.group(1)


def locked_version(package: str) -> str:
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    versions = [entry["version"] for entry in lock["package"] if entry["name"] == package]
    assert len(versions) == 1, f"{package}: {versions}"
    return versions[0]


def _keys(node: Any) -> list[str]:
    """Every mapping key in the document, at any depth."""
    if isinstance(node, dict):
        return [str(key) for key in node] + [key for value in node.values() for key in _keys(value)]
    if isinstance(node, list):
        return [key for item in node for key in _keys(item)]
    return []


def _step_problems(job: str, steps: Any, uv_version: str) -> list[str]:
    if not isinstance(steps, list) or len(steps) != 3:
        return [f"{job}: steps are not exactly checkout, setup-uv and one run"]
    if not all(isinstance(step, dict) for step in steps):
        return [f"{job}: a step is not a mapping"]
    problems = []
    checkout, setup_uv, run = steps
    for step in steps:
        extra = set(step) - STEP_KEYS
        if extra:
            problems.append(f"{job}: step keys {sorted(extra)}")
    want_checkout = {"persist-credentials": False}
    if job == "lint-test":
        want_checkout["fetch-depth"] = 0  # the secret scan reads the branch's commits
    if PINNED.fullmatch(str(checkout.get("uses", ""))) is None or not str(
        checkout.get("uses")
    ).startswith("actions/checkout@"):
        problems.append(f"{job}: step 1 is not actions/checkout pinned to a commit")
    if checkout.get("with") != want_checkout:
        problems.append(f"{job}: checkout with {checkout.get('with')}, expected {want_checkout}")
    uses = str(setup_uv.get("uses", ""))
    if PINNED.fullmatch(uses) is None or not uses.startswith("astral-sh/setup-uv@"):
        problems.append(f"{job}: step 2 is not astral-sh/setup-uv pinned to a commit")
    want_uv = {"version": uv_version, "enable-cache": False}
    if setup_uv.get("with") != want_uv:
        problems.append(f"{job}: setup-uv with {setup_uv.get('with')}, expected {want_uv}")
    if run.get("run") != JOB_TARGETS.get(job) or set(run) != {"run"}:
        problems.append(f"{job}: runs {run.get('run')!r}, expected {JOB_TARGETS.get(job)!r}")
    return problems


def problems(
    workflow: dict[str, Any], raw: str, uv_version: str, playwright_version: str
) -> list[str]:
    """One line per way the workflow breaks the plan's CI rules."""
    found: list[str] = []
    extra = set(workflow) - TOP_LEVEL - {True}
    if extra:
        found.append(f"top-level keys {sorted(map(str, extra))}")
    on = triggers(workflow)
    if not isinstance(on, dict) or set(on) != {"pull_request", "push"}:
        found.append(f"triggers {on!r}, expected pull_request and push")
    else:
        if on["pull_request"] not in (None, {}):
            found.append(f"pull_request is filtered: {on['pull_request']!r}")
        if on["push"] != {"branches": ["main"]}:
            found.append(f"push is {on['push']!r}, expected branches [main] and no filter")
    if workflow.get("permissions") != {"contents": "read"}:
        found.append(f"permissions {workflow.get('permissions')!r}, expected contents: read")
    if workflow.get("concurrency") != CONCURRENCY:
        found.append(f"concurrency {workflow.get('concurrency')!r}")

    jobs = workflow.get("jobs") or {}
    if set(jobs) != set(JOB_TARGETS):
        found.append(f"jobs {sorted(jobs)}, expected {sorted(JOB_TARGETS)}")
    for name, job in jobs.items():
        keys = set(job)
        container = job.get("container")
        if container is not None:
            match = PLAYWRIGHT_IMAGE.fullmatch(str(container))
            if name != "web-test" or not match or match["version"] != playwright_version:
                found.append(f"{name}: container {container!r} is not the allowed fallback")
            keys.discard("container")
        if keys != JOB_KEYS:
            found.append(f"{name}: job keys {sorted(keys)}, expected {sorted(JOB_KEYS)}")
        if job.get("runs-on") != "ubuntu-24.04":
            found.append(f"{name}: runs-on {job.get('runs-on')!r}")
        if job.get("timeout-minutes") != TIMEOUTS.get(name):
            found.append(f"{name}: timeout-minutes {job.get('timeout-minutes')!r}")
        found += _step_problems(name, job.get("steps"), uv_version)
        for step in job.get("steps") or []:
            if isinstance(step, dict) and "${{" in str(step.get("run", "")):
                found.append(f"{name}: an expression inside run")

    found += [
        f"key {key!r} looks like a credential"
        for key in _keys(workflow)
        if BANNED_KEY.fullmatch(key)
    ]
    found += [f"text contains {word!r}" for word in BANNED_TEXT if word in raw]
    for number, line in enumerate(raw.splitlines(), start=1):
        if "uses:" not in line or line.lstrip().startswith("#"):
            continue
        ref = USES.search(line)
        if not ref or not PINNED.fullmatch(ref["ref"]):
            found.append(f"line {number}: uses without a commit SHA")
        if not VERSION_COMMENT.search(line):
            found.append(f"line {number}: uses without a version comment")
    return found


def _real() -> tuple[dict[str, Any], str]:
    raw = WORKFLOW.read_text(encoding="utf-8")
    return load(raw), raw


def test_workflow() -> None:
    workflow, raw = _real()
    found = problems(workflow, raw, uv_image_version(), locked_version("playwright"))
    assert not found, "ci.yml:\n" + "\n".join(found)
    # Non-vacuity: six jobs, each with a pinned checkout, a pinned setup-uv and a target.
    assert len(re.findall(r"uses:\s*\S+@[0-9a-f]{40}\s+#", raw)) == 12, raw


def test_check_names_are_the_job_ids() -> None:
    # A job without `name:` reports its id as the check name, which the ruleset requires.
    workflow, _ = _real()
    named = [job for job, body in (workflow.get("jobs") or {}).items() if "name" in body]
    assert not named, f"jobs with a display name: {named}"


def test_setup_uv_matches_the_dockerfile() -> None:
    # ADR 0006: the build toolchain is pinned by exact version, in both places.
    workflow, _ = _real()
    versions = {
        step["with"]["version"]
        for job in (workflow.get("jobs") or {}).values()
        for step in job.get("steps", [])
        if str(step.get("uses", "")).startswith("astral-sh/setup-uv@")
    }
    assert versions == {uv_image_version()}, f"setup-uv {versions}, Dockerfile {uv_image_version()}"


def _mutate(path: tuple[Any, ...], value: Any) -> dict[str, Any]:
    workflow = copy.deepcopy(_real()[0])
    node = workflow
    for key in path[:-1]:
        found = node.get(key) if isinstance(node, dict) else None
        if isinstance(node, list) and isinstance(key, int) and key < len(node):
            found = node[key]
        if found is None:
            pytest.fail(f"ci.yml has no {'.'.join(map(str, path[:-1]))} to plant into")
        node = found
    if value is DELETE:
        if path[-1] not in node:
            pytest.fail(f"ci.yml has no {'.'.join(map(str, path))} to delete")
        del node[path[-1]]
    else:
        node[path[-1]] = value
    return workflow


DELETE = object()
LINT = ("jobs", "lint-test")
WEB = ("jobs", "web-test")
DIGEST = "@sha256:" + "0" * 64
PLAYWRIGHT_REPO = "mcr.microsoft.com/playwright/python"
PLAYWRIGHT = locked_version("playwright")

PLANTED = [
    (("on",), {"pull_request_target": None, "push": {"branches": ["main"]}}, "triggers"),
    (("on",), {"pull_request": {"paths": ["app/**"]}, "push": {"branches": ["main"]}}, "filtered"),
    (("on",), {"pull_request": None, "push": None}, "push is None"),
    (("permissions",), {"contents": "write"}, "permissions"),
    (("permissions",), DELETE, "permissions"),
    ((*LINT, "permissions"), {"contents": "read"}, "job keys"),
    (("concurrency", "cancel-in-progress"), True, "concurrency"),
    ((*LINT, "if"), "github.event_name == 'push'", "job keys"),
    ((*LINT, "continue-on-error"), True, "job keys"),
    ((*LINT, "services"), {"db": {"image": "postgres"}}, "job keys"),
    ((*LINT, "env"), {"ANTHROPIC_API_KEY": "x"}, "looks like a credential"),
    ((*LINT, "container"), "python:3.12", "not the allowed fallback"),
    ((*WEB, "container"), "mcr.microsoft.com/playwright/python:v1.64.1-noble", "fallback"),
    ((*WEB, "container"), f"mcr.microsoft.com/playwright/python:v0.0.1-noble{DIGEST}", "fallback"),
    ((*WEB, "timeout-minutes"), 30, "timeout-minutes"),
    ((*WEB, "runs-on"), "ubuntu-latest", "runs-on"),
    ((*LINT, "steps", 2, "run"), "make test || true", "runs"),
    ((*LINT, "steps", 2, "run"), "make lint-test ${{ github.head_ref }}", "expression inside run"),
    ((*LINT, "steps", 2, "if"), "always()", "step keys"),
    ((*LINT, "steps", 2, "continue-on-error"), True, "step keys"),
    ((*LINT, "steps", 0, "with", "persist-credentials"), True, "checkout with"),
    ((*LINT, "steps", 0, "with", "fetch-depth"), 1, "checkout with"),
    ((*LINT, "steps", 1, "with", "version"), "latest", "setup-uv with"),
    ((*LINT, "steps", 1, "with", "enable-cache"), True, "setup-uv with"),
    ((*LINT, "steps", 0, "uses"), "actions/checkout@v7", "pinned to a commit"),
    ((*LINT, "steps", 1, "uses"), "astral-sh/setup-uv@v10.3.2", "setup-uv pinned to a commit"),
    ((*LINT, "steps", 2), "make lint-test", "not a mapping"),
    ((*LINT, "container"), f"{PLAYWRIGHT_REPO}:v{PLAYWRIGHT}-noble{DIGEST}", "allowed fallback"),
    (("jobs", "deploy"), {"runs-on": "ubuntu-24.04"}, "jobs"),
    (("jobs", "redteam"), DELETE, "jobs"),
]


@pytest.mark.parametrize(("path", "value", "needle"), PLANTED)
def test_planted_workflows_are_caught(path: tuple[Any, ...], value: Any, needle: str) -> None:
    _, raw = _real()
    found = problems(_mutate(path, value), raw, uv_image_version(), locked_version("playwright"))
    assert any(needle in line for line in found), found


def test_the_fallback_container_is_allowed_on_web_test_only() -> None:
    _, raw = _real()
    playwright = locked_version("playwright")
    image = f"mcr.microsoft.com/playwright/python:v{playwright}-noble{DIGEST}"
    found = problems(_mutate((*WEB, "container"), image), raw, uv_image_version(), playwright)
    assert not [line for line in found if "container" in line], found


@pytest.mark.parametrize(
    ("line", "needle"),
    [
        ("      - uses: actions/checkout@v7.0.1\n", "without a commit SHA"),
        ("      - uses: astral-sh/setup-uv@v10.3.2 # v10.3.2\n", "without a commit SHA"),
        ("      - uses: actions/checkout@" + "a" * 40 + "\n", "without a version comment"),
        ("# run: echo ${{ secrets.X }}\n", "secrets."),
        ("# permissions: id-token: write\n", "id-token"),
        ("#      - uses: azure/login@x\n", "azure/"),
        ("# run: az login --identity\n", "az login"),
        ("# on: issue_comment\n", "issue_comment"),
    ],
)
def test_planted_raw_text_is_caught(line: str, needle: str) -> None:
    workflow, raw = _real()
    found = problems(workflow, raw + line, uv_image_version(), locked_version("playwright"))
    assert any(needle in item for item in found), found


# docker-compose: the pgvector image in compose.yaml (PR 5a).
ECOSYSTEMS = {"github-actions", "uv", "docker", "docker-compose"}
UPDATE_KEYS = {"package-ecosystem", "directory", "schedule", "cooldown", "groups"}


def dependabot_problems(config: dict[str, Any]) -> list[str]:
    found = []
    if set(config) != {"version", "updates"}:
        found.append(f"top-level keys {sorted(config)}")
    if config.get("version") != 2:
        found.append(f"version {config.get('version')!r}")
    updates = config.get("updates") or []
    ecosystems = [update.get("package-ecosystem") for update in updates]
    if sorted(ecosystems) != sorted(ECOSYSTEMS):
        found.append(f"ecosystems {ecosystems}, expected {sorted(ECOSYSTEMS)}")
    for update in updates:
        name = update.get("package-ecosystem")
        if set(update) != UPDATE_KEYS:
            found.append(
                f"{name}: keys {sorted(set(update) - UPDATE_KEYS)} beyond the reviewed set"
            )
        if update.get("directory") != "/":
            found.append(f"{name}: directory {update.get('directory')!r}")
        if update.get("schedule") != {"interval": "weekly"}:
            found.append(f"{name}: schedule {update.get('schedule')!r}")
        if update.get("cooldown") != {"default-days": 7}:
            found.append(f"{name}: cooldown {update.get('cooldown')!r}, expected 7 days")
        groups = update.get("groups") or {}
        if not any(group.get("patterns") == ["*"] for group in groups.values()):
            found.append(f"{name}: not grouped")
    return found


def test_dependabot() -> None:
    config = yaml.safe_load(DEPENDABOT.read_text(encoding="utf-8"))
    found = dependabot_problems(config)
    assert not found, "dependabot.yml:\n" + "\n".join(found)


def test_ci_yml_is_the_only_workflow() -> None:
    # Another workflow could report a check under one of the six names, or run with
    # other permissions; tests/ci/ reads ci.yml only.
    names = sorted(path.name for path in WORKFLOW.parent.iterdir())
    assert names == ["ci.yml"], names


def test_dependabot_takes_no_other_keys() -> None:
    good = yaml.safe_load(DEPENDABOT.read_text(encoding="utf-8"))
    extra_top = dict(good, registries={"x": {"type": "python-index", "url": "https://x.test"}})
    entry = dict(good["updates"][0], ignore=[{"dependency-name": "*"}])
    extra_entry = dict(good, updates=[entry, *good["updates"][1:]])
    assert any("top-level keys" in line for line in dependabot_problems(extra_top))
    assert any("keys ['ignore']" in line for line in dependabot_problems(extra_entry))


def test_planted_dependabot_configs_are_caught() -> None:
    good = {
        "package-ecosystem": "uv",
        "directory": "/",
        "schedule": {"interval": "weekly"},
        "cooldown": {"default-days": 7},
        "groups": {"uv": {"patterns": ["*"]}},
    }
    pip = dict(good, **{"package-ecosystem": "pip"})
    short = dict(good, cooldown={"default-days": 3})
    daily = dict(good, schedule={"interval": "daily"})
    loose = {key: value for key, value in good.items() if key != "groups"}
    found = dependabot_problems({"version": 2, "updates": [pip, short, daily, loose]})
    for needle in ("ecosystems", "cooldown", "schedule", "not grouped"):
        assert any(needle in line for line in found), (needle, found)
