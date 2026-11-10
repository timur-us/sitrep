"""The synthetic corpus: three advisors, 40 mandates, product sheets, German and English.

Run as `python -P -m data.synthetic.generate [--out DIR]` (`make data`). It writes
`advisors.jsonl`, `mandates.jsonl`, `documents.jsonl`, `product_sheets.jsonl` and
`manifest.json` (the seed, the generator version and a sha256 per file) into
`data/synthetic/out/`, which git ignores. `app/db/load.py` (`make load`) loads it.
Each product sheet carries its exact URL as a field (`url`), which its body names too.

- **Deterministic.** The standard library and `app.config.branding` only, one
  `random.Random(SEED)`, no clock, no
  locale and no set iteration: the same templates give byte-identical files whatever
  PYTHONHASHSEED, TZ or LC_ALL say. Dates count back from `AS_OF`; call notes, and
  e-mails that name the report, fall after `REPORT_DATE`.
- **Synthetic.** The cast and the demo mandate come from `app/config/branding.py`, the
  other client families from `templates/family_names.txt` (chosen for this project, no
  well-known person; some are ordinary real surnames with nothing attached), the text from
  `templates/{de,en}/*.txt`. No IBAN, phone number or street address is generated, and
  the only domain is the staff domain.
- **No attack content.** Attacks enter from `redteam/attacks/` in M1, through placeholders.
- **Origin and trust per document, from its type only** (`ORIGIN`, `trust_for`). The
  mailbox is always untrusted, whoever sent the e-mail. Trust says where the text comes
  from; every retrieval hit is still wrapped as untrusted in the harness (CLAUDE.md rule 9).
  The other value is `internal`, not `trusted`, so that no reader takes it as permission
  to follow what the text says.
- **Writes only where asked.** A linked output directory or a linked file in it is
  refused (exit 2), so a link planted in the git-ignored output cannot redirect a write.

Templates are data (rule 9). They are read as plain text and filled by a one-pass
`{name}` substitution, never by `str.format`, which follows attribute and index lookups.
An unknown placeholder or a stray brace stops the run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from app.config import branding

SEED = 20261028
GENERATOR_VERSION = "1"
AS_OF = date(2026, 10, 30)  # the corpus' "today"; nothing reads the clock
REPORT_DATE = date(2026, 9, 30)  # the portfolio reports' reference date

HERE = Path(__file__).resolve().parent
TEMPLATES = HERE / "templates"
DEFAULT_OUT = HERE / "out"

LANGS = {"german": "de", "english": "en"}  # documents.lang (ADR 0005) -> template directory
GERMAN_SHARE = 0.7  # of each advisor's mandates; the rest are English

DOC_TYPES = ("kyc", "portfolio_report", "call_note", "email", "product_sheet")
ORIGIN = {
    "kyc": "kyc_register",
    "portfolio_report": "portfolio_system",
    "call_note": "call_notes",
    "email": "mailbox",
    "product_sheet": "product_catalogue",
}
_TRUST = {
    "kyc": "internal",
    "portfolio_report": "internal",
    "call_note": "internal",
    "email": "untrusted",
    "product_sheet": "internal",
}

CALL_NOTES = (2, 3)  # per mandate, inclusive
EMAILS = (3, 6)

# Template file names, by kind: "note_1.txt" is a variant of "note".
KINDS = ("kyc", "report", "note", "mail_client", "mail_advisor", "product")
PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")
PLACEHOLDERS = frozenset(
    {
        "advisor_email",
        "advisor_name",
        "asset_class",
        "bank",
        "bonds",
        "cash",
        "client_email",
        "client_since",
        "date",
        "equities",
        "equity_share",
        "family",
        "horizon",
        "min_investment",
        "next_date",
        "objective",
        "ongoing_charges",
        "performance",
        "positions",
        "product",
        "product_url",
        "residence",
        "risk_profile",
        "source_of_wealth",
        "sri",
        "total",
    }
)

# Asset classes in report order: (key, German label, English label).
ASSET_CLASSES = (
    ("cash", "Liquidität", "Cash"),
    ("bonds", "Anleihen", "Bonds"),
    ("equities", "Aktien", "Equities"),
    ("real_estate", "Immobilienfonds", "Real estate funds"),
    ("alternatives", "Alternative Anlagen", "Alternatives"),
)

# Weights per risk profile, in ASSET_CLASSES order (alternatives only sometimes held).
WEIGHTS = {
    "conservative": (12, 55, 18, 15, 5),
    "balanced": (8, 37, 40, 15, 6),
    "growth": (5, 20, 60, 10, 8),
}
RISK_PROFILES = {
    "conservative": ("konservativ", "conservative"),
    "balanced": ("ausgewogen", "balanced"),
    "growth": ("wachstumsorientiert", "growth-oriented"),
}
SOURCES_OF_WEALTH = (
    ("Verkauf eines Familienunternehmens", "sale of a family business"),
    ("Erbschaft", "inheritance"),
    ("Einkünfte aus freiberuflicher Tätigkeit", "income from a professional practice"),
    ("Verkauf von Immobilien", "sale of real estate"),
    ("Vergütung als Geschäftsführung", "executive compensation"),
)
OBJECTIVES = (
    ("Vermögenserhalt mit planbaren Ausschüttungen", "capital preservation with regular income"),
    ("langfristiger Vermögensaufbau", "long-term growth"),
    ("Vorsorge für die nächste Generation", "provision for the next generation"),
)
RESIDENCES = (("Deutschland", "Germany"), ("Österreich", "Austria"), ("Schweiz", "Switzerland"))
MONTHS_EN = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


@dataclass(frozen=True)
class Product:
    slug: str
    name: str  # invented
    lang: str
    asset_class: str  # a key of ASSET_CLASSES, or "multi_asset"
    sri: int  # the PRIIPs summary risk indicator, 1 to 7
    ongoing_charges_bp: int  # basis points
    min_investment_eur: int


PRODUCTS = (
    Product("lindenhof-renten", "Lindenhof Rentenportfolio", "german", "bonds", 2, 45, 25_000),
    Product("nordlicht-aktien", "Nordlicht Aktien Europa", "german", "equities", 4, 95, 25_000),
    Product("kontor-reserve", "Kontor Liquiditätsreserve", "german", "cash", 1, 15, 10_000),
    Product(
        "basalt-immobilien", "Basalt Immobilien Spezial", "german", "real_estate", 3, 110, 50_000
    ),
    Product(
        "harbourline-equity", "Harbourline Global Equity", "english", "equities", 5, 85, 25_000
    ),
    Product(
        "silverbirch-income", "Silverbirch Balanced Income", "english", "multi_asset", 3, 70, 25_000
    ),
)
MULTI_ASSET = ("Mischfonds", "Multi-asset")
PRODUCT_PATH = {"german": "produkte", "english": "products"}


class OutputError(ValueError):
    """An output path that is a symbolic link."""


class TemplateError(ValueError):
    """A template that cannot be filled: unknown placeholder, stray brace, no title."""


@dataclass(frozen=True)
class Template:
    name: str  # "de/note_1.txt"
    title: str
    body: str


# How a template names the portfolio report, per language.
REPORT_NAMES = {"german": "Vermögensbericht", "english": "portfolio report"}


def mentions_report(template: Template, lang: str) -> bool:
    """Whether the template names the portfolio report: then it is dated after it."""
    name = REPORT_NAMES[lang].lower()
    return name in template.title.lower() or name in template.body.lower()


def trust_for(doc_type: str) -> str:
    """A document's trust, from its type alone: never from a sender, address or text."""
    return _TRUST[doc_type]


