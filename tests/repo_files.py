"""The repository's files as git sees them, shared by the repo-wide scans."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def list_files(root: Path = ROOT) -> list[str]:
    """Tracked and untracked files that are not ignored, as repo-relative POSIX paths.

    `git ls-files --cached --others --exclude-standard`: a new file is scanned
    before it is committed.
    """
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        check=True,
        capture_output=True,
    )
    return sorted({path.decode() for path in result.stdout.split(b"\0") if path})


def read_text(root: Path, rel: str) -> str | None:
    """The file's text; for a symlink, the path it points to; None if the file is gone."""
    path = root / rel
    if path.is_symlink():
        return os.readlink(path)
    if not path.is_file():
        return None
    return path.read_bytes().decode("utf-8", errors="replace")
