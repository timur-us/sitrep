# ADR 0009: Dev auth shim on loopback, and prefixed settings

## Status

Accepted (Timur, 2026-10-28, plan question 13: a cookie with server-side sessions; the `SITREP_` prefix on 2026-10-29, PR 3 review sec-7). Built in M0 PR 7. Database URLs prefixed in M1 PR 3, the Host check made global in M1 PR 5 (Timur, 2026-11-08, answers 1 and 4 to the PR 7 questions).

## Date

2026-11-04

## Context

SITREP (situation report) needs a verified login before M3's Entra ID, to test login to `set_config` to row-level security now (rule 8). A local server is not private: a browser page from any origin can send it requests, and a name that resolves to 127.0.0.1 can read its answers (DNS rebinding). `scope["server"]` cannot prove the bind, and the lifespan runs before binding. The PR 3 review found that the settings were matched without regard to case and without a prefix, and tcsh exports `HOST`.

## Options

- **An identity header** (`X-Dev-Advisor`): any page or process names any advisor; it trains the pattern threat-model path 3 forbids.
- **A stateless signed cookie** (Starlette's `SessionMiddleware`): adds `itsdangerous`, and logout cannot revoke a copied cookie.
- **Starlette's `TrustedHostMiddleware`** for the Host check: its `www_redirect` redirects, and the app has no redirects.
- **Our own shim:** a random session id in process memory, an HMAC-signed cookie, our own Host and Origin checks, about 200 lines to review.

## Decision

The last option, `app/auth/dev_shim.py`:
- **Where it runs.** `python -P -m app` takes no arguments in any mode (`--uds`, `--fd`: exit 2). With `SITREP_DEV_AUTH=1` (exactly `1`) it also exits 2 before binding for any host but exactly `127.0.0.1`, port 0, and a missing `app.auth.dev_shim`. It always binds through `uvicorn.Config.bind_socket()` and hands that socket to the server; with the dev login it checks every listening socket again after startup. `create_app()` mounts the shim only for `bind_host == "127.0.0.1"` and exactly `1`. The image lacks the file (`.dockerignore`); `make docker-build` proves it.
- **Browser defences.** The TCP peer must be `127.0.0.1` (403), whoever serves the app. Host must be exactly `127.0.0.1:<port>`, else 400: since M1 PR 5 on every route, shim or not (`app/http/host_check.py`), unless `SITREP_ALLOWED_HOSTS` names others; the shim, loopback only. Every method but GET needs `Origin: http://127.0.0.1:<port>` exactly, and `Sec-Fetch-Site: same-origin` when sent, otherwise 403. The login page sets its referrer policy to `same-origin`: under the app's `no-referrer`, a browser sends `Origin: null` even on a same-origin POST.
- **Sessions.** `POST /dev/login` with the form Content-Type and exactly `advisor=<id>`, advisors only. The cookie is `<id>.<HMAC-SHA256>` under a key drawn per app instance: HttpOnly, SameSite=Strict, Path=/. Logout and a new login end the session. Two session cookies count as none.
- **Identity.** The middleware attaches an issued `VerifiedAdvisor` to the ASGI scope; `current_advisor()` reads that and nothing else. Without the shim, routes that need it answer 401.
- **Settings.** `SITREP_HOST`, `SITREP_PORT`, `SITREP_DEV_AUTH`, `SITREP_TRACE_PLAINTEXT`, since M1 PR 3 `SITREP_DATABASE_URL` and `SITREP_MIGRATION_DATABASE_URL`, since PR 5 `SITREP_ALLOWED_HOSTS`. Unprefixed names are ignored, never refused.

## Consequences

- **Easier:** one path from login to RLS is tested end to end, including in Chromium. M3 replaces the shim's middleware and keeps `current_advisor()`.
- **Harder:** a restart signs everyone out; the shim works natively only; every variable name changed.
- **Watch:**
  - Any local process is trusted, as with the database: it can log in as any advisor and forge any Origin. Cookies ignore ports, so a server on another port of 127.0.0.1 receives the session cookie and can toss a second one.
  - Sessions never expire, and the store is unbounded (kept in M0). Two session cookies end neither session.

## Sources

- Timur's answers to the PR 7 questions (answer 1, the database URLs, built in M1 PR 3; answers 4 and 5 in M1 PR 5, `docs/plans/m1-mvp-core.md`), `docs/plans/m0-foundation.md`, amendment recorded in PR 9 (2026-11-08).
- `docs/plans/m0-foundation.md`, "Dev shim" and PR 7 (2026-10-28).
- PR 3 review findings sec-1 (Host allowlist) and sec-7 (prefix), https://github.com/timur-us/sitrep/pull/5 (2026-10-29).
- Fetch Standard, "Serializing a request origin" (no-referrer gives `null` for non-GET requests), https://fetch.spec.whatwg.org/ (read 2026-11-04; measured in `tests/web/test_dev_login.py`).
- RFC 6265, section 8.5, cookies do not provide isolation by port, https://www.rfc-editor.org/rfc/rfc6265 (published April 2011; read 2026-11-04).
- uvicorn 0.54.0, `Config.bind_socket()` and `Server.startup(sockets=…)`, read in the installed source (2026-11-04).
- Starlette 1.9.0, `TrustedHostMiddleware` and its `www_redirect` (read 2026-10-29).
