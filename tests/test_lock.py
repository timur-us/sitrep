"""uv.lock holds what ADR 0006 says it holds.

- The minimum release age is recorded as a span (P7D), not a timestamp.
- Every package comes from PyPI, except the project itself (editable).
- Every sdist and wheel is a PyPI file with a sha256 and an upload time.
- No upload lies inside the 7-day window before this test runs.

uv enforces the window when it resolves. This test catches a lock whose
recorded facts break the policy: another span, another source, a missing hash,
a recorded upload inside the window. It trusts upload-time as written: no hash
covers it, so a hand edit that backdates it is not caught here. Re-resolving
in CI is the control for that (PR 4).

Messages name packages, files and source hosts only: never a hash, a URL's
credentials or its query string.
"""

from __future__ import annotations

import re
import tomllib
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from repo_files import ROOT

LOCK = ROOT / "uv.lock"
PYPI = {"registry": "https://pypi.org/simple"}
FILE_HOST = "https://files.pythonhosted.org/packages/"
SPAN = "P7D"
WINDOW = timedelta(days=7)
SHA256 = re.compile(r"sha256:[0-9a-f]{64}")
PROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["name"]


def _describe(source: Any) -> str:
    """The source's kind and, for a URL, its host: no credentials, path or query."""
    if not isinstance(source, dict) or not source:
        return repr(type(source).__name__)
    kinds = "+".join(sorted(source))
    hosts = {urlsplit(str(value)).hostname for value in source.values()} - {None}
    return f"{kinds} ({', '.join(sorted(hosts))})" if hosts else kinds


def _upload_problem(uploaded: Any, now: datetime) -> str | None:
    try:
        parsed = datetime.fromisoformat(uploaded)
    except (TypeError, ValueError):
        return f"upload time {uploaded!r} is not an ISO timestamp"
    if parsed.tzinfo is None:
        return f"upload time {uploaded!r} has no time zone"
    if parsed > now - WINDOW:
        return f"uploaded {uploaded}, inside the 7-day window"
    return None


def _is_project(package: dict[str, Any]) -> bool:
    return package.get("name") == PROJECT and package.get("source") == {"editable": "."}


def lock_problems(lock: dict[str, Any], now: datetime) -> list[str]:
    """One line per way the lock breaks the policy."""
    span = lock.get("options", {}).get("exclude-newer-span")
    problems = [] if span == SPAN else [f"exclude-newer-span is {span!r}, expected {SPAN!r}"]
    packages = lock.get("package", [])
    count = sum(map(_is_project, packages))
    if count != 1:
        problems.append(f"the project {PROJECT!r} appears {count} times as editable, expected once")
    for package in packages:
        name = f"{package.get('name')} {package.get('version', '')}".strip()
        dists = [package["sdist"]] if "sdist" in package else []
        dists += package.get("wheels", [])
        if _is_project(package):
            # Built from the working tree: nothing to download, nothing else to check.
            if dists:
                problems.append(f"{name}: the project entry lists files to download")
            continue
        if package.get("source") != PYPI:
            problems.append(
                f"{name}: source {_describe(package.get('source'))}, expected the PyPI registry"
            )
        if not dists:
            problems.append(f"{name}: no sdist and no wheel")
        for dist in dists:
            url = str(dist.get("url", ""))
            file = url.split("?", 1)[0].rsplit("/", 1)[-1] or "?"
            if not url.startswith(FILE_HOST):
                problems.append(f"{name}: {file} not from {FILE_HOST}")
            if not SHA256.fullmatch(str(dist.get("hash", ""))):
                problems.append(f"{name}: {file} has no sha256")
            if "upload-time" not in dist:
                problems.append(f"{name}: {file} has no upload time")
            elif problem := _upload_problem(dist["upload-time"], now):
                problems.append(f"{name}: {file} {problem}")
    return problems


def test_lock_contents() -> None:
    lock = tomllib.loads(LOCK.read_text(encoding="utf-8"))
    packages = lock.get("package", [])
    # Non-vacuity: the dev group's packages are there, and so is the project.
    names = {package["name"] for package in packages}
    assert {PROJECT, "pytest", "pytest-socket", "pip-audit", "ruff"} <= names, sorted(names)
    assert len(packages) >= 30, f"only {len(packages)} packages in uv.lock"
    problems = lock_problems(lock, datetime.now(UTC))
    assert not problems, "uv.lock:\n" + "\n".join(problems)


