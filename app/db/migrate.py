"""Apply db/migrations: `python -P -m app.db.migrate` (ADR 0008).

NAIVE, fail-first commit of M0 PR 5a: the runner applies new files as whoever logs in,
without SET ROLE, and does not compare the sha256 of applied files. The next commit
adds both.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql

from app.config.settings import Settings
from app.db import names
from app.db.connect import connect

MIGRATIONS = Path(__file__).resolve().parents[2] / "db" / "migrations"
FILE_NAME = re.compile(r"(?P<version>\d{4})_[a-z0-9]+(?:_[a-z0-9]+)*\.sql")

RECORD = f"{names.META_SCHEMA}.schema_migrations"
_META = sql.Identifier(names.META_SCHEMA)
_RECORD = sql.Identifier(names.META_SCHEMA, "schema_migrations")
BOOTSTRAP = (
    sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(_META),
    sql.SQL("REVOKE ALL ON SCHEMA {} FROM PUBLIC").format(_META),
    sql.SQL(
        """CREATE TABLE IF NOT EXISTS {} (
        version text PRIMARY KEY,
        file_name text NOT NULL UNIQUE,
        sha256 text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{{64}}$'),
        session_user_name name NOT NULL,
        current_user_name name NOT NULL,
        applied_at timestamptz NOT NULL DEFAULT now()
    )"""
    ).format(_RECORD),
    sql.SQL("REVOKE ALL ON {} FROM PUBLIC").format(_RECORD),
)
LOCK_RECORD = sql.SQL("LOCK TABLE {} IN EXCLUSIVE MODE").format(_RECORD)
SELECT_APPLIED = sql.SQL("SELECT version, file_name, sha256 FROM {}").format(_RECORD)
INSERT_APPLIED = sql.SQL(
    "INSERT INTO {} (version, file_name, sha256, session_user_name, current_user_name)"
    " VALUES (%s, %s, %s, session_user, current_user)"
).format(_RECORD)


class MigrationError(Exception):
    """A run the runner refuses. The message names files and rules, not file contents."""


@dataclass(frozen=True)
class Migration:
    version: str
    file_name: str
    sha256: str
    sql: str


def load(directory: Path) -> list[Migration]:
    """The migrations in directory, numbered 0001 upwards without gaps."""
    if not directory.is_dir():
        raise MigrationError(f"no migrations directory at {directory}")
    found = []
    for path in sorted(directory.iterdir()):
        match = FILE_NAME.fullmatch(path.name)
        if not match or path.is_symlink() or not path.is_file():
            raise MigrationError(f"{path.name}: not a NNNN_name.sql file")
        data = path.read_bytes()
        found.append(
            Migration(match["version"], path.name, hashlib.sha256(data).hexdigest(), data.decode())
        )
    versions = [migration.version for migration in found]
    expected = [f"{number:04d}" for number in range(1, len(found) + 1)]
    if versions != expected:
        raise MigrationError(f"versions {versions}, expected {expected}")
    return found


def run(url: str, directory: Path = MIGRATIONS) -> list[str]:
    """Apply the new migrations; return their file names."""
    migrations = load(directory)
    with connect(url, autocommit=True) as conn:
        with conn.transaction():
            for statement in BOOTSTRAP:
                conn.execute(statement)
        with conn.transaction():
            conn.execute(LOCK_RECORD)
            rows = conn.execute(SELECT_APPLIED).fetchall()
            done = {version for version, _, _ in rows}
            new = [migration for migration in migrations if migration.version not in done]
            for migration in new:
                apply(conn, migration)
    return [migration.file_name for migration in new]


def apply(conn: psycopg.Connection[Any], migration: Migration) -> None:
    # No parameters: psycopg sends the file as one simple query, statements and all.
    conn.execute(migration.sql)
    conn.execute(INSERT_APPLIED, (migration.version, migration.file_name, migration.sha256))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -P -m app.db.migrate",
        description="Apply db/migrations as the migration role (MIGRATION_DATABASE_URL).",
    )
    parser.add_argument("--migrations", type=Path, default=MIGRATIONS, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        url = Settings(_env_file=None).migration_database_url
        applied = run(url, args.migrations)
    except (MigrationError, ValueError) as refused:
        # ValueError: a database URL the settings check rejects.
        print(f"migrate: refused: {refused}", file=sys.stderr)
        return 1
    except psycopg.Error as error:
        print(f"migrate: {type(error).__name__} ({error.sqlstate}): {error}", file=sys.stderr)
        return 1
    for file_name in applied:
        print(f"migrate: applied {file_name}")
    if not applied:
        print("migrate: nothing to apply")
    return 0


if __name__ == "__main__":
    sys.exit(main())