def fill(text: str, values: dict[str, str], where: str) -> str:
    """Replace each `{name}` with values[name], in one pass: a value is never re-read."""
    for brace in re.finditer(r"[{}]", PLACEHOLDER.sub("", text)):
        raise TemplateError(f"{where}: stray {brace.group(0)!r}")

    def value(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in values:
            raise TemplateError(f"{where}: unknown placeholder {{{name}}}")
        return values[name]

    return PLACEHOLDER.sub(value, text)


def load_templates(root: Path = TEMPLATES) -> dict[str, dict[str, list[Template]]]:
    """templates[lang][kind] -> the variants, sorted by file name."""
    found: dict[str, dict[str, list[Template]]] = {}
    for lang, directory in LANGS.items():
        kinds: dict[str, list[Template]] = {kind: [] for kind in KINDS}
        for path in sorted((root / directory).glob("*.txt")):
            kind = re.sub(r"_\d+$", "", path.stem)
            name = f"{directory}/{path.name}"
            if kind not in kinds:
                raise TemplateError(f"{name}: unknown kind {kind!r}")
            text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
            title, blank, body = text.partition("\n\n")
            if not blank or not title.strip() or "\n" in title:
                raise TemplateError(f"{name}: a one-line title, a blank line, then the body")
            unknown = sorted(set(PLACEHOLDER.findall(text)) - PLACEHOLDERS)
            if unknown:
                raise TemplateError(f"{name}: unknown placeholders {unknown}")
            kinds[kind].append(Template(name, title.strip(), body.strip() + "\n"))
        missing = [kind for kind, variants in kinds.items() if not variants]
        if missing:
            raise TemplateError(f"{directory}/: no template for {missing}")
        found[lang] = kinds
    return found


def load_family_names(path: Path = TEMPLATES / "family_names.txt") -> list[str]:
    """The client family names, one per line; `#` starts a comment line."""
    lines = path.read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.startswith("#")]