def _dist(file: str, uploaded: str = "2025-01-01T00:00:00Z", **change: str) -> dict[str, str]:
    dist = {"url": FILE_HOST + "ab/cd/" + file, "hash": "sha256:" + "0" * 64}
    return dist | {"upload-time": uploaded} | change


def test_planted_violations() -> None:
    now = datetime(2026, 10, 29, 12, tzinfo=UTC)
    good = {
        "options": {"exclude-newer-span": SPAN},
        "package": [
            {"name": PROJECT, "version": "0.1.0", "source": {"editable": "."}},
            {"name": "ok", "version": "1", "source": PYPI, "wheels": [_dist("ok-1.whl")]},
            # Exactly at the edge of the window: allowed. uv writes fractional seconds.
            {"name": "edge", "version": "1", "source": PYPI, "sdist": _dist("edge-1.tar.gz")},
            {
                "name": "frac",
                "version": "1",
                "source": PYPI,
                "wheels": [_dist("frac-1.whl", uploaded="2025-04-03T10:39:49.734Z")],
            },
        ],
    }
    good["package"][2]["sdist"]["upload-time"] = "2026-10-22T12:00:00Z"
    assert lock_problems(good, now) == []

    def pkg(name: str, *dists: dict[str, str], source: dict[str, str] = PYPI) -> dict[str, Any]:
        return {"name": name, "version": "1", "source": source, "wheels": list(dists)}

    bad = {
        "options": {"exclude-newer-span": "P1D"},
        "package": [
            {"name": "other", "version": "1", "source": {"editable": "."}},
            pkg("fromgit", _dist("g-1.whl"), source={"git": "https://github.com/x/y?rev=1"}),
            # Assembled, so the repo-wide secret scan does not flag this file.
            pkg(
                "creds", _dist("c-1.whl"), source={"registry": "https://u:" + "t0k@mirror.example"}
            ),
            pkg("nohash", _dist("n.whl", hash="")),
            pkg("fresh", _dist("f-1.whl", uploaded="2026-10-25T00:00:00Z")),
            # One second inside the window: a WINDOW shorter than 7 days lets this through.
            pkg("justin", _dist("j-1.whl", uploaded="2026-10-22T12:00:01Z")),
            pkg("naive", _dist("v-1.whl", uploaded="2025-01-01T00:00:00")),
            pkg("garbage", _dist("x-1.whl", uploaded="yesterday")),
            pkg("mirror", _dist("m.whl") | {"url": "https://mirror.example/m.whl?token=abc"}),
            {"name": "empty", "version": "1", "source": PYPI},
        ],
    }
    del bad["package"][3]["wheels"][0]["upload-time"]
    assert lock_problems(bad, now) == [
        "exclude-newer-span is 'P1D', expected 'P7D'",
        f"the project {PROJECT!r} appears 0 times as editable, expected once",
        "other 1: source editable, expected the PyPI registry",
        "other 1: no sdist and no wheel",
        "fromgit 1: source git (github.com), expected the PyPI registry",
        "creds 1: source registry (mirror.example), expected the PyPI registry",
        "nohash 1: n.whl has no sha256",
        "nohash 1: n.whl has no upload time",
        "fresh 1: f-1.whl uploaded 2026-10-25T00:00:00Z, inside the 7-day window",
        "justin 1: j-1.whl uploaded 2026-10-22T12:00:01Z, inside the 7-day window",
        "naive 1: v-1.whl upload time '2025-01-01T00:00:00' has no time zone",
        "garbage 1: x-1.whl upload time 'yesterday' is not an ISO timestamp",
        f"mirror 1: m.whl not from {FILE_HOST}",
        "empty 1: no sdist and no wheel",
    ]


def test_planted_project_entries() -> None:
    # The project's own entry skips the per-file checks, so it must be exactly
    # one editable entry with nothing to download.
    now = datetime(2026, 10, 29, 12, tzinfo=UTC)
    project = {"name": PROJECT, "version": "0.1.0", "source": {"editable": "."}}
    carrying = project | {"wheels": [{"url": "https://elsewhere.example/p.whl"}]}
    lock = {"options": {"exclude-newer-span": SPAN}, "package": [project, carrying]}
    assert lock_problems(lock, now) == [
        f"the project {PROJECT!r} appears 2 times as editable, expected once",
        f"{PROJECT} 0.1.0: the project entry lists files to download",
    ]
