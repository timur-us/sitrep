# Local setup

First version (M0, PR 1). SITREP (situation report) builds and tests locally with uv and make, offline, with no `.env` and no API key. Later PRs add the database, the app and the browser test; each one extends this runbook in the same PR. `tests/test_runbook.py` fails when the Makefile and this file drift apart.

## Prerequisites

- **uv** 0.13.5 or later, below 0.14 (`required-version` in `pyproject.toml`). Update with `uv self update`.
- **GNU make** and **git**.
- **Python 3.12** is not a prerequisite: uv installs the version in `.python-version` on first use.

## Commands

| Command | What it does |
|---|---|
| `make help` | Lists the targets. |
| `make test` | Runs `make lint`, then pytest over `tests/` without `tests/web`. Offline. |
| `make lint` | `ruff check` and `ruff format --check`. |
| `make fmt` | `ruff format`. |

The first `make test` creates `.venv/` from `uv.lock`.

## How the toolchain is pinned

- **Lock.** `uv.lock` pins every package with hashes. The Makefile exports `UV_LOCKED=1`, so uv fails instead of rewriting the lock when it disagrees with `pyproject.toml`. After a dependency change, run `uv lock` and commit both files.
- **Minimum release age: 7 days.** `exclude-newer = "7 days"` in `[tool.uv]`: nothing uploaded to PyPI in the last 7 days is resolved. uv records the span in the lock (`exclude-newer-span = "P7D"`), not a timestamp, so the lock does not go stale as days pass. uv 0.13.5 and 0.13.6 both accept it.
- **`sys.path`.** pytest runs as `python -P -m pytest`, so the working directory is not put in front of the standard library. The editable install still appends the repository root (its `.pth` file, a consequence of `module-root = ""`), so `evals`, `redteam` and `data` are importable as namespace packages. `tests/test_data_dirs.py` is what keeps the data in them inert.
- **pytest plugins load by name only.** `--disable-plugin-autoload -p pytest_socket`: an installed package's `pytest11` entry point (platformdirs, a dependency of pip-audit, declares one) does not run.

## Offline tests

pyproject sets `--disable-socket --allow-hosts=127.0.0.1,::1` for pytest. A TCP `connect()` on a Python socket to an address other than `127.0.0.1` or `::1` raises `SocketConnectBlockedError` (`tests/test_network.py`). No test and no config may switch this off: the opt-out markers, fixture and flags are rejected by a scan.

Not covered:
- UDP `sendto()`;
- `connect_ex()` (unverified);
- DNS lookups through libc;
- subprocesses and native libraries;
- anything reached through a loopback proxy or tunnel, such as an `HTTPS_PROXY` on 127.0.0.1.

Later PRs keep those offline through loopback-only URLs and scrubbed environments.

## Repository checks in the test run

| Test | What it holds |
|---|---|
| `tests/test_branding.py` | `app/config/branding.py` is the only file outside prose, `.claude/` and recordings that names the product, the bank, a domain, a cast member or the demo mandate. Infrastructure files that cannot import Python may carry the slug. The values match `CLAUDE.md`. |
| `tests/test_no_secrets.py` | In any file git sees, none of these shapes: an Anthropic key, an AWS access key id, a GitHub token, a private-key block, an Azure storage `AccountKey=`, a password in a URL, or a JWT. Other secret shapes are not detected. The only allowed match is the fake key in `tests/guardrails/test_block_secrets.py`, pinned by its fingerprint. Findings show file, line, pattern and fingerprint (16 hex digits of the match's sha256), never the text. |
| `tests/test_runbook.py` | Every Makefile target is documented here, has a `##` comment, is phony and is listed by `make help`; every `make` command here exists. |
| `tests/test_data_dirs.py` | `evals/cases`, `evals/recorded`, `redteam/attacks`, `redteam/recorded`, `data/synthetic/templates` and `data/synthetic/out` hold only `.json`, `.jsonl`, `.toml`, `.yaml`, `.yml` and `.txt` files (plus `.gitkeep`). No `conftest`, no executable bit, no symlinks, no unreadable directories. |
| `tests/test_network.py` | The offline guarantee above, and that no plugin is autoloaded. |
| `tests/test_repo_files.py` | The shared file lister ignores `GIT_*` variables (as set inside a git hook) and shows git's error on failure. |
| `tests/guardrails/test_block_secrets.py` | The secret hook in `.claude/hooks/` blocks what it must and allows the rest. |

Scans cover tracked files and untracked files that are not ignored (`git ls-files --cached --others --exclude-standard`), so a new file is checked before it is committed. The data-directory check walks the file system instead, so ignored output counts too.
