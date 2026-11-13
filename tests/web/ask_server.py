"""The app for the read path in Chromium (M1 plan, PR 12), with a scripted model.

`python -m app` takes no model client, and must not: a test seam there would be one in
production. So tests/web/test_ask.py starts this script instead, in a subprocess from an
empty directory with the environment tests/web/conftest.py builds. It serves the same
create_app() with the dev login on 127.0.0.1, uvicorn configured as app/__main__.py's
server_config() does (no access log, no proxy headers, no server header, the redacting
log config, h11, no websockets), the real embedder and the real database. Only the model
is scripted: tests/scripted_model.py's EchoModel, which searches for the demo family (20
hits, the most a search returns) and cites all of what the advisor's own search
returned, so the demo family's e-mails are among the sources. No key, no socket but
loopback.

It refuses to start (exit 2) unless the host is exactly 127.0.0.1 and the dev login is on.
"""

from __future__ import annotations

import sys

import uvicorn
from scripted_model import EchoModel

from app.config import branding
from app.config.settings import LOOPBACK, Settings
from app.main import create_app
from app.observability.logs import LOG_CONFIG

REFUSED = 2


def main() -> int:
    settings = Settings(_env_file=None)
    if settings.host != LOOPBACK or not settings.dev_auth_on or settings.port == 0:
        print("ask_server: needs the dev login on 127.0.0.1 and a port", file=sys.stderr)
        return REFUSED
    model = EchoModel(branding.DEMO_MANDATE.family, cite=20, arguments={"limit": 20})
    app = create_app(settings, bind_host=LOOPBACK, model_client=model)
    uvicorn.run(
        app,
        host=LOOPBACK,
        port=settings.port,
        access_log=False,
        proxy_headers=False,
        server_header=False,
        log_config=LOG_CONFIG,
        http="h11",
        ws="none",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
