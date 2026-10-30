"""The make targets CI calls: what they run, and that none of them can swallow a failure.

`make -n` prints a recipe without running it, so these checks need neither Docker nor
the network.
"""

from __future__ import annotations

import os
import re
import subprocess
import tomllib

import pytest
from repo_files import ROOT

# Key commands per target, as `make -n` prints them.
TARGET_COMMANDS = {
    "lint-test": [
        "uv run ruff check",
        "uv run ruff format --check",
        "uv run python -P -m pytest --ignore=tests/web",
    ],
    "audit": [
        "uv lock --check --refresh",
        "uv export --format requirements.txt --locked --no-emit-project --all-groups",
        "uv run pip-audit -r",
        "--require-hashes",
        "--disable-pip",
    ],
    "docker-build": ["docker build", "id -u", "-p 127.0.0.1::8000", "-e TRACE_PLAINTEXT=1"],
    "web-deps": ["uv run playwright install chromium"],
    "web-test": ["uv run playwright install chromium", "uv run python -P -m pytest tests/web"],
    "evals": ["uv run python -P -m app.runners.evals"],
    "redteam": ["uv run python -P -m app.runners.redteam"],
}

# Ways a recipe or the Makefile keeps going after a failed command.
SWALLOWED = re.compile(
    r"\|\|\s*(?:true\b|:(?!\S)|exit 0\b)|;\s*true\b|--suppress-no-test-exit-code"
)
IGNORING = re.compile(
    r"\.IGNORE\b.*|\s*(?:export\s+)?MAKEFLAGS\b.*(?:\s-\w*[ik]|--ignore-errors|--keep-going).*"
)


def dry_run(target: str) -> str:
    # Outer make variables (MAKEFLAGS, MAKELEVEL) would leak into the inner make when
    # the tests themselves run under `make test`.
    env = {
        key: value for key, value in os.environ.items() if not key.startswith(("MAKE", "MFLAGS"))
    }
    result = subprocess.run(
        ["make", "--no-print-directory", "-n", target],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"make -n {target}: {result.stderr.strip()}"
    return result.stdout


@pytest.mark.parametrize("target", sorted(TARGET_COMMANDS))
def test_ci_target_runs_its_commands(target: str) -> None:
    out = dry_run(target)
    missing = [command for command in TARGET_COMMANDS[target] if command not in out]
    assert not missing, f"make -n {target} lacks {missing}:\n{out}"


def test_web_deps_installs_chromium_without_root() -> None:
    # No --with-deps: it runs apt as root (Project 1 lost CI runs to the apt mirror).
    out = dry_run("web-deps")
    for word in ("--with-deps", "sudo", "apt"):
        assert word not in out, f"web-deps runs {word!r}:\n{out}"
    assert "chromium" in out and "firefox" not in out and "webkit" not in out, out


def recipe_problems(makefile: str) -> list[str]:
    """Recipe lines that ignore an error, and settings that make every line do so."""
    found = []
    for number, line in enumerate(makefile.splitlines(), start=1):
        if IGNORING.fullmatch(line):
            found.append(f"{line.strip()!r} ignores failures")
        if not line.startswith("\t"):
            continue
        prefix = re.match(r"\t[@+\-\s]*", line).group(0)
        if "-" in prefix:
            found.append(f"line {number}: a leading '-' ignores the command's failure")
        if match := SWALLOWED.search(line):
            found.append(f"line {number}: {match.group(0)!r} swallows a failure")
    return found


def test_no_recipe_swallows_a_failure() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert makefile.count("\n\t") >= 10, "the Makefile has almost no recipe lines"
    found = recipe_problems(makefile)
    assert not found, "\n".join(found)


def test_planted_recipes_are_caught() -> None:
    planted = (
        "a:\n"
        "\t-rm -f x\n"
        "\t@-echo y\n"
        "\tpytest || true\n"
        "\tpytest || :\n"
        "\tfalse; true\n"
        "\tpytest --suppress-no-test-exit-code\n"
        "\techo fine && true_story\n"
        ".IGNORE:\n"
        "MAKEFLAGS += -i\n"
        "export MAKEFLAGS := --keep-going\n"
        "MAKEFLAGS=-i\n"
        "MAKEFLAGS+=-k\n"
        "override MAKEFLAGS:=-ik\n"
    )
    assert recipe_problems(planted) == [
        "line 2: a leading '-' ignores the command's failure",
        "line 3: a leading '-' ignores the command's failure",
        "line 4: '|| true' swallows a failure",
        "line 5: '|| :' swallows a failure",
        "line 6: '; true' swallows a failure",
        "line 7: '--suppress-no-test-exit-code' swallows a failure",
        "'.IGNORE:' ignores failures",
        "'MAKEFLAGS += -i' ignores failures",
        "'export MAKEFLAGS := --keep-going' ignores failures",
        "'MAKEFLAGS=-i' ignores failures",
        "'MAKEFLAGS+=-k' ignores failures",
        "'override MAKEFLAGS:=-ik' ignores failures",
    ]


def test_pytest_still_blocks_sockets() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    addopts = pyproject["tool"]["pytest"]["addopts"]
    assert "--disable-socket" in addopts, addopts
    assert "--allow-hosts=127.0.0.1,::1" in addopts, addopts
