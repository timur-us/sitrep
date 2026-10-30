# Every public target carries a "##" comment: `make help` lists them, and
# tests/test_runbook.py keeps them in step with docs/runbooks/local-setup.md.

.DEFAULT_GOAL := help
SHELL := /bin/sh

# uv fails instead of rewriting uv.lock when pyproject.toml and the lock disagree.
export UV_LOCKED := 1

# The local image tag. The smoke test publishes on 127.0.0.1 only, on a free port.
IMAGE := sitrep:local

.PHONY: help lint fmt test lint-test audit docker-build web-deps web-test evals redteam

help: ## List the targets
	@grep -E '^[a-z][a-z0-9-]*:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "  %-13s %s\n", $$1, $$2}'

lint: ## Check lint and formatting with ruff
	uv run ruff check
	uv run ruff format --check

fmt: ## Format the code with ruff
	uv run ruff format

test: lint ## Lint, then run the offline tests (all of tests/ except tests/web)
	uv run python -P -m pytest --ignore=tests/web

# CI runs one target per job (.github/workflows/ci.yml); tests/ci/ pins both sides.

lint-test: test ## CI's lint-test job: make test, which also scans the branch's commits for secrets

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

docker-build: ## Build the image, then smoke-test it: /health on 127.0.0.1, non-root, plaintext refused
	docker build -t $(IMAGE) .
	@set -eu; \
	uid="$$(docker run --rm $(IMAGE) id -u)"; \
	echo "smoke: image runs as uid $$uid"; \
	test "$$uid" != 0
	@set -eu; \
	cid="$$(docker run -d -p 127.0.0.1::8000 $(IMAGE))"; \
	trap 'docker rm -f "$$cid" >/dev/null' EXIT; \
	addr="$$(docker port "$$cid" 8000/tcp)"; \
	echo "smoke: published on $$addr"; \
	case "$$addr" in 127.0.0.1:*) ;; *) echo "smoke: not on 127.0.0.1 only"; exit 1;; esac; \
	for i in $$(seq 1 40); do \
	  curl --noproxy '*' -fsS -o /dev/null "http://$$addr/health" 2>/dev/null && break; sleep 0.25; \
	done; \
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
	cid="$$(docker run -d -e TRACE_PLAINTEXT=1 $(IMAGE))"; \
	trap 'docker rm -f "$$cid" >/dev/null' EXIT; \
	for i in $$(seq 1 40); do \
	  test "$$(docker inspect -f '{{.State.Status}}' "$$cid")" = exited && break; sleep 0.25; \
	done; \
	state="$$(docker inspect -f '{{.State.Status}} {{.State.ExitCode}}' "$$cid")"; \
	echo "smoke: TRACE_PLAINTEXT=1 on 0.0.0.0: $$state"; \
	test "$$state" = "exited 2"; \
	docker logs "$$cid" 2>&1 | grep -q 'refused to start: TRACE_PLAINTEXT=1'; \
	echo "smoke: refused with exit code 2"
