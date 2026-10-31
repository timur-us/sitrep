"""The advisor a request acts for, as the verified login established it (CLAUDE.md rule 8).

Only app/auth/ constructs a VerifiedAdvisor (tests/test_identity.py scans app/ for it):
the dev shim locally (PR 7), Entra ID in Azure (M3). Nothing a model, a tool, a header
or a query parameter says can become one. app/db/session.py takes nothing else.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class VerifiedAdvisor:
    advisor_id: str
