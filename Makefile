# Every public target carries a "##" comment: `make help` lists them, and
# tests/test_runbook.py keeps them in step with docs/runbooks/local-setup.md.

.DEFAULT_GOAL := help
SHELL := /bin/sh

# uv fails instead of rewriting uv.lock when pyproject.toml and the lock disagree.
export UV_LOCKED := 1

# The local image tag. The smoke test publishes on 127.0.0.1 only, on a free port.
IMAGE := sitrep:local

.PHONY: help up down migrate model lint fmt test lint-test audit docker-build web-deps web-test evals redteam data

help: ## List the targets
	@grep -E '^[a-z][a-z0-9-]*:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "  %-13s %s\n", $$1, $$2}'

# The database (compose.yaml, ADR 0008): PostgreSQL 17 with pgvector on 127.0.0.1:55432,
# trust auth, synthetic data only. db/init runs on an empty data directory only, so
# `down` deletes the data and the next `up` starts from db/init again. -f names
# compose.yaml alone, so no override file and no COMPOSE_FILE is merged in.
up: ## Start PostgreSQL with pgvector on 127.0.0.1:55432 and wait until it answers
	docker compose -f compose.yaml up -d --wait

down: ## Stop PostgreSQL and delete its data
	docker compose -f compose.yaml down --volumes

migrate: ## Apply db/migrations as the migration role (after make up)
	uv run python -P -m app.db.migrate

# The embedding model (ADR 0005): exactly FILES at REVISION, from app/config/embedding.py,
# into models/ (git-ignored). `missing` deletes a file with other bytes and lists what to
# fetch, so a second run downloads nothing; `verify` checks every sha256 and deletes a
# mismatch. curl, not Python: app/ holds no HTTP client. -q (first) skips ~/.curlrc; https
# only, redirects included; anonymous: no token, no netrc; nothing over 600 MiB (the
# model is 470 MB). Each file lands in a fresh temporary file beside it, then is renamed;
# a failure or an interrupt removes the temporary file. No cache (plan M1, default 6).
MODEL_CURL := curl -q --fail --silent --show-error --location --proto '=https' --proto-redir '=https' --tlsv1.2 --max-filesize 629145600 --retry 3 --connect-timeout 30 --max-time 900

model: ## Download the embedding model's pinned files into models/ and check each sha256 (needs the network)
	@set -eu; \
	todo="$$(uv run python -P -m app.embeddings.model_files missing)"; \
	printf '%s\n' "$$todo" | while read -r url path; do \
	  test -n "$$url" || continue; \
	  mkdir -p "$$(dirname "$$path")"; \
	  tmp="$$(mktemp "$$path.XXXXXX")"; \
	  trap 'rm -f "$$tmp"' EXIT; \
	  trap 'exit 130' INT TERM; \
	  echo "model: downloading $$url"; \
	  $(MODEL_CURL) -o "$$tmp" "$$url"; \
	  mv -f "$$tmp" "$$path"; \
	done
	uv run python -P -m app.embeddings.model_files verify

lint: ## Check lint and formatting with ruff
	uv run ruff check
	uv run ruff format --check

fmt: ## Format the code with ruff
	uv run ruff format

# Offline means no network beyond loopback: tests/db needs `make up` and `make migrate`
# first, tests/embeddings needs `make model`, and both fail, never skip, without them.
test: lint ## Lint, then run the offline tests (all of tests/ except tests/web; needs make up, make migrate, make model)
	uv run python -P -m pytest --ignore=tests/web

# CI runs one target per job (.github/workflows/ci.yml); tests/ci/ pins both sides.
# Prerequisites run in order: the database is up and migrated, and the model is in
# place, before pytest starts.

lint-test: up migrate model test ## CI's lint-test job: up, migrate, model, then make test (which also scans the branch's commits for secrets)

# Re-resolving the lock catches a hand edit that the hashes do not cover (ADR 0006).
# --refresh: without it, uv answers from its cache and misses a backdated upload-time.
# The audit reads an export of the lock with hashes, so pip-audit installs nothing.
audit: ## Re-resolve the lock, then pip-audit it with hashes (needs the network)
	uv lock --check --refresh
	@set -eu; \
	req="$$(mktemp)"; \
	trap 'rm -f "$$req"' EXIT; \
	uv export --format requirements.txt --locked --no-emit-project --all-groups --quiet -o "$$req"; \
	uv run pip-audit -r "$$req" --require-hashes --disable-pip --progress-spinner off

# No --with-deps: it runs apt as root. The runner image is trusted to carry the libraries.
web-deps: ## Download Chromium for Playwright (from Playwright's CDN, not hash-pinned)
	uv run playwright install chromium

# pytest exits 5 when it collects nothing, and make fails on it.
web-test: web-deps ## The browser smoke test (tests/web) against the app on 127.0.0.1
	uv run python -P -m pytest tests/web

evals: ## The eval gate, offline: check the corpus against evals/gate.toml (no scoring in M0)
	uv run python -P -m app.runners.evals

