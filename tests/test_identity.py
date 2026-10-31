"""The advisor a query runs for comes from the verified login only (CLAUDE.md rule 8).

app/db/session.py's advisor_transaction() takes a VerifiedAdvisor and nothing else: a
string, a dict, a look-alike object or None raise TypeError before the pool is touched.
At runtime only app/auth/identity.issue() makes one; an instance cannot be changed,
copied, pickled or subclassed, and pydantic and FastAPI cannot build one from a request
(review finding sec-1: a dataclass let a route parameter turn JSON into one).

An AST scan of app/ outside app/auth/ reports any use of the class except in
annotations, isinstance() and `is` checks (calls, aliases, map() and the like), any use
of issue() or the issuer token, and any __setattr__ (object.__setattr__ can write the
slot). The scan reads names: construction through getattr(), globals() or a string is
not caught.
"""

from __future__ import annotations

import ast
import copy
import dataclasses
import pickle

import pydantic
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from repo_files import ROOT

from app.auth import identity
from app.auth.identity import VerifiedAdvisor
from app.config import branding
from app.config.settings import Settings
from app.db import session

APP = ROOT / "app"
AUTH = "app/auth/"
CLASS = "VerifiedAdvisor"
ISSUER = frozenset({"issue", "_ISSUER"})
ADVISOR = branding.ADVISORS[0].id


def constructions(source: str, rel: str) -> list[str]:
    """Places outside app/auth/ that could make or change a VerifiedAdvisor."""
    if rel.startswith(AUTH):
        return []
    tree = ast.parse(source)
    aliases = {CLASS}
    modules: set[str] = set()  # local names for app.auth.identity
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "app.auth.identity":
            aliases |= {alias.asname or alias.name for alias in node.names if alias.name == CLASS}
        if isinstance(node, ast.ImportFrom) and node.module == "app.auth":
            modules |= {
                alias.asname or alias.name for alias in node.names if alias.name == "identity"
            }
    # Nodes where the class may be named: annotations, isinstance() and `is` checks.
    allowed: set[int] = set()
    for node in ast.walk(tree):
        annotations = []
        if isinstance(node, ast.arg) and node.annotation is not None:
            annotations.append(node.annotation)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.returns:
            annotations.append(node.returns)
        if isinstance(node, ast.AnnAssign):
            annotations.append(node.annotation)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "isinstance"
            and len(node.args) == 2
        ):
            annotations.append(node.args[1])
        if isinstance(node, ast.Compare) and all(
            isinstance(op, ast.Is | ast.IsNot) for op in node.ops
        ):
            annotations += [node.left, *node.comparators]
        for annotation in annotations:
            allowed |= {id(sub) for sub in ast.walk(annotation)}
    found = []
    for node in ast.walk(tree):
        where = f"{rel}:{getattr(node, 'lineno', '?')}"
        if isinstance(node, ast.ImportFrom) and node.module == "app.auth.identity":
            found += [
                f"{where}: imports {alias.name}" for alias in node.names if alias.name in ISSUER
            ]
        named = (isinstance(node, ast.Name) and node.id in aliases) or (
            isinstance(node, ast.Attribute) and node.attr == CLASS
        )
        if named and id(node) not in allowed:
            found.append(f"{where}: {CLASS} outside an annotation, isinstance() or an is check")
        if isinstance(node, ast.Attribute) and node.attr in ISSUER:
            value = node.value
            if (isinstance(value, ast.Name) and value.id in modules) or (
                isinstance(value, ast.Attribute) and value.attr == "identity"
            ):
                found.append(f"{where}: identity.{node.attr}")
        if isinstance(node, ast.Attribute) and node.attr == "__setattr__":
            found.append(f"{where}: __setattr__")
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
        # Review findings sec-1 and test-11: an alias, a reference that is not a call,
        # writing the slot, and the issuer used outside app/auth/.
        "from app.auth.identity import VerifiedAdvisor\nV = VerifiedAdvisor\nV('x')\n",
        "from app.auth.identity import VerifiedAdvisor\nadvisors = map(VerifiedAdvisor, ids)\n",
        "object.__setattr__(advisor, '_advisor_id', 'x')\n",
        "from app.auth.identity import issue\nissue('x')\n",
        "from app.auth import identity\nidentity.issue('x')\n",
        "import app.auth.identity\napp.auth.identity.issue('x')\n",
        "from app.auth.identity import _ISSUER\n",
    ],
)
def test_planted_constructions_are_caught(source: str) -> None:
    assert constructions(source, "app/db/x.py"), source


