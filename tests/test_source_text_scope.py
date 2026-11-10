"""source_text() has one caller: the number guard (M1 plan, PR 9).

app/harness/context.py keeps a tool result's text behind Untrusted, and wrap() is the
way to the model. source_text() is the second way out, unescaped and outside any
delimiter, for the number guard alone (app/harness/grounding.py). An AST scan of app/
reports any use of the name (a name, an attribute, an import, an alias, a definition or a
string) outside its definition in context.py and its uses in grounding.py.

Like the scans in tests/test_identity.py and tests/test_loader_scope.py, it reads names
and literals: getattr() with a string built at runtime is not caught. Docstrings are
prose and are not read as strings.
"""

from __future__ import annotations

import ast
from collections import Counter

import pytest
from repo_files import ROOT

NAME = "source_text"
CONTEXT = "app/harness/context.py"
GROUNDING = "app/harness/grounding.py"


def docstrings(tree: ast.AST) -> set[int]:
    """The ids of the docstring nodes of the module, its classes and its functions."""
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                found.add(id(body[0].value))
    return found


def scan(source: str, rel: str) -> tuple[list[str], Counter[str]]:
    """The uses of source_text outside the two sanctioned files, and what was seen."""
    tree = ast.parse(source)
    prose = docstrings(tree)
    found: list[str] = []
    seen: Counter[str] = Counter()
    # In grounding.py the name may only be called: an alias or a rebinding would pass
    # the function on.
    called = {
        id(node.func)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    for node in ast.walk(tree):
        where = f"{rel}:{getattr(node, 'lineno', '?')}"
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == NAME:
            if rel == CONTEXT:
                seen["definitions"] += 1
            else:
                found.append(f"{where}: defines {NAME}")
        elif isinstance(node, ast.Name) and node.id == NAME:
            if rel == GROUNDING and id(node) in called:
                seen["calls"] += 1
            else:
                found.append(f"{where}: {NAME} used")
        elif isinstance(node, ast.Attribute) and node.attr == NAME:
            found.append(f"{where}: .{NAME} used")
        elif isinstance(node, ast.alias) and NAME in (node.name, node.asname):
            if rel == GROUNDING and node.name == NAME and node.asname is None:
                seen["imports"] += 1
            else:
                found.append(f"{where}: imports {NAME}")
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str | bytes)
            and id(node) not in prose
        ):
            text = node.value if isinstance(node.value, str) else node.value.decode("latin-1")
            if NAME in text:
                found.append(f"{where}: a string names {NAME}")
    return found, seen


def app_sources() -> dict[str, str]:
    return {
        path.relative_to(ROOT).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted((ROOT / "app").rglob("*.py"))
    }


def test_source_text_has_one_caller() -> None:
    sources = app_sources()
    for required in (CONTEXT, GROUNDING):
        assert required in sources, f"{required} was not scanned"
    found: list[str] = []
    seen: Counter[str] = Counter()
    for rel, text in sources.items():
        file_found, file_seen = scan(text, rel)
        found += file_found
        seen += file_seen
    assert not found, "\n".join(found)
    # Non-vacuity: the scan sees the definition, the one import and a call.
    assert seen["definitions"] == 1, seen
    assert seen["imports"] == 1, seen
    assert seen["calls"] >= 1, seen


PLANTED = [
    ("app/harness/loop.py", "from app.harness.context import source_text\n", "imports"),
    (
        "app/harness/loop.py",
        "from app.harness import context\ncontext.source_text(x)\n",
        ".source_text",
    ),
    (GROUNDING, "alias = source_text\n", "source_text used"),
    (GROUNDING, "source_text = None\n", "source_text used"),
    (GROUNDING, "f(source_text)\n", "source_text used"),
    ("app/api/ask.py", "import app.harness.context as c\nc.source_text\n", ".source_text"),
    ("app/harness/loop.py", "source_text(x)\n", "source_text used"),
    ("app/harness/loop.py", "getattr(context, 'source_text')\n", "a string names"),
    ("app/harness/loop.py", "def source_text(x):\n    pass\n", "defines"),
    (GROUNDING, "from app.harness.context import source_text as text\n", "imports"),
    (GROUNDING, "def source_text(x):\n    pass\n", "defines"),
    (GROUNDING, "context.source_text(x)\n", ".source_text"),
    (CONTEXT, "source_text(x)\n", "source_text used"),
    (CONTEXT, "alias = source_text\n", "source_text used"),
    (CONTEXT, "__all__ = ['source_text']\n", "a string names"),
]


@pytest.mark.parametrize(("rel", "source", "needle"), PLANTED)
def test_planted_uses_are_caught(rel: str, source: str, needle: str) -> None:
    found, _ = scan(source, rel)
    assert any(needle in item for item in found), found


def test_the_sanctioned_uses_pass() -> None:
    grounding = (
        '"""The guard reads source_text() and says so in prose."""\n'
        "from app.harness.context import source_text\n"
        "def check(sources):\n"
        "    return [source_text(item) for item in sources]\n"
    )
    found, seen = scan(grounding, GROUNDING)
    assert found == [] and seen["imports"] == 1 and seen["calls"] == 1, (found, seen)
    context = '"""Holds source_text()."""\ndef source_text(item):\n    """Its doc."""\n'
    found, seen = scan(context, CONTEXT)
    assert found == [] and seen["definitions"] == 1, (found, seen)
