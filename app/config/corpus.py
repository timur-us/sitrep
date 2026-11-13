"""The reviewed synthetic corpus, by the sha256 of its manifest.json.

The loader refuses any other corpus (app/db/load.py), and a recorded eval run names the
corpus it ran on with this hash, which the offline gate compares (ADR 0014). It lives
here, not in the loader, because nothing in app/ but the loader's own entry point may
import app.db.load (tests/test_loader_scope.py). tests/synthetic/test_synthetic_corpus.py
keeps it equal to what the generator writes: a change to the corpus changes both.
"""

from __future__ import annotations

MANIFEST_SHA256 = "7053bbbc4bd2b52f4c1ddacd4647726dc9a556422661d8bb1bbadd269b29bf5d"