# Formatting: German "1.250.000,00 EUR" and "42,3 %", English "EUR 1,250,000.00" and
# "42.3%". Integer arithmetic only: cents and tenths of a percent.


def money(cents: int, lang: str) -> str:
    euros, rest = divmod(cents, 100)
    if lang == "german":
        return f"{euros:,}".replace(",", ".") + f",{rest:02d} EUR"
    return f"EUR {euros:,}.{rest:02d}"


def percent(tenths: int, lang: str, signed: bool = False) -> str:
    sign = ("+" if tenths > 0 else "-" if tenths < 0 else "") if signed else ""
    whole, rest = divmod(abs(tenths), 10)
    if lang == "german":
        return f"{sign}{whole},{rest} %"
    return f"{sign}{whole}.{rest}%"


def basis_points(points: int, lang: str) -> str:
    whole, rest = divmod(points, 100)
    return f"{whole},{rest:02d} %" if lang == "german" else f"{whole}.{rest:02d}%"


def long_date(day: date, lang: str) -> str:
    if lang == "german":
        return f"{day.day:02d}.{day.month:02d}.{day.year}"
    return f"{day.day} {MONTHS_EN[day.month - 1]} {day.year}"


def whole_euros(euros: int, lang: str) -> str:
    return money(euros * 100, lang)


def _pick(pair: tuple[str, str], lang: str) -> str:
    return pair[0] if lang == "german" else pair[1]


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


@dataclass(frozen=True)
class Portfolio:
    positions: tuple[tuple[str, int], ...]  # (asset class key, cents), report order
    total: int
    performance: int  # tenths of a percent, year to date

    def share(self, key: str) -> int:
        """The position's share of the total, in tenths of a percent, rounded half up."""
        cents = dict(self.positions)[key]
        return (cents * 1000 * 2 + self.total) // (self.total * 2)


