"""Database, schema and role names, derived from the slug (ADR 0008).

The SQL files under db/ cannot import Python and write these names out; the role and
migration tests in tests/db/ connect with them, so a drift fails there. Python SQL is
never schema-qualified for the app schema: each login role's search_path is set in
db/init/00-roles.sql. The migration runner's own table is the one exception.
"""

from __future__ import annotations

import re

from app.config.branding import SLUG

DATABASE = SLUG
SCHEMA = SLUG
# The runner's bookkeeping, outside the app schema; the app role has no grant on it.
META_SCHEMA = f"{SLUG}_meta"
# Each login role's search_path in the app database, as db/init/00-roles.sql sets it.
SEARCH_PATH = f"{SCHEMA}, public"

# NOLOGIN; owns the database, the schemas and everything in them.
OWNER_ROLE = f"{SLUG}_owner"
# LOGIN; holds OWNER_ROLE WITH INHERIT FALSE, so it acts as the owner only after SET ROLE.
MIGRATOR_ROLE = f"{SLUG}_migrator"
# LOGIN; no attributes, no memberships, owns nothing.
APP_ROLE = f"{SLUG}_app"

# The names go into SQL text unquoted (SET ROLE, the runner's bootstrap), so each one is
# a plain lower-case identifier.
_IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]{0,62}")
for _name in (DATABASE, SCHEMA, META_SCHEMA, OWNER_ROLE, MIGRATOR_ROLE, APP_ROLE):
    if not _IDENTIFIER.fullmatch(_name):
        raise ValueError(f"not a plain SQL identifier: {_name!r}")
