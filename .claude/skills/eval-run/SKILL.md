---
name: eval-run
description: Run the eval suite offline against recorded runs, compare with the baseline of the current prompt version, and write the report. Use after every change to prompts, tools, retrieval or the harness.
---
# Eval run

1. Offline by default: `make evals` scores the recorded runs in `evals/recorded/<version>/`. No API key is needed and none is used.
2. A live run (new recording) only when the prompt version, the tools or the retrieval changed: estimate the cost first (cases × samples × average tokens), show it, and wait for `--live --yes` from Timur.
3. Compare with the baseline: correctness metrics (grounding, citations, number guard, refusal correctness, tool choice) may not drop; style metrics may change with a reason.
4. Write `evals/results/<version>.md`: table per metric, failures with case id and the reason, "spec gap vs model error" for each failure.
5. Scorer changes only with evidence that the scorer was wrong, never to make a run pass.
6. Pre-registration: say before a live run what would count as better and what as worse.
