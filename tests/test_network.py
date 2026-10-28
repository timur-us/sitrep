"""Tests run offline: pytest-socket blocks connect() except to loopback.

The flags live in pyproject.toml (`--disable-socket --allow-hosts=127.0.0.1,::1`).
The claim is limited to connect() on in-process Python sockets. Not covered:
UDP sendto(), DNS lookups through libc, subprocesses and native libraries.
"""

from __future__ import annotations

import socket

import pytest_socket

TEST_NET_1 = "192.0.2.1"  # RFC 5737: documentation only, never routed


def test_network_is_blocked() -> None:
    error: Exception | None = None
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1)
        try:
            sock.connect((TEST_NET_1, 9))
        except (OSError, RuntimeError) as exc:  # the block is a RuntimeError
            error = exc
    assert isinstance(error, pytest_socket.SocketConnectBlockedError), (
        f"connect to {TEST_NET_1} was not blocked by pytest-socket: {error!r}"
    )

    # Positive control: loopback stays open, so the block above is not just a
    # broken socket module.
    with socket.create_server(("127.0.0.1", 0)) as server:
        port = server.getsockname()[1]
        with socket.create_connection(("127.0.0.1", port), timeout=2) as client:
            assert client.getpeername() == ("127.0.0.1", port)