def test_uses_that_construct_nothing_pass() -> None:
    source = (
        "from app.auth.identity import VerifiedAdvisor\n"
        "def f(advisor: VerifiedAdvisor) -> bool:\n"
        "    return isinstance(advisor, VerifiedAdvisor) and type(advisor) is VerifiedAdvisor\n"
        "def g(advisors: list[VerifiedAdvisor]) -> VerifiedAdvisor | None:\n"
        "    return None\n"
        "'a'.replace('a', 'b')\n"
    )
    assert constructions(source, "app/db/x.py") == []
    assert constructions("VerifiedAdvisor('x')\n", "app/auth/dev_shim.py") == []
    assert constructions("from app.auth.identity import issue\nissue('x')\n", "app/auth/x.py") == []


class LookAlike:
    def __init__(self, advisor_id: str) -> None:
        self.advisor_id = advisor_id


@pytest.mark.parametrize(
    "value",
    [ADVISOR, {"advisor_id": ADVISOR}, LookAlike(ADVISOR), None],
    ids=["str", "dict", "look-alike", "None"],
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


# At runtime (review finding sec-1): only issue() makes one, and nothing changes, copies,
# subclasses or deserialises one.


def test_only_issue_makes_a_verified_advisor() -> None:
    advisor = identity.issue(ADVISOR)
    assert type(advisor) is VerifiedAdvisor and advisor.advisor_id == ADVISOR
    assert advisor == identity.issue(ADVISOR) and hash(advisor) == hash(identity.issue(ADVISOR))
    assert ADVISOR in repr(advisor)
    for forged in ((ADVISOR,), (ADVISOR, object()), (ADVISOR, None)):
        with pytest.raises(TypeError, match="app/auth/"):
            VerifiedAdvisor(*forged)


@pytest.mark.parametrize("value", [123, None, b"x"], ids=["int", "None", "bytes"])
def test_issue_takes_a_string(value: object) -> None:
    with pytest.raises(TypeError):
        identity.issue(value)  # type: ignore[arg-type]


def test_verified_advisor_cannot_be_changed_or_copied() -> None:
    advisor = identity.issue(ADVISOR)
    with pytest.raises(AttributeError):
        advisor.advisor_id = "x"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        del advisor.advisor_id  # type: ignore[misc]
    for duplicate in (copy.copy, copy.deepcopy, pickle.dumps):
        with pytest.raises(TypeError):
            duplicate(advisor)
    with pytest.raises(TypeError):
        dataclasses.replace(advisor, advisor_id="x")  # type: ignore[type-var]
    assert advisor.advisor_id == ADVISOR


def test_verified_advisor_cannot_be_subclassed() -> None:
    with pytest.raises(TypeError):
        type("Subclass", (VerifiedAdvisor,), {"__slots__": ()})


def test_pydantic_cannot_build_a_verified_advisor() -> None:
    with pytest.raises(pydantic.PydanticSchemaGenerationError):
        pydantic.TypeAdapter(VerifiedAdvisor)


def test_a_route_cannot_take_a_verified_advisor_from_a_request() -> None:
    # A route parameter typed VerifiedAdvisor would turn a request body into one.
    app = FastAPI()
    called = []

    def handler(advisor: VerifiedAdvisor) -> str:
        called.append(advisor)
        return "ok"

    try:
        app.post("/probe")(handler)
    except Exception:  # noqa: BLE001
        return  # refused when the route is defined
    response = TestClient(app).post("/probe", json={"advisor_id": ADVISOR})
    assert not called, f"a request body became a VerifiedAdvisor ({response.status_code})"
