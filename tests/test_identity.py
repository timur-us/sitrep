"""The advisor a query runs for comes from the verified login only (CLAUDE.md rule 8).

app/db/session.py's advisor_transaction() takes a VerifiedAdvisor and nothing else: a
string, a dict, a look-alike object or a subclass raise TypeError before the pool is
touched. An AST scan keeps every construction of a VerifiedAdvisor inside app/auth/:
calls of the class (by any import name), of its __new__, and dataclasses.replace(). The
scan reads names: construction through getattr() or type() is not caught.
"""

from __future__ import annotations

import ast
import dataclasses

import pytest
from repo_files import ROOT

from app.auth.identity import VerifiedAdvisor
from app.config import branding
from app.config.settings import Settings
from app.db import session

APP = ROOT / "app"
AUTH = "app/auth/"
CLASS = "VerifiedAdvisor"
ADVISOR = branding.ADVISORS[0].id


def constructions(source: str, rel: str) -> list[str]:
    """Places outside app/auth/ that construct a VerifiedAdvisor."""
    if rel.startswith(AUTH):
        return []
    tree = ast.parse(source)
    aliases = {CLASS}
    replace_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            aliases |= {alias.asname or alias.name for alias in node.names if alias.name == CLASS}
            if node.module == "dataclasses":
                replace_names |= {
                    alias.asname or alias.name for alias in node.names if alias.name == "replace"
                }
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        where = f"{rel}:{node.lineno}"
        if name in aliases:
            found.append(f"{where}: {name}(...)")
        elif name == "__new__" and any(
            getattr(arg, "id", getattr(arg, "attr", None)) in aliases for arg in node.args
        ):
            found.append(f"{where}: __new__({CLASS})")
        elif (isinstance(func, ast.Name) and func.id in replace_names) or (
            isinstance(func, ast.Attribute)
            and func.attr == "replace"
            and isinstance(func.value, ast.Name)
            and func.value.id == "dataclasses"
        ):
            found.append(f"{where}: dataclasses.replace")
    return found


def test_verified_advisor_only_from_auth() -> None:
    sources = {
        path.relative_to(ROOT).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(APP.rglob("*.py"))
    }
    assert "app/auth/identity.py" in sources
    found = [item for rel, source in sorted(sources.items()) for item in constructions(source, rel)]
    assert not found, "\n".join(found)
    # Non-vacuity: the advisor transaction names the class it requires.
    assert CLASS in sources["app/db/session.py"], (
        "app/db/session.py does not require a VerifiedAdvisor"
    )


@pytest.mark.parametrize(
    "source",
    [
        "VerifiedAdvisor('x')\n",
        "from app.auth import identity\nidentity.VerifiedAdvisor('x')\n",
        "from app.auth.identity import VerifiedAdvisor as V\nV('x')\n",
        "VerifiedAdvisor.__new__(VerifiedAdvisor)\n",
        "object.__new__(VerifiedAdvisor)\n",
        "import dataclasses\ndataclasses.replace(advisor, advisor_id='x')\n",
        "from dataclasses import replace as r\nr(advisor, advisor_id='x')\n",
    ],
)
def test_planted_constructions_are_caught(source: str) -> None:
    assert constructions(source, "app/db/x.py"), source


def test_uses_that_construct_nothing_pass() -> None:
    source = (
        "from app.auth.identity import VerifiedAdvisor\n"
        "def f(advisor: VerifiedAdvisor) -> bool:\n"
        "    return isinstance(advisor, VerifiedAdvisor) and type(advisor) is VerifiedAdvisor\n"
        "'a'.replace('a', 'b')\n"
    )
    assert constructions(source, "app/db/x.py") == []
    assert constructions("VerifiedAdvisor('x')\n", "app/auth/dev_shim.py") == []


class LookAlike:
    def __init__(self, advisor_id: str) -> None:
        self.advisor_id = advisor_id


class Subclass(VerifiedAdvisor):
    __slots__ = ()


@pytest.mark.parametrize(
    "value",
    [ADVISOR, {"advisor_id": ADVISOR}, LookAlike(ADVISOR), Subclass(ADVISOR), None],
    ids=["str", "dict", "look-alike", "subclass", "None"],
)
def test_advisor_transaction_takes_only_a_verified_advisor(
    settings: Settings, value: object
) -> None:
    # The pool is never opened: an advisor transaction that reached it would raise
    # PoolClosed instead of TypeError.
    pool = session.make_pool(settings.database_url)
    try:
        with session.advisor_transaction(pool, value):  # type: ignore[arg-type]
            pass
    except TypeError as refused:
        assert CLASS in str(refused), refused
    except Exception as other:  # noqa: BLE001
        pytest.fail(f"a {type(value).__name__} reached the pool: {type(other).__name__}")
    else:
        pytest.fail(f"a {type(value).__name__} was accepted as the advisor")


def test_verified_advisor_is_frozen() -> None:
    advisor = VerifiedAdvisor(ADVISOR)
    with pytest.raises(dataclasses.FrozenInstanceError):
        advisor.advisor_id = "x"  # type: ignore[misc]
