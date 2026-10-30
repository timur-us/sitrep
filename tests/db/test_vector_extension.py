"""The server is PostgreSQL 17, and pgvector 0.8.7 is installed in the app's database."""

from __future__ import annotations

import pytest

from app.config.settings import Settings
from app.db.connect import connect

pytestmark = pytest.mark.usefixtures("database")


def test_vector_extension(settings: Settings) -> None:
    with connect(settings.database_url) as app:
        found = app.execute(
            "SELECT extversion, extnamespace::regnamespace::text FROM pg_extension"
            " WHERE extname = 'vector'"
        ).fetchall()
        assert found == [("0.8.7", "public")], f"vector in pg_extension: {found}"
        # The app role can use the type through its search_path.
        assert app.execute("SELECT '[1,2,3]'::vector::text").fetchone() == ("[1,2,3]",)
        major = app.execute("SELECT current_setting('server_version_num')::int / 10000")
        assert major.fetchone() == (17,)
