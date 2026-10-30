"""The database tests fail, never skip: a skipped role test would pass CI with the
boundary untested. This scan keeps skip and xfail markers out of tests/db.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SKIPPING = frozenset({"skip", "skipif", "xfail", "importorskip"})


def skips(source: str) -> list[str]:
    """`pytest.skip(...)`, `pytest.mark.skipif`, `importorskip` and the like, by line."""
    found = []
    for node in ast.walk(ast.parse(source)):
        name = node.attr if isinstance(node, ast.Attribute) else getattr(node, "id", None)
        if isinstance(node, ast.alias):
            name = node.name
        if name in SKIPPING:
            found.append(f"{getattr(node, 'lineno', '?')}: {name}")
    return found


def test_database_tests_never_skip() -> None:
    files = sorted(HERE.glob("*.py"))
    assert len(files) >= 8, [path.name for path in files]
    found = [
        f"{path.name}:{line}"
        for path in files
        if path.name != Path(__file__).name
        for line in skips(path.read_text(encoding="utf-8"))
    ]
    assert not found, found


@pytest.mark.parametrize(
    "source",
    [
        "import pytest\npytest.skip('no db')\n",
        "import pytest\n@pytest.mark.skipif(True, reason='x')\ndef test_x(): pass\n",
        "import pytest\npytest.importorskip('psycopg')\n",
        "from pytest import skip\n",
        "import pytest\npytestmark = pytest.mark.xfail\n",
    ],
)
def test_planted_skips_are_caught(source: str) -> None:
    assert skips(source)
