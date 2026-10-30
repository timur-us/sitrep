"""`make redteam`: the red-team gate, offline. See app/runners/corpus.py for what M0 checks."""

from __future__ import annotations

import sys
from collections.abc import Sequence

from app.runners.corpus import Suite
from app.runners.corpus import main as run

SUITE = Suite(name="redteam", case_dir="attacks")


def main(argv: Sequence[str] | None = None) -> int:
    return run(SUITE, argv)


if __name__ == "__main__":
    sys.exit(main())