def portfolio(rng: random.Random, profile: str) -> Portfolio:
    base = rng.randint(400, 9_000) * 1_000  # euros
    with_alternatives = rng.random() < 0.5
    positions = []
    for (key, _, _), weight in zip(ASSET_CLASSES, WEIGHTS[profile], strict=True):
        if key == "alternatives" and not with_alternatives:
            continue
        jitter = rng.randint(85, 115)
        # euros * weight % * jitter %, in cents, plus some odd cents
        cents = base * weight * jitter // 100 + rng.randint(0, 99)
        positions.append((key, cents))
    total = sum(cents for _, cents in positions)
    return Portfolio(tuple(positions), total, rng.randint(-30, 95))


def positions_block(folio: Portfolio, lang: str) -> str:
    labels = {key: _pick((de, en), lang) for key, de, en in ASSET_CLASSES}
    return "\n".join(
        f"- {labels[key]}: {money(cents, lang)} ({percent(folio.share(key), lang)})"
        for key, cents in folio.positions
    )


@dataclass(frozen=True)
class Context:
    """Everything a mandate's templates may name."""

    values: dict[str, str]
    lang: str


def _document(
    *,
    advisor_id: str | None,
    mandate_id: str | None,
    doc_id: str,
    doc_type: str,
    lang: str,
    title: str,
    body: str,
    day: date,
    sender: str | None = None,
    recipients: tuple[str, ...] = (),
) -> dict[str, object]:
    record: dict[str, object] = {
        "id": doc_id,
        "type": doc_type,
        "lang": lang,
        "title": title,
        "body": body,
        "date": day.isoformat(),
        "origin": ORIGIN[doc_type],
        "trust": trust_for(doc_type),
    }
    if advisor_id is not None:
        record |= {
            "advisor_id": advisor_id,
            "mandate_id": mandate_id,
            "sender": sender,
            "recipients": list(recipients),
        }
    return record


def _render(template: Template, values: dict[str, str]) -> tuple[str, str]:
    return fill(template.title, values, template.name), fill(template.body, values, template.name)


def _days_back(rng: random.Random, count: int, low: int, high: int) -> list[date]:
    """count distinct dates between high and low days before AS_OF, oldest first."""
    offsets = sorted(rng.sample(range(low, high + 1), count), reverse=True)
    return [AS_OF - timedelta(days=offset) for offset in offsets]


def build(
    templates: dict[str, dict[str, list[Template]]] | None = None,
    family_names: list[str] | None = None,
) -> dict[str, list[dict[str, object]]]:
    """The corpus as records per output file (without the manifest)."""
    templates = templates if templates is not None else load_templates()
    families = list(family_names if family_names is not None else load_family_names())
    rng = random.Random(SEED)
    staff = branding.DOMAINS.staff
    demo = branding.DEMO_MANDATE
    advisors = {person.id: person for person in branding.ADVISORS}

    # Mandates per advisor: 13 each, and one more for the demo mandate's advisor (14+13+13).
    counts = {advisor_id: 13 for advisor_id in advisors}
    counts[demo.advisor_id] += 1
    needed = sum(counts.values()) - 1
    if len(families) < needed or len(set(families)) != len(families):
        raise ValueError(f"family_names.txt: need {needed} distinct names, got {len(families)}")
    rng.shuffle(families)

    mandates: list[dict[str, object]] = []
    queue = iter(families)
    for advisor_id, count in counts.items():
        own = [demo.family] if advisor_id == demo.advisor_id else []
        own += [next(queue) for _ in range(count - len(own))]
        english = round(count * (1 - GERMAN_SHARE))
        # The demo mandate is German; the English ones are drawn from the rest.
        candidates = [family for family in own if family != demo.family]
        english_families = set(rng.sample(candidates, english))
        for family in own:
            mandate_id = demo.id if family == demo.family else _slug(family)
            mandates.append(
                {
                    "advisor_id": advisor_id,
                    "id": mandate_id,
                    "family": family,
                    "lang": "english" if family in english_families else "german",
                    "client_email": f"{mandate_id}.family@{staff}",
                }
            )

    products_by_lang = {
        lang: [product for product in PRODUCTS if product.lang == lang] for lang in LANGS
    }
    documents: list[dict[str, object]] = []
    for mandate in mandates:
        documents += _mandate_documents(rng, templates, mandate, advisors, products_by_lang)

    sheets = [_product_sheet(rng, templates, product) for product in PRODUCTS]
    advisor_rows = [
        {"advisor_id": person.id, "display_name": person.name, "email": person.email}
        for person in advisors.values()
    ]
    return {
        "advisors.jsonl": advisor_rows,
        "mandates.jsonl": mandates,
        "documents.jsonl": documents,
        "product_sheets.jsonl": sheets,
    }


