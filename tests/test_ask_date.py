"""The route's date for the prompt (prompt v2, M1 PR 16): the server's, read per request.

The loop reads no clock; app/api/ask.py reads the date for each ask and passes it on, and
tests/db/test_recorder.py shows it reaching the system prompt through the route. Here,
without a database: the default is the server's local date, and a given clock replaces it.
"""

from __future__ import annotations

import datetime

from app.config.settings import Settings
from app.main import create_app


def test_the_route_reads_the_servers_date_by_default() -> None:
    app = create_app(Settings(_env_file=None))
    assert app.state.ask.today == datetime.date.today  # a classmethod: equal, not identical
    assert app.state.ask.today() == datetime.date.today()


def test_a_given_date_replaces_the_servers() -> None:
    def fixed() -> datetime.date:
        return datetime.date(2026, 11, 3)

    app = create_app(Settings(_env_file=None), today=fixed)
    assert app.state.ask.today is fixed
