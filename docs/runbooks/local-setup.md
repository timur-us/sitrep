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
- **Minimum release age: 7 days.** `exclude-newer = "7 days"` in `[tool.uv]`: nothing uploaded to PyPI in the last 7 days is resolved. uv records the span in the lock (`exclude-newer-span = "P7D"`), not a timestamp, so the lock does not go stale as days pass. uv 0.13.5 and 0.13.6 both accept it. The policy does not cover the build backend `uv_build`, which the lock never resolves; the exact uv pin in CI and Docker is the control there (ADR 0006; planned for PRs 3 and 4, not in place yet).
- **`sys.path`.** pytest runs as `python -P -m pytest`, so the working directory is not put in front of the standard library. The editable install still appends the repository root (its `.pth` file, a consequence of `module-root = ""`), so `evals`, `redteam` and `data` are importable as namespace packages. `tests/test_data_dirs.py` is what keeps the data in them inert.
- **pytest plugins load by name only.** `--disable-plugin-autoload -p pytest_socket`: an installed package's `pytest11` entry point (platformdirs, a dependency of pip-audit, declares one) does not run.

## Offline tests

pyproject sets `--disable-socket --allow-hosts=127.0.0.1,::1` for pytest. A TCP `connect()` on a Python socket to an address other than `127.0.0.1` or `::1` raises `SocketConnectBlockedError` (`tests/test_network.py`).

Two checks keep the block from being switched off:
- **The effective configuration.** pytest's own view must show `pyproject.toml` as the config file, the socket block on, exactly those two hosts allowed, and no force-enable. This holds wherever an option came from: another config file, `PYTEST_ADDOPTS` or the command line.
- **A text scan.** Every `.py`, `.toml`, `.ini` and `.cfg` file git sees, plus the Makefile and `.github/`, is checked for pytest-socket's opt-out markers, fixture and flags. Code that rebinds socket functions directly is not detected.

Not covered:
- UDP `sendto()`;
- `connect_ex()` (unverified);
- DNS lookups through libc;
- subprocesses and native libraries;
- anything reached through a loopback proxy or tunnel, such as an `HTTPS_PROXY` on 127.0.0.1;
- code that runs at import or collection time, or in fixture teardown. Suspected, not verified: pytest-socket applies the block per test, in setup.

Later PRs keep those offline through loopback-only URLs and scrubbed environments.

## Repository checks in the test run

| Test | What it holds |
|---|---|
| `tests/test_branding.py` | `app/config/branding.py` is the only file outside prose, `.claude/` and recordings that names the product, the bank, a domain, a cast member or the demo mandate. Infrastructure files that cannot import Python may carry the slug. The values match `CLAUDE.md`. |
| `tests/test_no_secrets.py` | In any file git sees, none of these shapes: an Anthropic key, an AWS access key id, a GitHub token, a private-key block, an Azure storage `AccountKey=`, a password in a URL, or a JWT. Other secret shapes are not detected. The only allowed match is the fake key in `tests/guardrails/test_block_secrets.py`, pinned by its fingerprint (16 hex digits of the match's sha256); any other match in that file is reported. Findings show file, line and pattern. The text is never shown, and the fingerprint is not either, because the fingerprint of a URL password can be reversed with a dictionary. To allowlist a match on purpose, compute its fingerprint locally with `find_secrets()`. |
| `tests/test_runbook.py` | Every Makefile target is documented here, has a `##` comment, is phony and is listed by `make help`; every `make` command here exists. |
| `tests/test_data_dirs.py` | `evals/cases`, `evals/recorded`, `redteam/attacks`, `redteam/recorded`, `data/synthetic/templates` and `data/synthetic/out` hold only `.json`, `.jsonl`, `.toml`, `.yaml`, `.yml` and `.txt` files (plus `.gitkeep`). No `conftest`, no executable bit, no symlinks, no unreadable directories. |
| `tests/test_network.py` | The offline guarantee above: the block itself, the effective configuration, the opt-out scan, and that no plugin is autoloaded. |
| `tests/test_docs.py` | Every ADR in `docs/decisions/` follows `.claude/skills/adr/SKILL.md`: numbered without gaps (at least six), the skill's sections in order, an allowed status, a valid ISO date, a date on every source (one `- ` item per source, continuation lines indented), the product name spelled out exactly once, at most 600 words outside fenced blocks and the sources. Any other file in `docs/decisions/` is reported. The README says what the project shows, with milestones, and what it does not. |
| `tests/test_embedding_config.py` | `app/config/embedding.py` agrees with the yaml block in ADR 0005, field by field. `DIM` and `MAX_TOKENS` match the model's own `config.json` (committed as `app/config/embedding_model_config.json`), whose sha256 is in `FILES`. The revision is a 40-hex commit; `FILES` holds plain relative paths to `.json` and `.onnx` files only, with 64-hex digests, and cannot be changed at runtime. Only `config.json` is hashed here; the ONNX digests are Hugging Face LFS oids, and the model download step in M1 checks their bytes. `.gitattributes` keeps git from converting the config's line endings. |
| `tests/test_lock.py` | `uv.lock` records the 7-day span (`P7D`); every package except the project (one editable entry, nothing to download) comes from PyPI; every file has a sha256 and a time-zoned upload time; no recorded upload lies inside the 7-day window before the test run. `upload-time` is trusted as written; re-resolving in CI is the control against a backdated hand edit (ADR 0006, PR 4). Messages show source hosts only, never credentials or query strings. |
| `tests/test_repo_files.py` | The shared file lister ignores `GIT_*` variables (as set inside a git hook) and shows git's error on failure. |
| `tests/guardrails/test_block_secrets.py` | The secret hook in `.claude/hooks/` blocks what it must and allows the rest. |

Scans cover tracked files and untracked files that are not ignored (`git ls-files --cached --others --exclude-standard`), so a new file is checked before it is committed. The data-directory check walks the file system instead, so ignored output counts too.
