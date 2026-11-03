"""GET /api/mandates: the signed-in advisor's own mandates (plan, PR 7).

A minimal list, beyond the brief's M0 row, that carries the path from the verified login
to row-level security: current_advisor() takes the advisor from the login, and
advisor_transaction() (app/db/session.py) makes it the advisor of one transaction, in
which the policies of db/migrations/0002_tables_rls.sql show that advisor's rows only.
The statement names no advisor; nothing from the request reaches it. No retrieval.

mount() adds the route to the app itself, not through an included router, so the route
list (and the no-redirect scan in tests/test_app.py) sees it.

401 without a login, 503 when the database does not answer (the pool times out or the
connection fails); neither response says more.
"""

from __future__ import annotations

from typing import Annotated

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Request

from app.auth.identity import VerifiedAdvisor, current_advisor
from app.db import queries
from app.db.session import advisor_transaction


def mount(app: FastAPI) -> None:
    app.add_api_route("/api/mandates", list_mandates, methods=["GET"])


# A sync route: FastAPI runs it in its thread pool, so the blocking pool and driver
# never hold the event loop.
def list_mandates(
    request: Request, advisor: Annotated[VerifiedAdvisor, Depends(current_advisor)]
) -> list[dict[str, str]]:
    try:
        with advisor_transaction(request.app.state.pool, advisor) as conn:
            rows = conn.execute(queries.LIST_MANDATES).fetchall()
    except psycopg.OperationalError:
        # PoolTimeout and PoolClosed are OperationalErrors too.
        raise HTTPException(status_code=503, detail="database unavailable") from None
    return [{"id": mandate_id, "family": family} for mandate_id, family in rows]
