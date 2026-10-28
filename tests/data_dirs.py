"""Data directories hold data only: no code, nothing executable, no links.

Text in these directories is untrusted (CLAUDE.md rule 9). pytest collects only
tests/, and every runnable starts with `python -P -m`, so no data directory
lands on sys.path. This check is the layer beneath: even if something did
collect or import from here, there is no code to run.

The file system is walked, not git's file list, so ignored output counts too.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

DATA_DIRS: tuple[str, ...] = (
    "evals/cases",
    "evals/recorded",
    "redteam/attacks",
    "redteam/recorded",
    "data/synthetic/templates",
    "data/synthetic/out",
)

# Read only with json, tomllib or yaml.safe_load, or as plain text.
DATA_SUFFIXES: frozenset[str] = frozenset(
    {".json", ".jsonl", ".toml", ".yaml", ".yml", ".txt", ".md"}
)

# Allowed by exact name: keeps an empty directory in git.
ALLOWED_NAMES = frozenset({".gitkeep"})

# Skipped: macOS Finder metadata, git-ignored, never read.
IGNORED_NAMES = frozenset({".DS_Store"})


def violations(root: Path) -> list[str]:
    """One "<path>: <reason>" line per entry that is not inert data."""
    found = []
    for rel_dir in DATA_DIRS:
        base = root / rel_dir
        if base.is_symlink():
            found.append(f"{rel_dir}: symlink")
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            for name in sorted(dirnames + filenames):
                path = Path(dirpath, name)
                rel = path.relative_to(root).as_posix()
                mode = path.lstat().st_mode
                if stat.S_ISLNK(mode):
                    found.append(f"{rel}: symlink")
                    continue
                if stat.S_ISDIR(mode) or name in IGNORED_NAMES:
                    continue
                if name.startswith("conftest"):
                    found.append(f"{rel}: conftest")
                elif name not in ALLOWED_NAMES and path.suffix not in DATA_SUFFIXES:
                    found.append(f"{rel}: not a declared data extension")
                if mode & 0o111:
                    found.append(f"{rel}: executable")
    return found
