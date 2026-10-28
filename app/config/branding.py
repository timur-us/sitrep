"""Single source of every branding string: product, bank, domains, cast, demo mandate.

Generator, prompts, allowlists, tests and UI read from here and nowhere else
(CLAUDE.md). tests/test_branding.py fails if one of these strings appears in
any other file that is not prose.

Everything here is fictional. The domains use the reserved `.example` TLD
(RFC 2606), which never resolves.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


@dataclass(frozen=True)
class Product:
    name: str  # in prose and UI
    expansion: str  # spelled out once per document: "<name> (<expansion>)"
    slug: str  # in code, package, repo and image
    tagline: str


@dataclass(frozen=True)
class Bank:
    legal_name: str
    display_name: str  # UI, English
    name_stems: tuple[str, ...]  # whole words that name the bank on their own


@dataclass(frozen=True)
class Domains:
    staff: str  # staff and clients
    attacker: str
    canary: str


class Role(StrEnum):
    ADVISOR = "advisor"
    ATTACKER = "attacker"  # active: the crafted e-mail
    PASSIVE_READER = "passive_reader"  # logs, link logs, proxies
    MANY_IDENTITIES = "many_identities"
    TRUSTED_PARTY = "trusted_party"  # Entra ID; the server that issues approval tokens


@dataclass(frozen=True)
class Person:
    id: str
    name: str
    role: Role
    email: str


@dataclass(frozen=True)
class Mandate:
    id: str
    family: str
    advisor_id: str


PRODUCT = Product(name="", expansion="", slug="", tagline="")
BANK = Bank(legal_name="", display_name="", name_stems=())
DOMAINS = Domains(staff="", attacker="", canary="")
CAST: tuple[Person, ...] = ()
DEMO_MANDATE = Mandate(id="", family="", advisor_id="")

SLUG = PRODUCT.slug
ADVISORS = tuple(person for person in CAST if person.role is Role.ADVISOR)
