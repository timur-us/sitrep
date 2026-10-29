"""The image, read statically: pinned bases, the exact uv, a locked install without dev
tools, a non-root user, and a build context that leaves secrets and tests out.

`make docker-build` checks the built image (health, uid, plaintext refused); this file
keeps the Dockerfile honest in every `make test`, where Docker is not required.
"""

from __future__ import annotations

import json
import re
import tomllib

from repo_files import ROOT

DOCKERFILE = ROOT / "Dockerfile"
DOCKERIGNORE = ROOT / ".dockerignore"
DIGEST = re.compile(r"@sha256:[0-9a-f]{64}")
PYTHON_IMAGE = re.compile(r"python:(?P<version>3\.\d+\.\d+)-slim-trixie@sha256:[0-9a-f]{64}")
UV_IMAGE = re.compile(r"ghcr\.io/astral-sh/uv:(?P<version>\d+\.\d+\.\d+)@sha256:[0-9a-f]{64}")


def instructions(text: str) -> list[tuple[str, str]]:
    """(INSTRUCTION, arguments), continuation lines joined, comments dropped."""
    lines: list[str] = []
    pending = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not pending and (not line or line.startswith("#")):
            continue
        if line.endswith("\\"):
            pending += line[:-1] + " "
            continue
        lines.append(pending + line)
        pending = ""
    parsed = []
    for line in lines:
        keyword, _, arguments = line.partition(" ")
        parsed.append((keyword.upper(), " ".join(arguments.split())))
    return parsed


def stages(text: str) -> list[list[tuple[str, str]]]:
    found: list[list[tuple[str, str]]] = []
    for keyword, arguments in instructions(text):
        if keyword == "FROM":
            found.append([])
        if found:
            found[-1].append((keyword, arguments))
    return found


def _text() -> str:
    return DOCKERFILE.read_text(encoding="utf-8")


def test_base_images_are_pinned() -> None:
    text = _text()
    assert not re.search(r"(?im)^#\s*syntax=", text), "a syntax directive pulls an unpinned image"
    names = set()
    images = []
    for keyword, arguments in instructions(text):
        if keyword == "FROM":
            image, *rest = arguments.split()
            images.append(image)
            if len(rest) == 2 and rest[0].upper() == "AS":
                names.add(rest[1])
    assert len(images) >= 3, f"expected uv, build and runtime stages, got {images}"
    unpinned = [image for image in images if image not in names and not DIGEST.search(image)]
    assert not unpinned, f"base images without a digest: {unpinned}"

    python = [match["version"] for image in images if (match := PYTHON_IMAGE.fullmatch(image))]
    wanted = (ROOT / ".python-version").read_text(encoding="utf-8").strip()
    assert python and all(version.startswith(wanted + ".") for version in python), python
    assert len(set(python)) == 1, f"build and runtime use different Pythons: {python}"

    # ADR 0006: the build toolchain is pinned by exact version, the one required-version
    # starts at (CI's setup-uv joins this check in PR 4).
    uv = [match["version"] for image in images if (match := UV_IMAGE.fullmatch(image))]
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    floor = re.match(r">=(\d+\.\d+\.\d+)", pyproject["tool"]["uv"]["required-version"])
    assert floor, pyproject["tool"]["uv"]["required-version"]
    assert uv == [floor.group(1)], f"uv image {uv}, required-version floor {floor.group(1)}"


def test_install_is_locked_without_dev_tools() -> None:
    runs = [arguments for keyword, arguments in instructions(_text()) if keyword == "RUN"]
    syncs = [run for run in runs if "uv sync" in run]
    assert syncs, "no uv sync"
    for run in syncs:
        assert "--locked" in run and "--no-dev" in run, run
    banned = ("pip install", "curl", "wget", "apt-get", "uv pip", "uv add", "uv lock")
    found = [(run, word) for run in runs for word in banned if word in run]
    assert not found, found
    adds = [arguments for keyword, arguments in instructions(_text()) if keyword == "ADD"]
    assert not adds, f"ADD can fetch URLs; use COPY: {adds}"


def test_runtime_stage() -> None:
    runtime = stages(_text())[-1]
    keywords = [keyword for keyword, _ in runtime]
    users = [arguments for keyword, arguments in runtime if keyword == "USER"]
    assert users, "the runtime stage has no USER"
    uid = users[-1].partition(":")[0]
    assert uid == "10001", f"runtime user {users[-1]!r}"

    env = {}
    for keyword, arguments in runtime:
        if keyword == "ENV":
            env |= dict(pair.split("=", 1) for pair in arguments.split())
    assert env.get("DEV_AUTH") == "0", env
    assert env.get("HOST") == "0.0.0.0", env  # noqa: S104 - the container's own interface
    assert "TRACE_PLAINTEXT" not in env or env["TRACE_PLAINTEXT"] == "0", env

    cmds = [arguments for keyword, arguments in runtime if keyword == "CMD"]
    assert cmds and json.loads(cmds[-1]) == ["python", "-P", "-m", "app"], cmds
    assert "ENTRYPOINT" not in keywords, "an ENTRYPOINT would wrap the CMD"
    copies = [arguments for keyword, arguments in runtime if keyword == "COPY"]
    assert all(arguments.startswith("--from=") for arguments in copies), (
        f"the runtime stage copies from the build context: {copies}"
    )


def test_dockerignore() -> None:
    rules = [
        line.strip()
        for line in DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert rules and rules[0] == "*", "the build context is not deny-by-default"
    included = {rule[1:].rstrip("/") for rule in rules if rule.startswith("!")}
    assert included == {"app", "pyproject.toml", "uv.lock"}, sorted(included)
    # Excluded again after the last re-include, so that nothing inside app/ slips back in.
    last = max(index for index, rule in enumerate(rules) if rule.startswith("!"))
    excluded = set(rules[last + 1 :])
    for rule in ("**/.env*", ".git", ".claude", "data/synthetic/out", "tests", "**/__pycache__"):
        assert rule in excluded, f"{rule} is not excluded after the re-includes"
