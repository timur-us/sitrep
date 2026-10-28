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
- **No working directory on `sys.path`.** pytest runs as `python -P -m pytest`.

## Offline tests

pyproject sets `--disable-socket --allow-hosts=127.0.0.1,::1` for pytest. A `connect()` on a Python socket to any address other than loopback raises `SocketConnectBlockedError` (`tests/test_network.py`).

Not covered: UDP `sendto()`, DNS lookups through libc, subprocesses, and native libraries. Later PRs keep those offline through loopback-only URLs and scrubbed environments.

## Repository checks in the test run

| Test | What it holds |
|---|---|
| `tests/test_branding.py` | `app/config/branding.py` is the only file outside prose, `.claude/` and recordings that names the product, the bank, a domain, a cast member or the demo mandate. Infrastructure files that cannot import Python may carry the slug. The values match `CLAUDE.md`. |
| `tests/test_no_secrets.py` | No API key, token, private key, storage key or password in a URL in any file git sees. The only allowed match is the fake key in `tests/guardrails/test_block_secrets.py`. Findings show file, line and pattern, never the text. |
| `tests/test_runbook.py` | Every Makefile target is documented here, and every `make` command here exists. |
| `tests/test_data_dirs.py` | `evals/cases`, `evals/recorded`, `redteam/attacks`, `redteam/recorded`, `data/synthetic/templates` and `data/synthetic/out` hold data files only: no code, no `conftest`, no executable bit, no symlinks. |
| `tests/guardrails/test_block_secrets.py` | The secret hook in `.claude/hooks/` blocks what it must and allows the rest. |

Scans cover tracked files and untracked files that are not ignored (`git ls-files --cached --others --exclude-standard`), so a new file is checked before it is committed. The data-directory check walks the file system instead, so ignored output counts too.
