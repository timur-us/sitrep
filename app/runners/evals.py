"""`make evals`: the eval gate, offline (scaffold, PR 4 commit 1)."""

from __future__ import annotations

import sys
from collections.abc import Sequence

from app.runners.corpus import Suite
from app.runners.corpus import main as run

SUITE = Suite(name="evals", case_dir="cases")


def main(argv: Sequence[str] | None = None) -> int:
    return run(SUITE, argv)


if __name__ == "__main__":
    sys.exit(main())
