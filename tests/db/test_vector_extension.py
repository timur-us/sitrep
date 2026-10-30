"""The server is PostgreSQL 17, and pgvector is installed in the app's database, at the
version compose.yaml's image tag names (a Dependabot bump moves both together).
"""

from __future__ import annotations

import re

import pytest
from repo_files import ROOT

from app.config.settings import Settings
from app.db.connect import connect

pytestmark = pytest.mark.usefixtures("database")


def test_vector_extension(settings: Settings) -> None:
    with connect(settings.database_url) as app:
        found = app.execute(
            "SELECT extversion, extnamespace::regnamespace::text FROM pg_extension"
            " WHERE extname = 'vector'"
        ).fetchall()
        tag = re.search(
            r"pgvector/pgvector:(\d+\.\d+\.\d+)-pg17-", (ROOT / "compose.yaml").read_text()
        )
        assert tag, "compose.yaml names no pgvector image"
        assert found == [(tag.group(1), "public")], f"vector in pg_extension: {found}"
        # The app role can use the type through its search_path.
        assert app.execute("SELECT '[1,2,3]'::vector::text").fetchone() == ("[1,2,3]",)
        major = app.execute("SELECT current_setting('server_version_num')::int / 10000")
        assert major.fetchone() == (17,)
