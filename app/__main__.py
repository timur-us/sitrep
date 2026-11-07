"""Entry point: `python -P -m app`. The only place that reads .env.

uvicorn runs without an access log (it would log paths and query strings), without
trusting proxy headers, without a server header, and with the redacting log config.

Settings are named here by their fields (host, port, dev_auth, trace_plaintext); each is
read from a variable with the product prefix (env_name(), ADR 0009). The entry point
refuses to start, with exit code 2 and before it binds anything:
- any command-line argument, --uds and --fd included, with or without the dev login:
  the bind is the host and port settings, on TCP only;
- trace_plaintext at "1" anywhere but 127.0.0.1 (ADR 0007);
- dev_auth at "1" (the dev login, ADR 0009) unless the host is exactly 127.0.0.1 and
  the port is not 0, and unless app.auth.dev_shim can be imported (the image leaves it
  out).

It always binds through uvicorn.Config.bind_socket() and hands the socket to the
server, with the dev login off too, so uvicorn never binds on its own. With
the dev login on, verify_listeners() checks the bound socket before serving, and every
socket the started server listens on after startup: each must be exactly 127.0.0.1 on
the port, or the server closes them, stops, and the entry point exits 2. The app learns
the host it was validated for through create_app(bind_host=...).
"""

from __future__ import annotations

import sys
from collections.abc import Sequence

import uvicorn
from opentelemetry.sdk.trace import TracerProvider

from app.config.settings import LOOPBACK, Settings, env_name
from app.main import create_app
from app.observability.logs import LOG_CONFIG
from app.observability.tracing import PlaintextRefused

# Exit code for a configuration the entry point refuses to start with.
REFUSED = 2
DEV_AUTH, HOST, PORT = env_name("dev_auth"), env_name("host"), env_name("port")


class StartRefused(ValueError):
    """A configuration the entry point refuses to serve. The message names no value."""


def server_config(
    settings: Settings, tracer_provider: TracerProvider | None = None
) -> uvicorn.Config:
    app = create_app(settings, tracer_provider=tracer_provider, bind_host=settings.host)
    return uvicorn.Config(
        app,
        host=settings.host,
        port=settings.port,
        access_log=False,
        proxy_headers=False,
        server_header=False,
        log_config=LOG_CONFIG,
        http="h11",
        ws="none",
    )


def check_start(settings: Settings, argv: Sequence[str]) -> None:
    """Raise StartRefused for a start the entry point does not serve."""
    if argv:
        raise StartRefused(
            f"python -m app takes no arguments (no --uds, no --fd); the bind is {HOST} and {PORT}"
        )
    if not settings.dev_auth_on:
        return
    if settings.host != LOOPBACK:
        raise StartRefused(f"{DEV_AUTH}=1 needs {HOST} to be exactly {LOOPBACK}")
    if settings.port == 0:
        raise StartRefused(f"{DEV_AUTH}=1 needs an explicit {PORT}, not 0")
    try:
        import app.auth.dev_shim  # noqa: F401 - only whether it can be imported
    except ImportError:
        raise StartRefused(
            f"{DEV_AUTH}=1 needs app.auth.dev_shim, which this install does not carry"
        ) from None


def listening_sockets(server: uvicorn.Server) -> list:
    """The sockets a started server listens on."""
    return [sock for listener in server.servers for sock in (listener.sockets or ())]


def verify_listeners(sockets: Sequence, port: int) -> None:
    """Raise StartRefused unless there are listeners and each is exactly 127.0.0.1:port.

    An IPv4 socket names itself as (host, port); IPv6 as a 4-tuple and a Unix socket as
    a path, so neither can equal the pair.
    """
    if not sockets:
        raise StartRefused("no listening socket to verify")
    for sock in sockets:
        if sock.getsockname() != (LOOPBACK, port):
            raise StartRefused(f"a listening socket is not exactly {LOOPBACK} on {PORT}")


class VerifiedServer(uvicorn.Server):
    """A uvicorn server that checks its listeners once it has started them."""

    def __init__(self, config: uvicorn.Config, port: int) -> None:
        super().__init__(config)
        self.port = port
        self.refused: StartRefused | None = None

    async def startup(self, sockets: list | None = None) -> None:
        await super().startup(sockets=sockets)
        try:
            verify_listeners(listening_sockets(self), self.port)
        except StartRefused as refused:
            self.refused = refused
            for listener in self.servers:
                listener.close()
            self.should_exit = True


def main(argv: Sequence[str] = ()) -> int:
    settings = Settings(_env_file=".env")
    try:
        check_start(settings, argv)
        config = server_config(settings)
    except (StartRefused, PlaintextRefused) as refused:
        print(f"refused to start: {refused}", file=sys.stderr)
        return REFUSED
    sock = config.bind_socket()
    try:
        if not settings.dev_auth_on:
            uvicorn.Server(config).run(sockets=[sock])
            return 0
        verify_listeners([sock], settings.port)
        server = VerifiedServer(config, settings.port)
        server.run(sockets=[sock])
        if server.refused is not None:
            raise server.refused
    except StartRefused as refused:
        print(f"refused to start: {refused}", file=sys.stderr)
        return REFUSED
    finally:
        sock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
