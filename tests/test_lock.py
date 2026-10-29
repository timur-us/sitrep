"""uv.lock holds what ADR 0006 says it holds.

- The minimum release age is recorded as a span (P7D), not a timestamp.
- Every package comes from PyPI, except the project itself (editable).
- Every sdist and wheel is a PyPI file with a sha256 and an upload time.
- No upload lies inside the 7-day window before this test runs.

uv enforces the window when it resolves; this test catches a lock that was
written another way (an override, a hand edit, another span).
"""

from __future__ import annotations

import re
import tomllib
from datetime import UTC, datetime, timedelta
from typing import Any

from repo_files import ROOT

LOCK = ROOT / "uv.lock"
PYPI = {"registry": "https://pypi.org/simple"}
FILE_HOST = "https://files.pythonhosted.org/packages/"
SPAN = "P7D"
WINDOW = timedelta(days=7)
SHA256 = re.compile(r"sha256:[0-9a-f]{64}")
PROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["name"]


def lock_problems(lock: dict[str, Any], now: datetime) -> list[str]:
    """One line per way the lock breaks the policy. Names packages and files, never hashes."""
    span = lock.get("options", {}).get("exclude-newer-span")
    problems = [] if span == SPAN else [f"exclude-newer-span is {span!r}, expected {SPAN!r}"]
    for package in lock.get("package", []):
        name = f"{package.get('name')} {package.get('version', '')}".strip()
        source = package.get("source")
        if package.get("name") == PROJECT and source == {"editable": "."}:
            continue
        if source != PYPI:
            problems.append(f"{name}: source {source}, expected {PYPI}")
        dists = [package["sdist"]] if "sdist" in package else []
        dists += package.get("wheels", [])
        if not dists:
            problems.append(f"{name}: no sdist and no wheel")
        for dist in dists:
            url = dist.get("url", "")
            file = url.rsplit("/", 1)[-1] or "?"
            if not url.startswith(FILE_HOST):
                problems.append(f"{name}: {file} not from {FILE_HOST}")
            if not SHA256.fullmatch(dist.get("hash", "")):
                problems.append(f"{name}: {file} has no sha256")
            uploaded = dist.get("upload-time")
            if uploaded is None:
                problems.append(f"{name}: {file} has no upload time")
            elif datetime.fromisoformat(uploaded) > now - WINDOW:
                problems.append(f"{name}: {file} uploaded {uploaded}, inside the 7-day window")
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
            # Exactly at the edge of the window: allowed.
            {"name": "edge", "version": "1", "source": PYPI, "sdist": _dist("edge-1.tar.gz")},
        ],
    }
    good["package"][2]["sdist"]["upload-time"] = "2026-10-22T12:00:00Z"
    assert lock_problems(good, now) == []

    git = {"git": "https://github.com/x/y?rev=1"}
    bad = {
        "options": {"exclude-newer-span": "P1D"},
        "package": [
            {"name": "other", "version": "1", "source": {"editable": "."}},
            {"name": "fromgit", "version": "1", "source": git, "wheels": [_dist("g-1.whl")]},
            {"name": "nohash", "version": "1", "source": PYPI, "wheels": [_dist("n.whl", hash="")]},
            {
                "name": "fresh",
                "version": "1",
                "source": PYPI,
                "sdist": _dist("f-1.tar.gz", uploaded="2026-10-25T00:00:00Z"),
            },
            {
                "name": "mirror",
                "version": "1",
                "source": PYPI,
                "wheels": [_dist("m.whl") | {"url": "https://mirror.example/m.whl"}],
            },
            {"name": "empty", "version": "1", "source": PYPI},
        ],
    }
    del bad["package"][2]["wheels"][0]["upload-time"]
    assert lock_problems(bad, now) == [
        "exclude-newer-span is 'P1D', expected 'P7D'",
        "other 1: source {'editable': '.'}, expected {'registry': 'https://pypi.org/simple'}",
        "other 1: no sdist and no wheel",
        f"fromgit 1: source {git}, expected {PYPI}",
        "nohash 1: n.whl has no sha256",
        "nohash 1: n.whl has no upload time",
        "fresh 1: f-1.tar.gz uploaded 2026-10-25T00:00:00Z, inside the 7-day window",
        f"mirror 1: m.whl not from {FILE_HOST}",
        "empty 1: no sdist and no wheel",
    ]
