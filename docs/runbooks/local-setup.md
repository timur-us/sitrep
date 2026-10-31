# Local setup

M0, PRs 1, 3, 4, 5a and 5b. SITREP (situation report) builds and tests locally with uv, make and a PostgreSQL container, offline, with no `.env` and no API key. Every CI gate is a make target, so it runs here the same way. Each PR extends this runbook in the same PR. `tests/test_runbook.py` fails when the Makefile and this file drift apart.

## Prerequisites

- **uv** 0.13.5 or later, below 0.14 (`required-version` in `pyproject.toml`). Update with `uv self update`.
- **GNU make** and **git**.
- **Python 3.12** is not a prerequisite: uv installs the version in `.python-version` on first use.
- **Docker** with Compose v2 (Docker Desktop on macOS; Engine 28 or later on Linux, because earlier engines expose localhost-published ports to the local network segment), for the database (`make up`) and `make docker-build`. `make test` needs the database.
- **`curl` and `seq`**, for `make docker-build`.
- **Port 55432 free on 127.0.0.1**, for the database. It is not 5432, so a native PostgreSQL there never answers in its place.
- **The network**, for `make audit` (PyPI and the vulnerability data), `make web-deps` (Chromium, about 100 MB, from Playwright's CDN), `make docker-build` (the base images) and the first `make up` (the pgvector image).

## Commands

| Command | What it does |
|---|---|
| `make help` | Lists the targets. |
| `make up` | Starts PostgreSQL with pgvector (`compose.yaml`) on `127.0.0.1:55432` and waits until it answers (below). |
| `make down` | Stops it and deletes its data. |
| `make migrate` | Applies `db/migrations` as the migration role (below). |
| `make test` | Runs `make lint`, then pytest over `tests/` without `tests/web`. Offline. `tests/db` needs `make up` and `make migrate` first, and fails, never skips, without them. |
| `make lint` | `ruff check` and `ruff format --check`. |
| `make fmt` | `ruff format`. |
| `make lint-test` | CI's lint-test job: `make up`, `make migrate`, then `make test`. Its `tests/test_secret_history.py` scans the branch's commits for secrets. |
| `make audit` | Re-resolves the lock against PyPI (`uv lock --check --refresh`), then runs pip-audit over an export of the lock with hashes. Needs the network. |
| `make docker-build` | Builds the image `sitrep:local`, then smoke-tests it (below). Needs Docker and the network for the base images. |
| `make web-deps` | Downloads Chromium for Playwright, without `--with-deps` (no apt, no root). |
| `make web-test` | `make web-deps`, then the browser smoke test in `tests/web` (below). |
| `make evals` | The eval gate, offline (below). |
| `make redteam` | The red-team gate, offline (below). |

The first `make test` creates `.venv/` from `uv.lock`. From a fresh clone:

```sh
make up
make migrate
make test
```

## The database

`compose.yaml` runs one service, `db`: `pgvector/pgvector:0.8.7-pg17-bookworm`, pinned by digest (PostgreSQL 17.11). It publishes on `127.0.0.1:55432` only and mounts `db/init` read-only. There are no variables and no env file.

- **Trust auth.** The container trusts every connection into it, and is reachable only through that 127.0.0.1 publish. It is a local convenience for synthetic data: any local process can connect as any role, the superuser included. Azure is Entra-only from M3.
- **Roles** (`db/init/00-roles.sql`, run once when the data directory is created; ADR 0008):
  - `sitrep_owner`: NOLOGIN; owns the database, the schemas and everything in them;
  - `sitrep_migrator`: LOGIN; holds `sitrep_owner` `WITH INHERIT FALSE`, so it has the owner's rights only after `SET ROLE`;
  - `sitrep_app`: LOGIN; no attributes and no memberships; owns nothing, can create nothing, and can connect to `sitrep` only.

  `vector` is installed in `public`, and `public` belongs to the superuser. PUBLIC cannot connect to `postgres` or `template1` and cannot create large objects. The owner's future functions and types are not granted to PUBLIC, except a table's row type, which PUBLIC may always use and which opens no rows.
- **URLs.** `DATABASE_URL` (the app role) and `MIGRATION_DATABASE_URL` (the migrator) default to `127.0.0.1:55432` (`.env.example`).
  - Each one must name one host, exactly `127.0.0.1`, an explicit port, its own role and the database `sitrep`, and nothing else: no password, `hostaddr`, `options` or `service`. Otherwise `Settings()` refuses it, without echoing the value.
  - `app/db/connect.py` is the only place that opens a connection. It refuses to run while any `PG*` variable is set in your shell (`PGPORT`, `PGHOST`, `PGUSER`, `PGOPTIONS`, `PGTZ` and the rest), naming the variables, not their values: unset them. It takes no connection parameter as a keyword argument, and pins `hostaddr` and `options` as well.
- **`make migrate`** runs `python -P -m app.db.migrate`:
  - it connects as the migrator, runs `SET ROLE sitrep_owner`, and refuses any other login;
  - it records each file's sha256, `session_user` and `current_user` in `sitrep_meta.schema_migrations`;
  - it stops before any DDL if an applied file changed or is missing;
  - new files run in one transaction; a file that commits, rolls back, or changes the role or `search_path` is refused and not recorded, though what it committed itself stays.

  Files are `db/migrations/NNNN_name.sql`, numbered without gaps and forward only: a fix is a new file.
- **Tables and row-level security** (`0002_tables_rls.sql`): `advisors`, `mandates`, `documents` and `chunks`, owned by `sitrep_owner`, `SELECT` only for `sitrep_app`. Every key includes `advisor_id`. Each table has ENABLE and FORCE ROW LEVEL SECURITY and one policy: a row is visible only while the transaction's `app.advisor_id` names its advisor. With none set, every query returns zero rows, for the owner too. `documents.lang` is `german` or `english`, and its generated `tsv` uses that language; `chunks.embedding` is `vector(384)` with an HNSW index.
- **The app's pool** (`app/db/session.py`) connects through `app/db/connect.py`, lazily: the app starts with the database down. Each connection is checked when it is made and at every checkout (app role, no attributes or memberships, no advisor setting, its own `search_path` and no other session-level setting, its role settings as `db/init` wrote them, no held cursor, LISTEN or advisory lock) and closed if anything differs. `advisor_transaction()` is the only way to act for an advisor: a real transaction with `set_config('app.advisor_id', %s, true)`, from a `VerifiedAdvisor` that only `app/auth/` issues.
- **`make down`** deletes the data, and the next `make up` runs `db/init` again. After a change to `db/init`, run `make down`, `make up` and `make migrate`.

Check it by hand:

```sh
docker compose port db 5432                                      # 127.0.0.1:55432
docker compose exec db psql -U postgres -d sitrep -c '\du'       # the three roles and their attributes
docker compose exec db psql -U postgres -d sitrep -c 'SELECT version()'
```

Row-level security, by hand: the superuser plants one advisor row in a transaction, sees it, and the app role, with no advisor set, does not. The rollback leaves nothing behind.

```sh
docker compose exec db psql -U postgres -d sitrep -c 'BEGIN' \
  -c "INSERT INTO sitrep.advisors VALUES ('probe', 'Probe')" \
  -c 'SELECT count(*) AS superuser_sees FROM sitrep.advisors' \
  -c 'SET LOCAL ROLE sitrep_app' \
  -c 'SELECT count(*) AS app_sees FROM sitrep.advisors' \
  -c 'ROLLBACK'                                               # superuser_sees 1, app_sees 0
```

## Running the app

```sh
uv run python -P -m app
```

It serves on `http://127.0.0.1:8000` (`HOST` and `PORT` change that). Routes in M0: `/`, a placeholder page, and `/health`, which answers `{"status":"ok"}`. There are no docs or OpenAPI routes, and a trailing slash is a 404, never a redirect. The app holds a pool on `DATABASE_URL` that connects nobody until a route needs it, so it starts and answers `/health` with the database down.

- **Settings** come from the environment. `python -m app` is the only place that also reads a `.env` in the working directory; `Settings()` elsewhere, tests included, never does. Variable names are in `.env.example`.
- **Flags** (`DEV_AUTH`, `TRACE_PLAINTEXT`) are on only for exactly `1`. `true`, `yes` or ` 1` are off.
- **Traces** go to stdout, one JSON span per line, through the allowlist of ADR 0007: method (standard ones only), route template, status code, scheme and protocol version. No path, query, header, cookie, client address or exception message. Trace ids come from `secrets`.
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
- a container published on `127.0.0.1` only, on a free port, answers `/health` and `/`; a canary sent in its query and a header is absent from `docker logs`, which do show the span;
- a container with `TRACE_PLAINTEXT=1` exits with code 2 and `refused to start`, because the image binds `0.0.0.0` by default. Overriding `HOST=127.0.0.1` inside the container would pass the check; do not.

The bases are pinned by digest: `python:3.12.16-slim-trixie` and `ghcr.io/astral-sh/uv:0.13.5`, the exact uv that `required-version` starts at. The install is `uv sync --locked --no-dev`. The build context is deny-by-default (`.dockerignore`): only `pyproject.toml`, `uv.lock` and `app/` go in, never a `.env`, `.git`, `.claude/` or `tests/`. The image sets `HOST=0.0.0.0` and `DEV_AUTH=0`. BuildKit warns about `DEV_AUTH` under `SecretsUsedInArgOrEnv`; the name trips the check, the value is a flag.

## How the toolchain is pinned

- **Lock.** `uv.lock` pins every package with hashes. The Makefile exports `UV_LOCKED=1`, so uv fails instead of rewriting the lock when it disagrees with `pyproject.toml`. After a dependency change, run `uv lock` and commit both files.
- **Minimum release age: 7 days.** `exclude-newer = "7 days"` in `[tool.uv]`: nothing uploaded to PyPI in the last 7 days is resolved. uv records the span in the lock (`exclude-newer-span = "P7D"`), not a timestamp, so the lock does not go stale as days pass. uv 0.13.5 and 0.13.6 both accept it. The policy does not cover the build backend `uv_build`, which the lock never resolves; the exact uv pin is the control there (ADR 0006). It is in place in the Dockerfile (PR 3; the image build does not download `uv_build`) and in CI (setup-uv `version: 0.13.5`, PR 4); a test keeps the two equal.
- **`sys.path`.** pytest runs as `python -P -m pytest`, so the working directory is not put in front of the standard library. The editable install still appends the repository root (its `.pth` file, a consequence of `module-root = ""`), so `evals`, `redteam` and `data` are importable as namespace packages. `tests/test_data_dirs.py` is what keeps the data in them inert.
- **pytest plugins load by name only.** `--disable-plugin-autoload -p pytest_socket`: an installed package's `pytest11` entry point (platformdirs, a dependency of pip-audit, declares one) does not run.

## CI

`.github/workflows/ci.yml` runs six jobs on every pull request and every push to `main`: `lint-test`, `pip-audit`, `docker-build`, `web-test`, `evals`, `redteam`. Each one is a checkout pinned by commit SHA (no persisted credentials), setup-uv pinned the same way with uv 0.13.5 and its cache off, and one `make` target. The token is read-only; there are no secrets, no cloud login and no service containers. On a pull request a new push cancels the running checks; on `main` it does not. `tests/ci/` pins the workflow, the Dependabot config and the targets. The job ids are the check names the ruleset requires.

`lint-test` checks out the full history (`fetch-depth: 0`) for the history scan; a shallow clone fails it. The tests run as a non-root user, so the unreadable-directory case in `tests/test_data_dirs.py` runs instead of skipping.

`lint-test` starts the database with `make up` (the same `compose.yaml`, on the runner's Docker) and migrates it before the tests. It is not a service container.

Dependabot (`.github/dependabot.yml`) proposes updates for actions, the uv lock, the Docker images and the image in `compose.yaml`: weekly, grouped, and only for releases at least 7 days old.

## Evals and red team in M0

`make evals` and `make redteam` run `python -P -m app.runners.evals` and `…redteam` over `evals/` and `redteam/`. Each suite has a `gate.toml` (`min_cases`), a case directory (`evals/cases`, `redteam/attacks`, one `<name>.json` per case) and `recorded/<version>/<case id>.json`. In M0 the runners check the corpus and score nothing:

- an empty corpus passes and prints `evals: no cases (min_cases=0)`; a corpus with cases fails closed until scoring arrives in M1;
- a stray file or symlink, invalid JSON, a duplicate key or id, an orphan recording, or a recording with an `authorization`, `proxy-authorization`, `x-api-key`, `api-key`, `ocp-apim-subscription-key`, `cookie` or `set-cookie` header fails, as a key or as a value (HAR-style name/value pairs). Header values are not scanned here; `tests/test_no_secrets.py` scans every committed file, recordings included;
- text is read as NFKC, with ideographic dots as `.` and without invisible format characters (zero-width, tags), before the rules below;
- cases name the cast, the bank, the product and the domains only through placeholders such as `{advisor.<id>}`, `{email.<id>}`, `{domain.attacker}` and `{mandate.demo}`, resolved from `app/config/branding.py`; a literal name or an unknown placeholder fails;
- after resolution, every host in a URL (a scheme with one or more slashes or backslashes, or `//`; tab, CR and LF before a dot are dropped, as browsers do), an e-mail address or a bare name must end in the label `example`. IP literals, `localhost`, `xn--` and non-ASCII labels fail. A bare file name such as `report.pdf` reads as a host: put it inside a URL, or write it without the dot;
- `--live` needs `--yes`, and live runs are refused in M0.

Errors name the file (escaped unless it is a plain name, so no file name can start a GitHub workflow command), the JSON path and the rule, never the text. `min_cases` must equal the committed case count (`tests/runners/test_runner_gate.py`), so deleting a case alone fails; deleting it and lowering `min_cases` in the same change passes, and the ask on `gate.toml` and the review are the control for that.

## The browser smoke test

`make web-test` starts the app as `python -P -m app` on `127.0.0.1` and a free port, from an empty directory with an environment built from scratch, and opens `/` in headless Chromium. Chromium gets an environment built from scratch as well. Its host-resolver rules answer "not found" for every name and every address except `127.0.0.1`, so the browser reaches nothing else; WebRTC may use proxied UDP only, and there is no proxy (that flag is not tested). The test checks the status, the CSP's exact `img-src`, the title from branding, that no request leaves the page's origin (service workers blocked, WebSockets watched) and that no CSP violation fires. Controls show each listener works and the rules hold: a blocked inline script raises a violation, a request to another loopback port and a WebSocket are recorded, and `localhost` and `192.0.2.1` do not resolve.

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

Later PRs keep those offline through loopback-only URLs and scrubbed environments. The first scrub is in place: `tests/conftest.py` removes `HTTP_PROXY`, `HTTPS_PROXY` and `ALL_PROXY` (any case) and sets `NO_PROXY` and `no_proxy` to `*` for every test and the subprocesses it starts. It also removes the app's own settings variables, so the shell does not leak into a test, and every `PG*` variable, which libpq would read and `app/db/connect.py` refuses.

## Repository checks in the test run

| Test | What it holds |
|---|---|
| `tests/test_branding.py` | `app/config/branding.py` is the only file outside prose, `.claude/` and recordings that names the product, the bank, a domain, a cast member or the demo mandate. Infrastructure files that cannot import Python may carry the slug. The values match `CLAUDE.md`. |
| `tests/test_no_secrets.py` | In any file git sees, none of these shapes: an Anthropic key, an AWS access key id, a GitHub token, a private-key block, an Azure storage `AccountKey=`, a password in a URL, or a JWT. Other secret shapes are not detected. The only allowed match is the fake key in `tests/guardrails/test_block_secrets.py`, pinned by its fingerprint (16 hex digits of the match's sha256); any other match in that file is reported. Findings show file, line and pattern. The text is never shown, and the fingerprint is not either, because the fingerprint of a URL password can be reversed with a dictionary. To allowlist a match on purpose, compute its fingerprint locally with `find_secrets()`. |
| `tests/test_runbook.py` | Every Makefile target is documented here, has a `##` comment, is phony and is listed by `make help`; every `make` command here exists. |
| `tests/test_data_dirs.py` | `evals/cases`, `evals/recorded`, `redteam/attacks`, `redteam/recorded`, `data/synthetic/templates` and `data/synthetic/out` hold only `.json`, `.jsonl`, `.toml`, `.yaml`, `.yml` and `.txt` files (plus `.gitkeep`); the report directories `evals/results` and `redteam/results` hold only `.md`. No `conftest`, no executable bit, no symlinks, no unreadable directories. The first four exist with a `.gitkeep` in git, so the check cannot pass on an empty tree. |
| `tests/test_network.py` | The offline guarantee above: the block itself, the effective configuration, the opt-out scan, and that no plugin is autoloaded. |
| `tests/test_docs.py` | Every ADR in `docs/decisions/` follows `.claude/skills/adr/SKILL.md`: numbered without gaps (at least six), the skill's sections in order, an allowed status, a valid ISO date, a date on every source (one `- ` item per source, continuation lines indented), the product name spelled out exactly once, at most 600 words outside fenced blocks and the sources. Any other file in `docs/decisions/` is reported. The README says what the project shows, with milestones, and what it does not. |
| `tests/test_embedding_config.py` | `app/config/embedding.py` agrees with the yaml block in ADR 0005, field by field. `DIM` and `MAX_TOKENS` match the model's own `config.json` (committed as `app/config/embedding_model_config.json`), whose sha256 is in `FILES`. The revision is a 40-hex commit; `FILES` holds plain relative paths to `.json` and `.onnx` files only, with 64-hex digests, and cannot be changed at runtime. Only `config.json` is hashed here; the ONNX digests are Hugging Face LFS oids, and the model download step in M1 checks their bytes. `.gitattributes` keeps git from converting the config's line endings. |
| `tests/test_lock.py` | `uv.lock` records the 7-day span (`P7D`); every package except the project (one editable entry, nothing to download) comes from PyPI; every file has a sha256 and a time-zoned upload time; no recorded upload lies inside the 7-day window before the test run. `upload-time` is trusted as written; re-resolving in CI is the control against a backdated hand edit (ADR 0006, PR 4). Messages show source hosts only, never credentials or query strings. |
| `tests/test_repo_files.py` | The shared file lister ignores `GIT_*` variables (as set inside a git hook) and shows git's error on failure. |
| `tests/test_app.py` | `/health` and its `X-Trace-Id`; the exact security headers on `/`, `/health`, a 404 and two 500s (one from an exception that refuses attributes), each with its span's trace id; headers a route sets are replaced; the page title from branding; no 3xx and no `Location` for any route, with and without a trailing slash, nor for open-redirect shapes such as `//host/`, under eight methods, a foreign Host and forwarding headers; no inbound trace context or trace state adopted, sampled, unsampled or malformed; the default exporter chain; the global tracer provider stays unset. |
| `tests/test_no_plaintext.py` | A real uvicorn server from `server_config()`, with a canary in the method, query, Host, custom, authorization, forwarding and trace-context headers, user agent, cookie, referrer, body, a 404 path and an exception message, and with OpenTelemetry's header-capture, raw-method and resource variables switched on: the canary is in no span, log record, stdout or stderr, and each response's `X-Trace-Id` is its span's. A control runs the same requests with an unfiltered exporter and uvicorn's default logging, where the canary does leak. Also: `redact()` on a hand-built span; the allowlists equal the reviewed ones; trace ids from `secrets`; `LOG_CONFIG` names only this project's classes; the formatter redacts any logger; a failed log write prints no exception text. |
| `tests/test_settings.py` | Flags on only for exactly `1`, each on its own; ports in range; `.env.example` safe, with both database URLs equal to the defaults; `TRACE_PLAINTEXT=1` only with the console exporter on `127.0.0.1`; the entry point exits 2 otherwise, from the environment or a `.env`, and starts on loopback with plaintext or on `0.0.0.0` without it; `Settings()` ignores a `.env`; the environment scrub (proxies in any case, `OTEL_*`, `PG*`, the settings' variables), checked in a pytest subprocess started with all of them set. |
| `tests/test_sql_hygiene.py` | In `app/`, every `.execute`, `.executemany`, `.copy` and `.stream` takes a string literal or a constant of `app/db/queries.py`, which holds literals only, never `**kwargs`; the statement methods are only called, and an imported constant is never rebound; no `psycopg.sql` (under any import name), `ClientCursor`, `mogrify`, `cursor_factory` or `pgconn` (`app/db/migrate.py` excepted from these); `set_config` and `app.advisor_id` only in `app/db/session.py`, in any case or spacing; no string says `SET LOCAL`, `SET SESSION` or `SET app.`. Planted cases for each rule. A lint, not a proof: its docstring lists what it cannot see. |
| `tests/test_identity.py` | Only `app/auth/identity.issue()` makes a `VerifiedAdvisor`: the constructor refuses a call without the issuer token, and an instance cannot be changed, copied, pickled, replaced or subclassed; pydantic cannot build one, and a FastAPI route cannot take one from a request. A scan of `app/` outside `app/auth/` allows the class in annotations, `isinstance()` and `is` checks only, and reports `issue()`, the token and `__setattr__`. The advisor transaction refuses a string, a dict, a look-alike and None with `TypeError`, before it reaches the pool. |
| `tests/test_app_without_db.py` | A real server from `server_config()`, with `DATABASE_URL` on a closed port, starts and answers `/health` within 2 s; its pool is open, empty (`min_size` 0) and has tried no connection; the server stops and the pool is closed. |
| `tests/test_import_allowlist.py` | The modules `app/` imports equal a reviewed list. HTTP clients, sockets, `subprocess`, `importlib`, `logging.handlers`, `opentelemetry.exporter`, `__import__`, `eval`, `os.system`, `os.exec*`, `RedirectResponse` and connection or server calls are reported, by dotted name, with planted cases. A `.connect` outside `app/db/connect.py` is reported too. `psycopg_pool` is reviewed in for `app/db/session.py` only, where its connections go through `app/db/connect.py` (`tests/db/test_pool.py`). The scan reads names in the syntax tree, not strings. |
| `tests/test_dockerfile.py` | Bases pinned by digest; every `--from=` names an earlier stage; the uv image tag equals the floor of `required-version`; `uv sync --locked --no-dev`, the last one `--no-editable`; no `ADD`, `curl` or `pip install`; uid 10001; `CMD ["python","-P","-m","app"]`; a deny-by-default `.dockerignore`. |
| `tests/guardrails/test_block_secrets.py` | The secret hook in `.claude/hooks/` blocks what it must and allows the rest. |
| `tests/test_secret_history.py` | Every line the branch's commits add since the merge base with `main`, each new path and every commit message, against the patterns and the allowlist of `tests/test_no_secrets.py`. Patches are read as text, binary files included; renames count as added lines; a merge counts with the lines its conflict resolution adds. A key added and removed again is reported. Findings show commit, path, line and pattern (a path with a key shows as `<path>`). A shallow clone fails. On `main` the range is empty by design. A finding means rotating the key: the commits stay reachable through the pull request. |
| `tests/ci/test_workflow.py` | The six jobs, their triggers, read-only permissions, PR-only cancelling, timeouts and runner; the steps (checkout pinned, without credentials; setup-uv with the Dockerfile's uv and no cache; one make target); no `if`, `continue-on-error`, services, containers (one digest-pinned Playwright fallback is allowed on `web-test`), expressions in `run`, secrets or cloud logins. Dependabot: four ecosystems (`docker-compose` included), weekly, grouped, 7-day cooldown. Planted cases for each rule. |
| `tests/ci/test_make_targets.py` | `make -n` of each CI target shows its key commands; `lint-test` starts (`docker compose -f compose.yaml`) and migrates the database before pytest; `make down` deletes the data; `web-deps` uses no `--with-deps`, sudo or apt; no recipe ignores a failure (a leading `-`, `\|\| true`, `; true`, `.IGNORE`, `MAKEFLAGS` with `-i`); pytest still blocks sockets. |
| `tests/ci/test_ci_user.py` | The tests do not run as root. |
| `tests/db/test_app_role.py` | Through `DATABASE_URL`: the login is `sitrep_app`, with no superuser, BYPASSRLS, CREATEROLE, CREATEDB or REPLICATION and no `pg_auth_members` row. It owns no database, schema, relation or other object (`pg_shdepend`). It has CREATE on no schema, neither CREATE nor TEMPORARY on the database, and no privilege on `sitrep_meta` or its table. The database and everything in `sitrep` and `sitrep_meta` belong to `sitrep_owner`. The ACLs of the database and of `sitrep`, `sitrep_meta` and `public` are exact, and `public` belongs to the superuser. Controls run the same catalog queries for the owner and for a planted role with attributes and a membership. |
| `tests/db/test_migration_role.py` | Every applied file is recorded with its sha256, `session_user` migrator and `current_user` owner. The three roles' attributes, the one membership (owner to migrator; inherit off, set on, admin off) and the role settings (`search_path` only) are exact. The migrator without `SET ROLE` creates nothing. With a migration pending, the runner refuses the app role (42501) and the superuser and leaves no trace. It applies nothing twice, stops with no DDL on a changed or missing applied file, rolls the whole run back when a file fails, and refuses, unrecorded, a file that commits, rolls back, resets the role or sets the `search_path`. `load()` refuses gaps, duplicates, bad names and symlinks. The entry point refuses a bad URL without echoing it. The app role can create no table (qualified, unqualified or temporary), schema, function, role, database or large object, cannot alter its role or `SET ROLE`/`SET SESSION AUTHORIZATION` to another. Neither login role connects to `postgres` or `template1`; the owner cannot log in. The owner's new functions and types are not usable by the app role. |
| `tests/db/test_vector_extension.py` | PostgreSQL 17; `vector` in `public` at the version `compose.yaml`'s image tag names, usable by the app role; `chunks.embedding` is `vector(384)`, NOT NULL, with an HNSW `vector_cosine_ops` index, next to the model and revision columns. |
| `tests/db/test_rls_catalog.py` | Discovery as the app role over every non-system schema: everything it can read is a table with row-level security enabled and forced (or a `security_invoker` view; materialized views and foreign tables are refused), and that is exactly `advisors`, `mandates`, `documents`, `chunks`. One policy per table, of the canonical shape. Grants: the exact ACL on each table, `SELECT` only, no column grants, no sequence, no function, no type but the tables' row types. No SECURITY DEFINER function. `advisor_id` in every unique index and primary key, in every foreign key on both sides at the same place, and in any exclusion constraint; a required `advisor_id` in every table. Each check has a control that plants what it must report (a view, a table and a column grant, a sequence, a function and a type, a SECURITY DEFINER function, a foreign key and an exclusion constraint without `advisor_id`) and runs the same query. |
| `tests/db/test_rls_rows.py` | One seeded row per table and advisor (`tests/db/seed_rows.py`; a discovered table without a factory fails). Fail closed: under one advisor, exactly the superuser's count of that advisor's rows; with no context, and with the `''` a reused connection carries, zero; rows whose `advisor_id` is `''` stay hidden whether the setting is unset or `''` (a control drops `NULLIF` from a policy and sees them); the owner under FORCE sees nothing and updates or deletes nothing without a context. Isolation: another advisor's rows are invisible by key; the app role cannot insert, update or delete; the owner writes its advisor's own rows but cannot insert another advisor's row, move a row to another advisor, or change another advisor's rows; odd ids (empty, `%`, other case, a trailing space, a quote) see nothing. Forged parameters in English and German: live when spliced into the search statement and run through a `ClientCursor` (the setting moves to the other advisor), inert when bound (the advisor's own rows only, the setting unchanged). |
| `tests/db/test_pool.py` | The pool connects as the app role; with `PGHOSTADDR`, `PGOPTIONS` or `PGTZ` set it connects nobody, where a plain `psycopg_pool` pool takes `PGOPTIONS` (control). The guard reports each planted state (superuser, BYPASSRLS, REPLICATION, a membership either way, defaults for the role, for all roles, for the database or replaced for this database, a session-level advisor setting or other setting, a foreign `search_path`, a held cursor, an advisory lock, LISTEN), a session that switched role, another login, and the app role's own `ALTER ROLE` of `search_path`, `row_security` or `statement_timeout`, for all databases or this one. The app role cannot put `app.advisor_id` into its defaults; `REVOKE SET ON PARAMETER` does not stop `set_config` (the residual risk). Pools on the superuser and migrator URLs connect nobody. Session-level state gets the connection closed and replaced at the next checkout, and a clean one is reused through twelve checkouts. `check()` closes a connection that is not idle, not the app role, or whose query fails. The advisor transaction is a real transaction, takes an idle connection only, leaves `''` behind, and rolls back cleanly on an error. The app's lifespan opens and closes its pool. |
| `tests/db/test_fts_language.py` | German and English stemming through their own search statements; each statement skips the other language's document that shares a lexeme; the generated `tsv` uses the document's language; `french` and NULL are refused, by the named constraint and column. A scan of `app/` and `db/` refuses any text-search call whose first argument is not `'german'` or `'english'`, with planted cases. |
| `tests/db/test_connect.py` | Controls: on a plain connect, `PGOPTIONS`, `PGTZ`, `PGPORT` and `PGHOSTADDR` take effect. `connect()` refuses each of ten `PG*` variables, naming it but not its value; refuses a keyword argument that is a connection parameter (`user`, `host`, `port` and the rest); accepts connection options; checks the URL before libpq runs. |
| `tests/db/test_compose.py` | Only `name` and `services` at the top; one service, with only image, environment, ports, volumes and healthcheck; ports on `127.0.0.1:` only; no `network_mode`; trust on `db` only; the image pgvector on pg17 by digest; `db/init` read-only; the exact healthcheck (TCP, the app role); no `env_file` and no variables; no override or other compose file in the repository. Planted cases for each rule. |
| `tests/db/test_database_urls.py` | Both URLs refuse a non-loopback, named, short-form, IPv6, IPv4-mapped, trailing-dot, second or socket host; a missing or bad port (0, 65536, sign, letters, non-ASCII digits); `options`, `hostaddr` (also percent-encoded), `service`, `sslmode`, a password, a duplicate `host`; a missing or empty user, host or database; another role than the field's own, and another database. The value is never echoed. Loopback URLs and the keyword form are accepted. |
| `tests/db/test_no_skips.py` | No `skip`, `skipif`, `xfail` or `importorskip` in `tests/db`, with planted cases. |
| `tests/runners/` | The runners above: exit codes, messages, the gate ratchet, stray files, ids, recordings, placeholders, literals, hosts, live refusal, and no connection attempted with a dummy key set. |

Scans cover tracked files and untracked files that are not ignored (`git ls-files --cached --others --exclude-standard`), so a new file is checked before it is committed. The data-directory check walks the file system instead, so ignored output counts too.