def _mandate_documents(
    rng: random.Random,
    templates: dict[str, dict[str, list[Template]]],
    mandate: dict[str, object],
    advisors: dict[str, branding.Person],
    products_by_lang: dict[str, list[Product]],
) -> list[dict[str, object]]:
    lang = str(mandate["lang"])
    mandate_id = str(mandate["id"])
    advisor = advisors[str(mandate["advisor_id"])]
    client_email = str(mandate["client_email"])
    variants = templates[lang]
    profile = rng.choice(sorted(WEIGHTS))
    folio = portfolio(rng, profile)
    cents = dict(folio.positions)
    bank = branding.BANK.legal_name if lang == "german" else branding.BANK.display_name

    values = {
        "family": str(mandate["family"]),
        "bank": bank,
        "advisor_name": advisor.name,
        "advisor_email": advisor.email,
        "client_email": client_email,
        "client_since": str(rng.randint(2004, 2023)),
        "risk_profile": _pick(RISK_PROFILES[profile], lang),
        "horizon": str(rng.choice((5, 7, 10, 15))),
        "source_of_wealth": _pick(rng.choice(SOURCES_OF_WEALTH), lang),
        "residence": _pick(rng.choice(RESIDENCES), lang),
        "objective": _pick(rng.choice(OBJECTIVES), lang),
        "positions": positions_block(folio, lang),
        "total": money(folio.total, lang),
        "performance": percent(folio.performance, lang, signed=True),
        "cash": money(cents["cash"], lang),
        "bonds": money(cents["bonds"], lang),
        "equities": money(cents["equities"], lang),
        "equity_share": percent(folio.share("equities"), lang),
    }
    docs: list[dict[str, object]] = []

    def add(doc_id: str, doc_type: str, template: Template, day: date, **mail: object) -> None:
        title, body = _render(template, values | {"date": long_date(day, lang)})
        docs.append(
            _document(
                advisor_id=advisor.id,
                mandate_id=mandate_id,
                doc_id=doc_id,
                doc_type=doc_type,
                lang=lang,
                title=title,
                body=body,
                day=day,
                **mail,  # type: ignore[arg-type]
            )
        )

    (kyc_day,) = _days_back(rng, 1, 30, 400)
    add(f"{mandate_id}-kyc", "kyc", variants["kyc"][0], kyc_day)
    add(f"{mandate_id}-report", "portfolio_report", variants["report"][0], REPORT_DATE)

    # The notes quote the report, so they fall between its date and AS_OF (Fable's review).
    since_report = (AS_OF - REPORT_DATE).days
    note_days = _days_back(rng, rng.randint(*CALL_NOTES), 1, since_report)
    for number, day in enumerate(note_days, start=1):
        values["product"] = rng.choice(products_by_lang[lang]).name
        values["next_date"] = long_date(day + timedelta(days=rng.randint(14, 45)), lang)
        add(f"{mandate_id}-note-{number}", "call_note", rng.choice(variants["note"]), day)

    mail_days = _days_back(rng, rng.randint(*EMAILS), 1, 60)
    for number, day in enumerate(mail_days, start=1):
        # The first e-mail is the client's; replies come from the advisor's own address.
        from_client = number == 1 or rng.random() < 0.6
        kind = "mail_client" if from_client else "mail_advisor"
        template = rng.choice(variants[kind])
        if mentions_report(template, lang) and day < REPORT_DATE:
            # An e-mail about the report comes after it (Timur, on #18); the others keep
            # their date, so the numbers no longer follow the dates for these few.
            (day,) = _days_back(rng, 1, 1, since_report)
        values["product"] = rng.choice(products_by_lang[lang]).name
        values["next_date"] = long_date(day + timedelta(days=rng.randint(3, 14)), lang)
        sender, recipient = (
            (client_email, advisor.email) if from_client else (advisor.email, client_email)
        )
        add(
            f"{mandate_id}-mail-{number}",
            "email",
            template,
            day,
            sender=sender,
            recipients=(recipient,),
        )
    return docs


