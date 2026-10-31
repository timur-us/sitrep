"""The app starts and answers /health with the database down (plan, PR 5b).

The pool is lazy: opening it in the lifespan connects nobody, and a connection is made
when a request first needs one. So a database that is down, or slow to start, never
keeps the app from serving the routes that need none. A real uvicorn server from
server_config() runs in a thread, with DATABASE_URL on a loopback port where nothing
listens.
"""

from __future__ import annotations

import http.client
import socket
import threading
import time

import uvicorn

from app.__main__ import server_config
from app.config.settings import LOOPBACK, Settings
from app.db import names

BUDGET = 2.0


def closed_port() -> int:
    """A loopback port that was free a moment ago and has no listener now."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((LOOPBACK, 0))
        return probe.getsockname()[1]


def test_app_starts_without_db(settings: Settings, restore_logging: None) -> None:
    url = f"postgresql://{names.APP_ROLE}@{LOOPBACK}:{closed_port()}/{names.DATABASE}"
    local = settings.model_copy(update={"port": 0, "database_url": url})
    config = server_config(local)
    pool = config.app.state.pool
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    started = time.monotonic()
    thread.start()
    try:
        while not server.started and time.monotonic() - started < BUDGET:
            time.sleep(0.02)
        assert server.started, f"the app did not start within {BUDGET} s with the database down"
        port = server.servers[0].sockets[0].getsockname()[1]
        connection = http.client.HTTPConnection(LOOPBACK, port, timeout=BUDGET)
        try:
            connection.request("GET", "/health")
            response = connection.getresponse()
            body = response.read()
        finally:
            connection.close()
        elapsed = time.monotonic() - started
        assert (response.status, body) == (200, b'{"status":"ok"}'), (response.status, body)
        assert elapsed < BUDGET, f"/health answered after {elapsed:.2f} s"
        # Review finding test-9: lazy, not merely in the background. The pool is open and
        # has not tried to connect anybody.
        stats = pool.get_stats()
        assert stats["pool_min"] == 0 and stats["pool_size"] == 0, stats
        assert stats.get("connections_num", 0) == 0, stats
        assert not pool.closed
    finally:
        server.should_exit = True
        thread.join(5.0)
    assert not thread.is_alive(), "the server did not stop"
    assert pool.closed, "the lifespan did not close the pool"
