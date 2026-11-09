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
    # Nothing under models/ is committed, a link named models included (finding sec-5).
    assert git(ROOT, "ls-files", "--", "models").decode() == ""


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
        ("directory", "config.json: not a regular file"),
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
    with pytest.raises(ModelFileError) as error:
        read_verified(tmp_path, "config.json", PLANTED)
    assert str(error.value).startswith(message), str(error.value)
    assert CANARY.decode() not in str(error.value)


FIFO_PROBE = (
    "import sys; from pathlib import Path; from app.embeddings import model_files as m;"
    " files = {'config.json': '0' * 64}\n"
    "try:\n m.read_verified(Path(sys.argv[1]), 'config.json', files)\n"
    "except m.ModelFileError as error:\n print(error)\n"
)


def test_a_fifo_is_refused_without_blocking(tmp_path: Path) -> None:
    # Review finding test-3: opening a FIFO for reading blocks until a writer comes, so
    # the probe runs in a subprocess with a timeout instead of hanging the test run.
    os.mkfifo(tmp_path / "config.json")
    result = subprocess.run(
        [sys.executable, "-P", "-c", FIFO_PROBE, str(tmp_path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("config.json: not a regular file"), result.stdout


@pytest.mark.parametrize(
    "digest",
    [
        # Review finding test-2: the whole digest is compared, not a prefix.
        PLANTED["config.json"][:-1] + ("0" if PLANTED["config.json"][-1] != "0" else "1"),
        PLANTED["config.json"][:63],
        PLANTED["config.json"].upper(),
        PLANTED["config.json"] + "0",
    ],
    ids=["last digit", "63 digits", "upper case", "65 digits"],
)
def test_read_verified_compares_the_whole_digest(tmp_path: Path, digest: str) -> None:
    write(tmp_path, "config.json", GOOD)
    with pytest.raises(ModelFileError, match="sha256 differs from FILES"):
        read_verified(tmp_path, "config.json", {"config.json": digest})


@pytest.mark.parametrize(
    "rel", ["../config.json", "/config.json", "./config.json", "onnx//model.onnx", "onnx\\x.json"]
)
def test_read_verified_takes_only_plain_paths(tmp_path: Path, rel: str) -> None:
    # Review finding test-1: each path, joined without the check, reaches a file with
    # the pinned bytes, so only the path check can refuse it.
    directory = tmp_path / "m"
    write(tmp_path, "config.json", GOOD)  # where "../config.json" lands
    write(directory, "config.json", GOOD)  # where "/config.json" and "./config.json" land
    write(directory, "onnx/model.onnx", GOOD)  # where "onnx//model.onnx" lands
    write(directory, "onnx\\x.json", GOOD)  # a backslash is a name character on POSIX
    files = {rel: hashlib.sha256(GOOD).hexdigest()}
    with pytest.raises(ModelFileError, match="not a plain relative path"):
        read_verified(directory, rel, files)


def test_read_verified_takes_only_listed_files(tmp_path: Path) -> None:
    write(tmp_path, "other.json", GOOD)
    with pytest.raises(ModelFileError, match="other.json: not one of FILES"):
        read_verified(tmp_path, "other.json", PLANTED)


def test_a_link_inside_the_model_directory_is_refused(tmp_path: Path) -> None:
    # Review finding sec-5: a linked directory between the model directory and a file
    # is refused, so neither a read nor a deletion follows it out.
    directory, elsewhere = tmp_path / "m", tmp_path / "elsewhere"
    write(directory, "config.json", GOOD)
    stale = write(elsewhere, "model.onnx", b"stale")
    (directory / "onnx").symlink_to(elsewhere, target_is_directory=True)
    message = "onnx/model.onnx: a link in the path"
    with pytest.raises(ModelFileError, match=message):
        read_verified(directory, "onnx/model.onnx", PLANTED)
    with pytest.raises(ModelFileError, match=message):
        missing(directory, PLANTED)
    assert verify(directory, PLANTED) == [f"{message} (remove it)"]
    assert stale.read_bytes() == b"stale", "a file behind the link was deleted"


def test_the_model_directory_itself_may_be_a_link(tmp_path: Path) -> None:
    # By design: models/ (or the revision directory) may be a link, to a shared disk.
    write(tmp_path / "real", "config.json", GOOD)
    (tmp_path / "link").symlink_to(tmp_path / "real", target_is_directory=True)
    assert read_verified(tmp_path / "link", "config.json", PLANTED) == GOOD


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
