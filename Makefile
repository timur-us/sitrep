# Every public target carries a "##" comment: `make help` lists them, and
# tests/test_runbook.py keeps them in step with docs/runbooks/local-setup.md.

.DEFAULT_GOAL := help
SHELL := /bin/sh

# uv fails instead of rewriting uv.lock when pyproject.toml and the lock disagree.
export UV_LOCKED := 1

.PHONY: help lint fmt test

help: ## List the targets
	@grep -E '^[a-z][a-z0-9-]*:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "  %-8s %s\n", $$1, $$2}'

lint: ## Check lint and formatting with ruff
	uv run ruff check
	uv run ruff format --check

fmt: ## Format the code with ruff
	uv run ruff format

test: lint ## Lint, then run the offline tests (all of tests/ except tests/web)
	uv run python -P -m pytest --ignore=tests/web
