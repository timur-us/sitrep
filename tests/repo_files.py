"""The repository's files as git sees them, shared by the repo-wide scans."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def git(root: Path, *args: str) -> bytes:
    """Run git in root and return its stdout.

    GIT_* variables are dropped: inside a git hook, GIT_DIR and GIT_INDEX_FILE
    point at the outer repository. LC_ALL=C keeps git's messages in English. A
    failure raises with git's own message.
    """
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env["LC_ALL"] = "C"
    result = subprocess.run(
        ["git", "-C", str(root), *args], env=env, capture_output=True, check=False
    )
    if result.returncode != 0:
        stderr = result.stderr.decode(errors="replace").strip()
        raise AssertionError(f"git {' '.join(args)} failed in {root}: {stderr}")
    return result.stdout


def list_files(root: Path = ROOT) -> list[str]:
    """Tracked and untracked files that are not ignored, as repo-relative POSIX paths.

    `git ls-files --cached --others --exclude-standard`: a new file is scanned
    before it is committed.
    """
    out = git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    return sorted({path.decode() for path in out.split(b"\0") if path})


def read_text(root: Path, rel: str) -> str | None:
    """The file's text; for a symlink, the path it points to; None if the file is gone."""
    path = root / rel
    if path.is_symlink():
        return os.readlink(path)
    if not path.is_file():
        return None
    return path.read_bytes().decode("utf-8", errors="replace")
