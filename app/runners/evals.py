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
    # As python -m app does: the redacting log config, then the hooks, so an exception
    # logs its type and not its text (ADR 0007; review finding F5 of M1 PR 16).
    import logging.config

    from app.observability.logs import LOG_CONFIG, install_exception_hooks

    logging.config.dictConfig(LOG_CONFIG)
    install_exception_hooks()
    sys.exit(main())
