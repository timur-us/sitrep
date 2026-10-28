"""Data directories hold data only: declared data suffixes, no executable bit, no links.

Text in these directories is untrusted (CLAUDE.md rule 9). pytest collects only
tests/, and `python -P` keeps the working directory off sys.path. The editable
install still appends the repository root (its .pth file, from module-root = ""),
so these directories are importable as namespace packages. This check is what
keeps them inert: no file here has a code suffix, an executable bit or a link
to somewhere else. Suffixes are what is checked, not content.

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
DATA_SUFFIXES: frozenset[str] = frozenset({".json", ".jsonl", ".toml", ".yaml", ".yml", ".txt"})

# Allowed by exact name: keeps an empty directory in git.
ALLOWED_NAMES = frozenset({".gitkeep"})

# Exempt from the suffix check only: macOS Finder metadata, git-ignored.
FINDER_METADATA = frozenset({".DS_Store"})


def violations(root: Path) -> list[str]:
    """One "<path>: <reason>" line per entry that is not inert data."""
    found: list[str] = []

    def unreadable(error: OSError) -> None:
        found.append(f"{Path(error.filename).relative_to(root).as_posix()}: unreadable")

    for rel_dir in DATA_DIRS:
        base = root / rel_dir
        if base.is_symlink():
            found.append(f"{rel_dir}: symlink")
            continue
        if not base.exists():
            continue
        if not base.is_dir():
            found.append(f"{rel_dir}: not a directory")
            continue
        for dirpath, dirnames, filenames in os.walk(base, onerror=unreadable):
            for name in sorted(dirnames + filenames):
                path = Path(dirpath, name)
                rel = path.relative_to(root).as_posix()
                mode = path.lstat().st_mode
                if stat.S_ISLNK(mode):
                    found.append(f"{rel}: symlink")
                    continue
                if stat.S_ISDIR(mode):
                    continue
                if mode & 0o111:
                    found.append(f"{rel}: executable")
                if name.startswith("conftest"):
                    found.append(f"{rel}: conftest")
                elif (
                    name not in ALLOWED_NAMES | FINDER_METADATA and path.suffix not in DATA_SUFFIXES
                ):
                    found.append(f"{rel}: not a declared data extension")
    return found
