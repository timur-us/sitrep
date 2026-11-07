"""The synthetic corpus generator: inert scaffolding (M0 PR 8, red).

The names the tests import exist; nothing is generated, loaded, filled or labelled yet.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

SEED = 20261028
GENERATOR_VERSION = "1"

HERE = Path(__file__).resolve().parent
TEMPLATES = HERE / "templates"
DEFAULT_OUT = HERE / "out"

KINDS = ("kyc", "report", "note", "mail_client", "mail_advisor", "product")
PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")
PLACEHOLDERS: frozenset[str] = frozenset()
ORIGIN: dict[str, str] = {}


class TemplateError(ValueError):
    """A template that cannot be filled."""


def trust_for(doc_type: str) -> str:
    return ""


def fill(text: str, values: dict[str, str], where: str) -> str:
    return text


def load_templates(root: Path = TEMPLATES) -> dict[str, dict[str, list[object]]]:
    return {}


def load_family_names(path: Path = TEMPLATES / "family_names.txt") -> list[str]:
    return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -P -m data.synthetic.generate")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.parse_args(argv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
