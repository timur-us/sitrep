---
name: pr-review
description: Review a pull request with the reviewers the risk level requires, cross-check every finding, and write the report into the PR description. Use before asking Timur for a merge.
---
# PR review

1. Classify the PR (CLAUDE.md rule 4): **docs** (text only) · **code** (no security relevance) · **sensitive** (tools, permissions, prompts, policies, auth, retrieval scoping, CI, infrastructure). State the class and why.
2. Run the reviewers as subagents, in parallel, each with the diff (`git diff main...HEAD`) and the PR description:
   - docs: spec-reviewer
   - code: spec-reviewer, test-reviewer
   - sensitive: spec-reviewer, test-reviewer, security-reviewer
3. Cross-check: one analyzer subagent checks all findings in a single run against the code and marks each one confirmed or rejected with file:line. One run, not one per finding: ForecastPilot spent 840k tokens on two small PRs.
4. Fix confirmed findings in the same PR, or explain why not. Re-run the affected reviewer once.
5. Write the report into the PR description under "## Review": class, reviewers, findings (confirmed / rejected), fixes, what remains open.
6. For a sensitive PR: stop here and ask Timur to get Fable's security review. Only then ask for the merge. Never merge without the explicit yes.
