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

from app.config.settings import env_name

# Key commands per target, as `make -n` prints them.
MODEL_MISSING = "uv run python -P -m app.embeddings.model_files missing"
MODEL_VERIFY = "uv run python -P -m app.embeddings.model_files verify"
# The download (ADR 0005): curl's first argument skips ~/.curlrc; https only, redirects
# included; the URL and the target come from model_files, never from the Makefile.
MODEL_COMMANDS = [
    MODEL_MISSING,
    "curl -q --fail --silent --show-error --location --proto '=https' --proto-redir '=https'",
    "--tlsv1.2",
    # Review finding sec-6: no download larger than the model (470 MB) fills the disk,
    # and an interrupted one leaves no temporary file.
    "--max-filesize 629145600",
    "trap 'rm -f \"$tmp\"' EXIT",
    "trap 'exit 130' INT TERM",
    '-o "$tmp" "$url"',
    'mv -f "$tmp" "$path"',
    MODEL_VERIFY,
]

TARGET_COMMANDS = {
    "lint-test": [
        # -f names compose.yaml alone: no override file and no COMPOSE_FILE is merged in.
        "docker compose -f compose.yaml up -d --wait",
        "uv run python -P -m app.db.migrate",
        *MODEL_COMMANDS,
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
    "docker-build": [
        "docker build",
        "id -u",
        # M1 PR 5: the published port is chosen before the start, so the allowlist can
        # name it, and a foreign Host is refused inside the image.
        "-p 127.0.0.1:$port:8000",
        f"-e {env_name('allowed_hosts')}=127.0.0.1:$port",
        "-H 'Host: 192.0.2.1:8000'",
        f"-e {env_name('trace_plaintext')}=1",
        # The dev login shim is not in the image, and asking for it refuses to start.
        "import app.auth.dev_shim",
        f"-e {env_name('dev_auth')}=1",
        f"-e {env_name('host')}=",
    ],
    "model": MODEL_COMMANDS,
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
    r"\.IGNORE\b.*|\s*(?:(?:export|override)\s+)*MAKEFLAGS\b.*(?:[\s=]-\w*[ik]|--ignore-errors|--keep-going).*"
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


def test_docker_smoke_refuses_the_dev_login_on_both_hosts() -> None:
    # Review finding test-10: the loop covers both hosts, and inside it the run with the
    # dev login on must have exited 2 and logged its refusal.
    out = dry_run("docker-build")
    loop = out.find("for host in 0.0.0.0 127.0.0.1; do")
    assert loop >= 0, out
    # Up to the line after the loop (the loop holds an inner retry loop with its own done).
    end = out.find('echo "smoke: the dev login refused', loop)
    assert end > loop, out
    body = out[loop:end]
    run = f"docker run -d -e {env_name('dev_auth')}=1 -e {env_name('host')}=$host"
    for needle in (
        run,
        'test "$state" = "exited 2"',
        f"refused to start: {env_name('dev_auth')}=1",
    ):
        assert needle in body, f"the dev-login loop lacks {needle!r}:\n{body}"


def test_lint_test_starts_and_migrates_the_database_before_pytest() -> None:
    # The tests in tests/db fail, never skip, without a migrated database.
    out = dry_run("lint-test")
    steps = [
        "docker compose -f compose.yaml up -d --wait",
        "python -P -m app.db.migrate",
        "-m pytest",
    ]
    positions = [out.find(step) for step in steps]
    assert -1 not in positions and positions == sorted(positions), f"order {positions}:\n{out}"


def test_lint_test_fetches_and_checks_the_model_before_pytest() -> None:
    # tests/embeddings fails, never skips, without the model (plan M1, default 6).
    out = dry_run("lint-test")
    steps = ["python -P -m app.db.migrate", MODEL_MISSING, "curl -q ", MODEL_VERIFY, "-m pytest"]
    positions = [out.find(step) for step in steps]
    assert -1 not in positions and positions == sorted(positions), f"order {positions}:\n{out}"


def test_the_model_download_is_anonymous_and_checked() -> None:
    out = dry_run("model")
    # No TLS bypass, no credentials, no plain http, no shell eval of what model_files prints.
    for word in ("--insecure", " -k ", "--netrc", "--user", "-u ", "Authorization", "http://"):
        assert word not in out, f"make model runs {word!r}:\n{out}"
    assert "eval" not in out.split() and "sh -c" not in out, out
    assert out.count("curl ") == 1 and "curl -q " in out, out
    assert out.rstrip().endswith(MODEL_VERIFY), "verify is not the last step"


def test_down_deletes_the_data() -> None:
    # db/init runs only on an empty data directory: `make down` must delete it.
    assert "docker compose -f compose.yaml down --volumes" in dry_run("down")


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
