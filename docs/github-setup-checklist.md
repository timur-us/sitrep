# GitHub setup checklist (done by Timur, by hand)

Date: 2026-10-27. These are layer-3 controls: they hold for every token, including the agent's.

## Repository

- [ ] Create `sitrep` (public), default branch `main`, add this seed, first commit "chore: guardrails and docs seed".
- [ ] Settings → General → Pull Requests: enable "Automatically delete head branches".
- [ ] Settings → Actions → General: "Require actions to be pinned to a full-length commit SHA" — check later whether a used action pulls another one by tag (ForecastPilot note: `upload-pages-artifact` did).
- [ ] Settings → Actions → General → Workflow permissions: "Read repository contents and packages permissions"; "Allow GitHub Actions to create and approve pull requests" off.
- [ ] Settings → Actions → General → "Approval for running fork pull request workflows from contributors": **Require approval for all external contributors**.
- [ ] Settings → Advanced Security: secret scanning and **push protection** on. GitHub then refuses a push that contains an Anthropic API key, whoever pushes it.

## Ruleset for `main` (Settings → Rules → Rulesets → New branch ruleset)

Day 1, before the first agent session:

- [ ] Name `main-protected`, enforcement **Active**, target branch: default branch.
- [ ] Rules: **Restrict deletions**, **Block force pushes**, **Require a pull request before merging** (0 required approvals). The checks are not the whole gate: a `pull_request` run uses the PR's own `ci.yml` and the PR's own tests, so a PR can weaken a gate and still pass it. The merge gate is Timur's explicit yes (CLAUDE.md rule 3), with the `gh pr merge` ask in `.claude/settings.json`; the review rule for gate paths is what reads such a change.
- [ ] **No** "Require status checks to pass" yet: a required check that no workflow reports blocks every merge, and `ci.yml` only exists after the CI pull request.
- [ ] Bypass list: **empty**. Claude Code works with your admin token; a bypass for you is a bypass for the agent. Emergency path: set the ruleset to Disabled, merge, set it back to Active (the agent cannot do this: `gh api *rulesets*` is denied).

After the CI pull request is merged:

- [ ] Add **Require status checks to pass** with "Require branches to be up to date before merging" (strict) and the six checks `lint-test`, `pip-audit`, `docker-build`, `web-test`, `evals`, `redteam`, source GitHub Actions. They are the job ids in `.github/workflows/ci.yml` (no job sets a `name:`; `tests/ci/test_workflow.py` keeps it so).
- [ ] After a green run on `main`: check that the six checks appear under those names in the ruleset's check picker.

## Environment `production` (Settings → Environments → New environment)

- [ ] Name `production`.
- [ ] **Required reviewers:** yourself. Every deploy run waits for your approval.
- [ ] **Deployment branches:** "Selected branches and tags" → only `main`.
- [ ] No secrets yet. From M3: environment variables `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID` (ids, not secrets), set by you. The federated credential in Entra is created by you with subject `repo:timur-us/sitrep:environment:production` — never for `pull_request`.
- [ ] Pull requests get no Azure login at all; a PR that changes `infra/` carries a what-if run by hand in its description, as in ForecastPilot.

## Dependabot

- [ ] Settings → Code security: enable Dependabot alerts and security updates; version updates come from `.github/dependabot.yml` (github-actions, uv, docker; weekly; grouped; 7-day cooldown). A bump of the uv image or of setup-uv alone fails CI on purpose: the uv pin is bumped by hand, in both places.

## Anthropic key

- [ ] M0 needs no key: there is no `.env` with a real key in M0.
- [ ] From M1: a separate Console workspace `sitrep` with a monthly spend limit; the key comes only from that workspace. Live runs (`--live`) are started by you in your own terminal. The details go into an ADR in M1 (ADR number: next free after 0009).

## Local

- [ ] `.claude/settings.local.json` (git-ignored) sets `AZURE_CONFIG_DIR` to an empty folder, so the agent has no Azure login in M0 to M2. How the agent reaches Azure in M3 is decided in an ADR (ADR number: next free after 0009).
- [ ] `.claude/settings.json` asks before an edit to `Makefile`, `pyproject.toml`, `evals/gate.toml` and `redteam/gate.toml`, next to `CLAUDE.md`, `.claude/agents/**`, `.claude/skills/**` and `.github/workflows/**`. Other files that shape CI are not asked for: `.github/dependabot.yml`, `tests/ci/**`, `app/runners/**`, the scanners in `tests/`. Writes made through Bash are not covered either. The review rule for gate paths catches both.
- [ ] Before starting Claude Code: `ANTHROPIC_API_KEY` is not set in your shell, otherwise Claude Code offers to bill the session to that key.
- [ ] Never choose "Yes, and don't ask again" on a prompt; allow once.
- [ ] On the first start, accept the trust dialog for this folder; project hooks run only in a trusted folder. Check with `/hooks` that both hooks are listed.
