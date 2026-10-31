"""The server is PostgreSQL 17, and pgvector is installed in the app's database, at the
version compose.yaml's image tag names (a Dependabot bump moves both together). The
embedding column has the model's dimension and an HNSW index for cosine distance.
"""

from __future__ import annotations

import re

import pytest
from repo_files import ROOT

from app.config.embedding import DIM
from app.config.settings import Settings
from app.db import names
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


def test_vector_dimension(settings: Settings) -> None:
    # chunks.embedding has the model's dimension (ADR 0005), and each vector carries the
    # model and revision that made it.
    with connect(settings.database_url) as app:
        columns = app.execute(
            "SELECT a.attname, a.atttypid::regtype::text, a.atttypmod, a.attnotnull"
            " FROM pg_attribute a WHERE a.attrelid = to_regclass(%s)"
            " AND a.attname IN ('embedding', 'embedding_model', 'embedding_revision')"
            " ORDER BY a.attname",
            (f"{names.SCHEMA}.chunks",),
        ).fetchall()
        assert columns == [
            ("embedding", "vector", DIM, True),
            ("embedding_model", "text", -1, True),
            ("embedding_revision", "text", -1, True),
        ], columns
        index = app.execute(
            "SELECT am.amname, opc.opcname FROM pg_index i"
            " JOIN pg_class c ON c.oid = i.indexrelid JOIN pg_am am ON am.oid = c.relam"
            " JOIN pg_opclass opc ON opc.oid = i.indclass[0]"
            " WHERE i.indrelid = to_regclass(%s)"
            " AND i.indkey[0] = (SELECT attnum FROM pg_attribute"
            "  WHERE attrelid = i.indrelid AND attname = 'embedding')",
            (f"{names.SCHEMA}.chunks",),
        ).fetchall()
        assert index == [("hnsw", "vector_cosine_ops")], f"indexes on chunks.embedding: {index}"
