# Local setup

M0, PR 1 and PR 3. SITREP (situation report) builds and tests locally with uv and make, offline, with no `.env` and no API key. Later PRs add the database and the browser test; each one extends this runbook in the same PR. `tests/test_runbook.py` fails when the Makefile and this file drift apart.

## Prerequisites

- **uv** 0.13.5 or later, below 0.14 (`required-version` in `pyproject.toml`). Update with `uv self update`.
- **GNU make** and **git**.
- **Python 3.12** is not a prerequisite: uv installs the version in `.python-version` on first use.
- **Docker** (Docker Desktop on macOS; Engine 28 or later on Linux) for `make docker-build` only. `make test` does not need it.

## Commands

| Command | What it does |
|---|---|
| `make help` | Lists the targets. |
| `make test` | Runs `make lint`, then pytest over `tests/` without `tests/web`. Offline. |
| `make lint` | `ruff check` and `ruff format --check`. |
| `make fmt` | `ruff format`. |
| `make docker-build` | Builds the image `sitrep:local`, then smoke-tests it (below). Needs Docker and the network for the base images. |

The first `make test` creates `.venv/` from `uv.lock`.

## Running the app

```sh
uv run python -P -m app
```

It serves on `http://127.0.0.1:8000` (`HOST` and `PORT` change that). Routes in M0: `/`, a placeholder page, and `/health`, which answers `{"status":"ok"}`. There are no docs or OpenAPI routes, and a trailing slash is a 404, never a redirect.

- **Settings** come from the environment. `python -m app` is the only place that also reads a `.env` in the working directory; `Settings()` elsewhere, tests included, never does. Variable names are in `.env.example`.
- **Flags** (`DEV_AUTH`, `TRACE_PLAINTEXT`) are on only for exactly `1`. `true`, `yes` or ` 1` are off.
- **Traces** go to stdout, one JSON span per line, through the allowlist of ADR 0007: method, route template, status code, scheme and protocol version. No path, query, header, cookie, client address or exception message.
- **Logs** go to stderr: time, level, logger, the message template without its values, the exception type (and errno) and the trace id. No access log.
- **Every response** carries `X-Trace-Id`, the strict CSP and the other security headers; API responses also `Cache-Control: no-store`.
- **`TRACE_PLAINTEXT=1`** prints spans unfiltered, for local debugging only. The app refuses to start with it (exit code 2) unless it binds exactly `127.0.0.1`.

Check it by hand:

```sh
curl -i 'http://127.0.0.1:8000/health?q=secret'   # headers; the span on stdout has no q=secret
curl -i 'http://127.0.0.1:8000/health/'           # 404
```

## The image

`make docker-build` builds the `Dockerfile` and checks the result:
- the image runs as uid 10001, not root;
- a container published on `127.0.0.1` only, on a free port, answers `/health` and `/`;
- a container with `TRACE_PLAINTEXT=1` exits non-zero, because the image binds `0.0.0.0`.

The bases are pinned by digest: `python:3.12.16-slim-trixie` and `ghcr.io/astral-sh/uv:0.13.5`, the exact uv that `required-version` starts at. The install is `uv sync --locked --no-dev`. The build context is deny-by-default (`.dockerignore`): only `pyproject.toml`, `uv.lock` and `app/` go in, never a `.env`, `.git`, `.claude/` or `tests/`. The image sets `HOST=0.0.0.0` and `DEV_AUTH=0`. BuildKit warns about `DEV_AUTH` under `SecretsUsedInArgOrEnv`; the name trips the check, the value is a flag.

## How the toolchain is pinned

- **Lock.** `uv.lock` pins every package with hashes. The Makefile exports `UV_LOCKED=1`, so uv fails instead of rewriting the lock when it disagrees with `pyproject.toml`. After a dependency change, run `uv lock` and commit both files.
- **Minimum release age: 7 days.** `exclude-newer = "7 days"` in `[tool.uv]`: nothing uploaded to PyPI in the last 7 days is resolved. uv records the span in the lock (`exclude-newer-span = "P7D"`), not a timestamp, so the lock does not go stale as days pass. uv 0.13.5 and 0.13.6 both accept it. The policy does not cover the build backend `uv_build`, which the lock never resolves; the exact uv pin is the control there (ADR 0006). It is in place in the Dockerfile (PR 3; the image build does not download `uv_build`) and arrives in CI with PR 4.
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

Later PRs keep those offline through loopback-only URLs and scrubbed environments. The first scrub is in place: `tests/conftest.py` removes `HTTP_PROXY`, `HTTPS_PROXY` and `ALL_PROXY` (any case) and sets `NO_PROXY` and `no_proxy` to `*` for every test and the subprocesses it starts. It also removes the app's own settings variables, so the shell does not leak into a test.

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
| `tests/test_app.py` | `/health` and its `X-Trace-Id`; the exact security headers on `/`, `/health`, a 404 and a 500; the page title from branding; no 3xx and no `Location` for any route, with and without a trailing slash, under eight methods and a foreign Host; no inbound trace context adopted; the default exporter chain, and that the global tracer provider stays unset. |
| `tests/test_no_plaintext.py` | A real uvicorn server from `server_config()`, with a canary in the query, Host, a custom header, user agent, cookie, referrer, body, a 404 path and an exception message, and with OpenTelemetry's header-capture and resource variables switched on: the canary is in no span, log record, stdout or stderr. The allowlists equal the reviewed ones. A failed log write prints no exception text. |
| `tests/test_settings.py` | Flags on only for exactly `1`; `TRACE_PLAINTEXT=1` only with the console exporter on `127.0.0.1`; the entry point exits 2 otherwise, from the environment or a `.env`; `Settings()` ignores a `.env`; the proxy scrub, checked in a pytest subprocess started with all six proxy variables set. |
| `tests/test_import_allowlist.py` | The modules `app/` imports equal a reviewed list. HTTP clients, sockets, `subprocess`, `importlib`, `__import__`, `eval`, `os.system`, `os.exec*` and connection calls are reported, with planted cases. The scan reads names in the syntax tree, not strings. |
| `tests/test_dockerfile.py` | Bases pinned by digest; the uv image tag equals the floor of `required-version`; `uv sync --locked --no-dev`; no `ADD`, `curl` or `pip install`; uid 10001; `CMD ["python","-P","-m","app"]`; a deny-by-default `.dockerignore`. |
| `tests/guardrails/test_block_secrets.py` | The secret hook in `.claude/hooks/` blocks what it must and allows the rest. |

Scans cover tracked files and untracked files that are not ignored (`git ls-files --cached --others --exclude-standard`), so a new file is checked before it is committed. The data-directory check walks the file system instead, so ignored output counts too.
