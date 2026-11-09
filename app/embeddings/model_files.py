"""The embedding model's files on disk: where they live, and the one way to read them.

`make model` downloads FILES at REVISION (app/config/embedding.py) into
`models/<MODEL_ID>/<REVISION>/` under the repository root, which git ignores. Every
load reads a file through read_verified(): its bytes, only if their sha256 is the pinned
one. The embedder hands those very bytes to onnxruntime and tokenizers, never the path,
so nothing can change the file between the check and the load.

The model directory itself may be a link (to a shared disk, say). Below it, a link is
refused, whether it is the file or a directory on the way to it (review finding sec-5):
nothing reads, deletes or downloads through one.

`make model` runs this module as `python -P -m app.embeddings.model_files`:
- `missing` deletes each file whose bytes differ from FILES and prints `<url> <path>`
  for every file to download: absent, or just deleted;
- `verify` checks every file, deletes each mismatch, and exits 1 naming the files.

The download itself is curl, in the Makefile: app/ holds no HTTP client
(tests/test_import_allowlist.py). The URL names the commit, never a branch.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import stat
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from app.config.embedding import FILES, MODEL_ID, REVISION

__all__ = (
    "HUB",
    "MODELS",
    "ModelFileError",
    "main",
    "missing",
    "model_dir",
    "read_verified",
    "url",
    "verify",
)

ROOT = Path(__file__).resolve().parents[2]
MODELS = ROOT / "models"  # git-ignored (.gitignore), outside the image's build context
HUB = "https://huggingface.co"
# Larger than any pinned file (the model is 470 MB), so a file in the wrong place
# cannot fill memory before its hash is known.
MAX_BYTES = 1 << 30


class ModelFileError(Exception):
    """A model file that is missing, not a plain file, or not the pinned bytes.

    The message names the file's path in the model repository and the reason, and
    never its content.
    """


def model_dir(models: Path = MODELS) -> Path:
    """Where FILES live: one directory per model and revision."""
    return models / MODEL_ID / REVISION


def url(rel: str) -> str:
    """The file at the pinned commit, anonymously, over https."""
    return f"{HUB}/{MODEL_ID}/resolve/{REVISION}/{rel}"


def _path(directory: Path, rel: str, files: Mapping[str, str]) -> Path:
    if rel not in files:
        raise ModelFileError(f"{rel}: not one of FILES")
    # The raw parts: PurePosixPath would fold "./x" and "a//b" into plain paths.
    parts = rel.split("/")
    if "\\" in rel or any(part in ("", ".", "..") for part in parts):
        raise ModelFileError(f"{rel}: not a plain relative path")
    # O_NOFOLLOW covers the last part only: a linked directory on the way is refused here.
    for depth in range(1, len(parts)):
        if os.path.islink(directory.joinpath(*parts[:depth])):
            raise ModelFileError(f"{rel}: a link in the path (remove it)")
    return directory.joinpath(*parts)


def _open(path: Path, rel: str) -> int:
    """A descriptor on a regular file; a link, a FIFO or a directory is refused unread."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        raise ModelFileError(f"{rel}: missing (run make model)") from None
    except OSError as error:
        reason = type(error).__name__
        raise ModelFileError(f"{rel}: cannot be opened as a plain file ({reason})") from None
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
        os.close(fd)
        raise ModelFileError(f"{rel}: not a regular file of at most {MAX_BYTES} bytes")
    return fd


def read_verified(directory: Path, rel: str, files: Mapping[str, str] = FILES) -> bytes:
    """The file's bytes, read once, if their sha256 is files[rel]; ModelFileError otherwise."""
    fd = _open(_path(directory, rel, files), rel)
    with os.fdopen(fd, "rb") as handle:
        data = handle.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ModelFileError(f"{rel}: larger than {MAX_BYTES} bytes")
    if hashlib.sha256(data).hexdigest() != files[rel]:
        raise ModelFileError(f"{rel}: sha256 differs from FILES (run make model)")
    return data


def _matches(directory: Path, rel: str, files: Mapping[str, str]) -> bool | None:
    """True when the file is the pinned bytes, False when it is a plain file that is not,
    None when it is absent. Anything that is not a plain file raises."""
    path = _path(directory, rel, files)
    if not os.path.lexists(path):
        return None
    fd = _open(path, rel)
    with os.fdopen(fd, "rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    return digest == files[rel]


def missing(directory: Path, files: Mapping[str, str] = FILES) -> list[tuple[str, Path]]:
    """(url, path) of each file to download. A plain file with other bytes is deleted
    first; a link or another kind of file raises ModelFileError, and is left alone."""
    todo = []
    for rel in files:
        found = _matches(directory, rel, files)
        if found is False:
            _path(directory, rel, files).unlink()
        if not found:
            todo.append((url(rel), _path(directory, rel, files)))
    return todo


def verify(directory: Path, files: Mapping[str, str] = FILES) -> list[str]:
    """One line per file that is not the pinned bytes; a plain file that differs is deleted."""
    problems = []
    for rel in files:
        try:
            found = _matches(directory, rel, files)
        except ModelFileError as error:
            problems.append(str(error))
            continue
        if found is None:
            problems.append(f"{rel}: missing (run make model)")
        elif found is False:
            _path(directory, rel, files).unlink()
            problems.append(f"{rel}: sha256 differs from FILES, deleted (run make model)")
    return problems


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -P -m app.embeddings.model_files",
        description="Plan and check the download of the pinned embedding model (make model).",
    )
    parser.add_argument("command", choices=("missing", "verify"))
    command = parser.parse_args(argv).command
    directory = model_dir()
    try:
        if command == "missing":
            for link, path in missing(directory):
                print(f"{link} {path}")
            return 0
        problems = verify(directory)
    except ModelFileError as error:
        print(f"model: {error}", file=sys.stderr)
        return 1
    for problem in problems:
        print(f"model: {problem}", file=sys.stderr)
    if problems:
        return 1
    print(f"model: {len(FILES)} files match FILES at {REVISION} ({MODEL_ID})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