def _product_sheet(
    rng: random.Random, templates: dict[str, dict[str, list[Template]]], product: Product
) -> dict[str, object]:
    lang = product.lang
    labels = {key: _pick((de, en), lang) for key, de, en in ASSET_CLASSES}
    asset_class = labels.get(product.asset_class) or _pick(MULTI_ASSET, lang)
    (day,) = _days_back(rng, 1, 30, 180)
    url = product_url(product)
    values = {
        "product": product.name,
        "bank": branding.BANK.legal_name if lang == "german" else branding.BANK.display_name,
        "asset_class": asset_class,
        "sri": str(product.sri),
        "ongoing_charges": basis_points(product.ongoing_charges_bp, lang),
        "min_investment": whole_euros(product.min_investment_eur, lang),
        "product_url": url,
        "date": long_date(day, lang),
    }
    title, body = _render(templates[lang]["product"][0], values)
    record = _document(
        advisor_id=None,
        mandate_id=None,
        doc_id=f"product-{product.slug}",
        doc_type="product_sheet",
        lang=lang,
        title=title,
        body=body,
        day=day,
    )
    # The structured URL the loader stores (ADR 0010): the same one the body names.
    return record | {"url": url}


def product_url(product: Product) -> str:
    """The sheet's exact URL: https, the staff domain, its language's path and its slug."""
    return f"https://{branding.DOMAINS.staff}/{PRODUCT_PATH[product.lang]}/{product.slug}"


def serialise(records: list[dict[str, object]]) -> bytes:
    """One JSON object per line, keys sorted, UTF-8, LF line ends."""
    lines = [
        json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        for record in records
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


def write(out: Path, corpus: dict[str, list[dict[str, object]]]) -> str:
    """Write the files and the manifest into out; return the manifest's sha256.

    Links are refused before anything is written: the directory itself, or any file the
    run would write. (Checked, then written: a link planted in between still wins. The
    output is local and git-ignored.)
    """
    hashes = {}
    files = {name: serialise(records) for name, records in sorted(corpus.items())}
    for name, data in files.items():
        hashes[name] = hashlib.sha256(data).hexdigest()
    manifest = {"files": hashes, "generator_version": GENERATOR_VERSION, "seed": SEED}
    files["manifest.json"] = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    if out.is_symlink():
        raise OutputError(f"{out} is a symbolic link")
    linked = sorted(name for name in files if (out / name).is_symlink())
    if linked:
        raise OutputError(f"{out}: symbolic links in place of {linked}")
    out.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        (out / name).write_bytes(data)
    return hashlib.sha256(files["manifest.json"]).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -P -m data.synthetic.generate",
        description="Regenerate the synthetic corpus (fixed seed).",
    )
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="output directory")
    args = parser.parse_args(argv)
    corpus = build()
    try:
        digest = write(args.out, corpus)
    except OutputError as error:
        print(f"data: refused: {error}", file=sys.stderr)
        return 2
    counts = ", ".join(f"{len(records)} {name}" for name, records in sorted(corpus.items()))
    print(f"data: {counts}; manifest sha256 {digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
