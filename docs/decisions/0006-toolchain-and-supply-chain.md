# ADR 0006: Toolchain and supply chain

## Status

Accepted (Timur, 2026-10-28, in the M0 plan, questions 2, 3 and 7).

## Date

2026-10-29

## Context

SITREP (situation report) handles client data and an API key, so every dependency, action and image is code that runs with that access (threat-model, "Supply chain"). A fresh malicious release is the common case: most are caught and pulled within days. CI must be reproducible and must not install anything unpinned.

## Options

- **pip-tools** with hashed requirements files. Proven, but a second tool for the venv and for Python itself.
- **uv** with `uv.lock`. One tool for Python, the venv and the lock, with hashes for every file and a minimum release age built in.
- **pypa/gh-action-pip-audit** for the audit. v1.1.0 (2024-08-08) is still the latest, and it installs `pip-audit ~= 2.0, >= 2.5.6` unhashed on every run.
- **Our own audit step** from the lock.

## Decision

- **uv** (question 2): `uv.lock` with sha256 on every distribution; `required-version = ">=0.13.5,<0.14"`; the Makefile exports `UV_LOCKED=1`, so uv fails instead of rewriting the lock.
- **Minimum release age, 7 days** (question 3): `exclude-newer = "7 days"`. uv records it in the lock as `exclude-newer-span = "P7D"`, with a placeholder timestamp labelled as having no effect, so the lock does not go stale. Verified in PR 1 with uv 0.13.6 (which wrote the lock) and 0.13.5 (`uv lock --check`). `tests/test_lock.py` checks the span, PyPI-only sources, a sha256 and an upload time on every file, and that no upload lies inside the window.
- **The build toolchain is pinned by exact version, not by release age.** uv builds the editable package itself, so `uv_build` is never resolved through the lock, and its constraint `>=0.13,<0.14` admits a release newer than 7 days. The control is uv pinned to exactly 0.13.5 in CI (setup-uv `version:`) and in the Dockerfile's uv image tag, kept equal by a test (PRs 3 and 4).
- **Audit:** `uv export --format requirements.txt --locked --no-emit-project --all-groups`, then `pip-audit -r … --require-hashes --disable-pip`, with pip-audit 2.10.1 itself in the lock.
- **Tests offline:** pytest-socket 0.8.1 with `--disable-socket --allow-hosts=127.0.0.1,::1`; plugins load by name only (`--disable-plugin-autoload -p pytest_socket`).
- **Browser test:** Playwright for Python; `playwright install chromium` without `--with-deps`, which would run apt as root.
- **Pins:** actions by full commit SHA (and the repository setting that requires it), images by `@sha256:` digest.
- **Dependabot:** `github-actions`, `uv`, `docker`; weekly, grouped, `cooldown: default-days: 7` (question 7), above Dependabot's default of 3 days. Security updates are not delayed.

## Consequences

- **Easier:** one lock to read, a hash for every file, and a week for the ecosystem to catch a bad release before we install it.
- **Harder:**
  - an urgent fix newer than 7 days needs a deliberate, reviewed exception;
  - the exact uv pin is bumped by hand, in two places;
  - the Chromium download from Playwright's CDN is the one fetch in CI without a hash pin.
- **Watch:** Dependabot's `uv` support and cooldown on uv 0.13 locks, once PR 4 runs it.

## Sources

- uv settings reference (`exclude-newer`, `required-version`), https://docs.astral.sh/uv/reference/settings/ (accessed 2026-10-29).
- uv 0.13.5 release, https://github.com/astral-sh/uv/releases/tag/0.13.5 (published 2026-10-22).
- astral-sh/setup-uv v10.3.2, https://github.com/astral-sh/setup-uv/releases/tag/v10.3.2 (published 2026-10-22).
- pip-audit v2.10.1 README, https://github.com/pypa/pip-audit/blob/v2.10.1/README.md (release 2026-06-10).
- pypa/gh-action-pip-audit v1.1.0, https://github.com/pypa/gh-action-pip-audit/releases/tag/v1.1.0 (2024-08-08).
- pytest-socket README, https://github.com/miketheman/pytest-socket (0.8.1, 2026-08-19).
- Playwright for Python, "Browsers", https://playwright.dev/python/docs/browsers (accessed 2026-10-29).
- GitHub, Dependabot options reference (`cooldown`), https://docs.github.com/en/code-security/dependabot/working-with-dependabot/dependabot-options-reference (accessed 2026-10-29).
- GitHub, "Managing GitHub Actions settings for a repository" (SHA pinning), https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/enabling-features-for-your-repository/managing-github-actions-settings-for-a-repository (accessed 2026-10-29).
- PR #3 description, "Tooling facts checked while building" (2026-10-29).
