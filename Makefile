# Every public target carries a "##" comment: `make help` lists them, and
# tests/test_runbook.py keeps them in step with docs/runbooks/local-setup.md.

.DEFAULT_GOAL := help
SHELL := /bin/sh

# uv fails instead of rewriting uv.lock when pyproject.toml and the lock disagree.
export UV_LOCKED := 1

# The local image tag. The smoke test publishes on 127.0.0.1 only, on a free port.
IMAGE := sitrep:local

.PHONY: help lint fmt test docker-build

help: ## List the targets
	@grep -E '^[a-z][a-z0-9-]*:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "  %-13s %s\n", $$1, $$2}'

lint: ## Check lint and formatting with ruff
	uv run ruff check
	uv run ruff format --check

fmt: ## Format the code with ruff
	uv run ruff format

test: lint ## Lint, then run the offline tests (all of tests/ except tests/web)
	uv run python -P -m pytest --ignore=tests/web

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
	  curl -fsS -o /dev/null "http://$$addr/health" 2>/dev/null && break; sleep 0.25; \
	done; \
	body="$$(curl -fsS "http://$$addr/health")"; \
	echo "smoke: /health $$body"; \
	test "$$body" = '{"status":"ok"}'; \
	curl -fsS -o /dev/null "http://$$addr/"; \
	echo "smoke: / 200"
	@set -eu; \
	cid="$$(docker run -d -e TRACE_PLAINTEXT=1 $(IMAGE))"; \
	trap 'docker rm -f "$$cid" >/dev/null' EXIT; \
	for i in $$(seq 1 40); do \
	  test "$$(docker inspect -f '{{.State.Status}}' "$$cid")" = exited && break; sleep 0.25; \
	done; \
	state="$$(docker inspect -f '{{.State.Status}} {{.State.ExitCode}}' "$$cid")"; \
	echo "smoke: TRACE_PLAINTEXT=1 on 0.0.0.0: $$state"; \
	case "$$state" in "exited 0") exit 1;; exited*) ;; *) exit 1;; esac
