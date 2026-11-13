"""`make evals`: the eval gate, offline, scoring recorded runs (M1 PR 16, ADR 0014).

See app/runners/corpus.py for the checks and the gate, app/runners/scoring.py for the
scorers, and app/runners/live.py for `--live --yes`, by hand only.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence

from app.runners.corpus import Suite
from app.runners.corpus import main as run

SUITE = Suite(name="evals", case_dir="cases", scored=True)


def main(argv: Sequence[str] | None = None) -> int:
    return run(SUITE, argv)


if __name__ == "__main__":
    sys.exit(main())
