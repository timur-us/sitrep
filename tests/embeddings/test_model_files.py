"""The model files: where `make model` puts them, and how every load reads them (ADR 0005).

read_verified() returns a file's bytes only when their sha256 is the pinned one, and
refuses a missing file, a link, a FIFO and a directory without reading them. The
`missing` and `verify` steps of `make model` delete a plain file with other bytes, never
what a link points to. Planted directories, with a files mapping of their own; one
subprocess runs both steps against the real model directory.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from repo_files import ROOT, git

from app.config.embedding import FILES, MODEL_ID, REVISION
from app.embeddings import model_files
from app.embeddings.model_files import (
    HUB,
    MODELS,
    ModelFileError,
    missing,
    model_dir,
    read_verified,
    url,
    verify,
)

CANARY = b"cnry-model-content-41c7"
GOOD = b'{"hidden_size": 384}\n' + CANARY
OTHER = b"onnx bytes " + CANARY
PLANTED = {
    "config.json": hashlib.sha256(GOOD).hexdigest(),
    "onnx/model.onnx": hashlib.sha256(OTHER).hexdigest(),
}


def write(directory: Path, rel: str, data: bytes) -> Path:
    path = directory / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_the_model_directory() -> None:
    assert MODELS == ROOT / "models"
    assert model_dir() == ROOT / "models" / MODEL_ID / REVISION
    assert model_dir(Path("/elsewhere")) == Path("/elsewhere") / MODEL_ID / REVISION


def test_the_model_files_are_git_ignored() -> None:
    for rel in FILES:
        path = (model_dir() / rel).relative_to(ROOT).as_posix()
        assert git(ROOT, "check-ignore", "--no-index", path).decode().strip() == path
    # Only the top-level models/ directory: a models/ package elsewhere stays visible.
    with pytest.raises(AssertionError):
        git(ROOT, "check-ignore", "--no-index", "app/models/x.py")


def test_urls_name_the_commit_over_https() -> None:
    for rel in FILES:
        parts = urlsplit(url(rel))
        assert (parts.scheme, parts.hostname, parts.query, parts.fragment) == (
            "https",
            "huggingface.co",
            "",
            "",
        )
        assert parts.path == f"/{MODEL_ID}/resolve/{REVISION}/{rel}"
    assert HUB == "https://huggingface.co"


def test_read_verified_returns_the_pinned_bytes(tmp_path: Path) -> None:
    write(tmp_path, "config.json", GOOD)
    assert read_verified(tmp_path, "config.json", PLANTED) == GOOD


@pytest.mark.parametrize(
    ("setup", "message"),
    [
        ("absent", "config.json: missing (run make model)"),
        ("changed", "config.json: sha256 differs from FILES (run make model)"),
        ("link", "config.json: cannot be opened as a plain file"),
        ("directory", "config.json: "),
        ("fifo", "config.json: not a regular file"),
    ],
)
def test_read_verified_refuses(tmp_path: Path, setup: str, message: str) -> None:
    path = tmp_path / "config.json"
    if setup == "changed":
        path.write_bytes(GOOD + b" ")
    elif setup == "link":
        # A link to the right bytes is refused too: the file itself must be in place.
        target = write(tmp_path, "elsewhere.json", GOOD)
        path.symlink_to(target)
    elif setup == "directory":
        path.mkdir()
    elif setup == "fifo":
        os.mkfifo(path)  # opening it for reading would block without O_NONBLOCK
    with pytest.raises(ModelFileError) as error:
        read_verified(tmp_path, "config.json", PLANTED)
    assert str(error.value).startswith(message), str(error.value)
    assert CANARY.decode() not in str(error.value)


@pytest.mark.parametrize(
    "rel", ["other.json", "../config.json", "/config.json", "./config.json", "onnx//model.onnx"]
)
def test_read_verified_takes_only_listed_plain_paths(tmp_path: Path, rel: str) -> None:
    files = dict(PLANTED) | {rel: PLANTED["config.json"]}
    write(tmp_path, "config.json", GOOD)
    with pytest.raises(ModelFileError):
        read_verified(tmp_path, rel, PLANTED if rel == "other.json" else files)


def test_read_verified_refuses_an_oversized_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(model_files, "MAX_BYTES", len(GOOD) - 1)
    write(tmp_path, "config.json", GOOD)
    with pytest.raises(ModelFileError, match="not a regular file of at most"):
        read_verified(tmp_path, "config.json", PLANTED)


def test_missing_lists_what_to_download(tmp_path: Path) -> None:
    assert missing(tmp_path, PLANTED) == [
        (url(rel), tmp_path.joinpath(*rel.split("/"))) for rel in PLANTED
    ]
    write(tmp_path, "config.json", GOOD)
    changed = write(tmp_path, "onnx/model.onnx", b"stale")
    assert missing(tmp_path, PLANTED) == [(url("onnx/model.onnx"), changed)]
    assert not changed.exists(), "a plain file with other bytes is deleted"
    write(tmp_path, "onnx/model.onnx", OTHER)
    assert missing(tmp_path, PLANTED) == []


def test_missing_leaves_a_link_and_its_target_alone(tmp_path: Path) -> None:
    target = write(tmp_path, "outside.txt", b"keep me")
    (tmp_path / "config.json").symlink_to(target)
    with pytest.raises(ModelFileError, match="cannot be opened as a plain file"):
        missing(tmp_path, PLANTED)
    assert target.read_bytes() == b"keep me" and (tmp_path / "config.json").is_symlink()


def test_verify(tmp_path: Path) -> None:
    write(tmp_path, "config.json", GOOD)
    changed = write(tmp_path, "onnx/model.onnx", b"stale")
    assert verify(tmp_path, PLANTED) == [
        "onnx/model.onnx: sha256 differs from FILES, deleted (run make model)"
    ]
    assert not changed.exists()
    assert verify(tmp_path, PLANTED) == ["onnx/model.onnx: missing (run make model)"]
    write(tmp_path, "onnx/model.onnx", OTHER)
    assert verify(tmp_path, PLANTED) == []
    (tmp_path / "config.json").unlink()
    (tmp_path / "config.json").symlink_to(tmp_path / "onnx" / "model.onnx")
    (problem,) = verify(tmp_path, PLANTED)
    assert problem.startswith("config.json: cannot be opened as a plain file"), problem
    assert (tmp_path / "onnx" / "model.onnx").read_bytes() == OTHER


def run_module(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-P", "-m", "app.embeddings.model_files", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_the_make_model_steps_on_the_real_directory(embedder: object) -> None:
    # `embedder` (conftest) fails first when `make model` has not run.
    listed = run_module("missing")
    assert (listed.returncode, listed.stdout, listed.stderr) == (0, "", "")
    checked = run_module("verify")
    assert checked.returncode == 0, checked.stderr
    assert checked.stdout == f"model: {len(FILES)} files match FILES at {REVISION} ({MODEL_ID})\n"
    assert run_module("download").returncode == 2
    assert run_module().returncode == 2