redteam: ## The red-team gate, offline: check the attacks against redteam/gate.toml (no scoring in M0)
	uv run python -P -m app.runners.redteam

# Standard library only, fixed seed: the same templates give the same bytes. The output
# is git-ignored. `data` is phony, or make would take the data/ directory for it.
data: ## Regenerate the synthetic corpus (fixed seed) into data/synthetic/out/ (git-ignored)
	uv run python -P -m data.synthetic.generate

docker-build: ## Build the image, then smoke-test it: /health on 127.0.0.1, a foreign Host refused, non-root, plaintext and dev login refused
	docker build -t $(IMAGE) .
	@set -eu; \
	uid="$$(docker run --rm $(IMAGE) id -u)"; \
	echo "smoke: image runs as uid $$uid"; \
	test "$$uid" != 0
	@set -eu; \
	port="$$(python3 -I -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')"; \
	cid="$$(docker create -p 127.0.0.1:$$port:8000 -e SITREP_ALLOWED_HOSTS=127.0.0.1:$$port $(IMAGE))"; \
	trap 'docker rm -f "$$cid" >/dev/null' EXIT; \
	docker start "$$cid" >/dev/null; \
	addr="$$(docker port "$$cid" 8000/tcp)"; \
	echo "smoke: published on $$addr"; \
	test "$$addr" = "127.0.0.1:$$port" || { echo "smoke: not on 127.0.0.1:$$port only"; exit 1; }; \
	for i in $$(seq 1 40); do \
	  curl --noproxy '*' -fsS -o /dev/null "http://$$addr/health" 2>/dev/null && break; sleep 0.25; \
	done; \
	code="$$(curl --noproxy '*' -sS -o /dev/null -w '%{http_code}' -H 'Host: 192.0.2.1:8000' "http://$$addr/health")"; \
	echo "smoke: a foreign Host: $$code"; \
	test "$$code" = 400; \
	canary="cnry$$$$$$(date +%s)"; \
	body="$$(curl --noproxy '*' -fsS -H "X-Canary: $$canary" "http://$$addr/health?q=$$canary")"; \
	echo "smoke: /health $$body"; \
	test "$$body" = '{"status":"ok"}'; \
	curl --noproxy '*' -fsS -o /dev/null "http://$$addr/"; \
	echo "smoke: / 200"; \
	sleep 0.5; \
	logs="$$(docker logs "$$cid" 2>&1)"; \
	case "$$logs" in *'"GET /health"'*) ;; *) echo "smoke: no span in the logs"; exit 1;; esac; \
	case "$$logs" in *"$$canary"*) echo "smoke: the canary is in the logs"; exit 1;; esac; \
	echo "smoke: logs carry the span, not the canary"
	@set -eu; \
	cid="$$(docker create -e SITREP_TRACE_PLAINTEXT=1 $(IMAGE))"; \
	trap 'docker rm -f "$$cid" >/dev/null' EXIT; \
	docker start "$$cid" >/dev/null; \
	for i in $$(seq 1 40); do \
	  test "$$(docker inspect -f '{{.State.Status}}' "$$cid")" = exited && break; sleep 0.25; \
	done; \
	state="$$(docker inspect -f '{{.State.Status}} {{.State.ExitCode}}' "$$cid")"; \
	echo "smoke: SITREP_TRACE_PLAINTEXT=1 on 0.0.0.0: $$state"; \
	test "$$state" = "exited 2"; \
	docker logs "$$cid" 2>&1 | grep -q 'refused to start: SITREP_TRACE_PLAINTEXT=1'; \
	echo "smoke: refused with exit code 2"
	@set -eu; \
	if docker run --rm $(IMAGE) python -P -c 'import app.auth.dev_shim' 2>/dev/null; then \
	  echo "smoke: the dev login shim is in the image"; exit 1; \
	fi; \
	echo "smoke: the dev login shim is not in the image"
	@set -eu; \
	for host in 0.0.0.0 127.0.0.1; do \
	  cid="$$(docker create -e SITREP_DEV_AUTH=1 -e SITREP_HOST=$$host $(IMAGE))"; \
	  trap 'docker rm -f "$$cid" >/dev/null 2>&1' EXIT; \
	  docker start "$$cid" >/dev/null; \
	  for i in $$(seq 1 40); do \
	    test "$$(docker inspect -f '{{.State.Status}}' "$$cid")" = exited && break; sleep 0.25; \
	  done; \
	  state="$$(docker inspect -f '{{.State.Status}} {{.State.ExitCode}}' "$$cid")"; \
	  logs="$$(docker logs "$$cid" 2>&1)"; \
	  docker rm -f "$$cid" >/dev/null; \
	  echo "smoke: SITREP_DEV_AUTH=1 on $$host: $$state"; \
	  test "$$state" = "exited 2"; \
	  case "$$logs" in *'refused to start: SITREP_DEV_AUTH=1'*) ;; *) echo "smoke: no refusal"; exit 1;; esac; \
	done; \
	echo "smoke: the dev login refused in the image, with exit code 2"
