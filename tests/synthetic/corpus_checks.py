"""Detectors and figure checks for the synthetic corpus (tests/synthetic/).

- **Real-looking data:** strings that pass the IBAN mod-97 check, phone numbers, street
  addresses, hosts other than the staff domain, and the attacker and canary domains. Each
  detector returns what it found, so the tests can show it; the corpus is synthetic, so
  nothing here is a real value. Text is read as NFKC first (`app.runners.corpus.normalize`),
  so no-break spaces, fullwidth digits and invisible characters do not hide a match.
- **Attack markers:** instructions aimed at an assistant (English and German) and markup
  that renders (Markdown images and links, HTML tags). The corpus must carry none: attacks
  enter from `redteam/attacks/` in M1.
- **Figures:** amounts in euros and percentages, in the German (`1.250.000,00 EUR`,
  `42,3 %`) and the English format (`EUR 1,250,000.00`, `42.3%`), as (unit, Decimal).
  Dates, years and counts are not figures.
- **Report checks:** a portfolio report's total equals the sum of its positions, each
  position's share matches its amount, and every figure in a call note appears in its
  mandate's report.

The checks are regular expressions over text. They are a net for what the generator
might emit, not a proof that no personal data or attack can exist in any text. Known
misses: street names without a street word ("Am Markt 3") or of several words ("Platz der
Republik 1"), and phrasing the marker list does not name.
"""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal

from app.config import branding
from app.runners import corpus

# IBAN lengths by country (ISO 13616 registry) for the countries a DACH bank sees most.
# An unknown country is checked at its longest candidate only.
IBAN_LENGTHS = {
    "AT": 20,
    "BE": 16,
    "CH": 21,
    "DE": 22,
    "ES": 24,
    "FR": 27,
    "GB": 22,
    "IT": 27,
    "LI": 21,
    "LU": 20,
    "NL": 18,
}
# Two letters, two check digits, then letters and digits with single spaces between.
IBAN_START = re.compile(r"(?<![A-Za-z0-9])([A-Za-z]{2}\d{2})((?: ?[A-Za-z0-9]){11,30})")

# "+49 30 1234567", "+49 (0)30 1234567", "(030) 1234567", "030/1234567", "0171 1234567":
# a country code (optionally with "(0)"), an area code in parentheses, or a leading 0, then
# at least six more digits with single spaces, slashes or hyphens between. Amounts
# ("1.250.000,00"), shares ("(12,0 %)"), dates ("30.10.2026", "2026-10-30") and "Nr. 3" do
# not match.
PHONE = re.compile(r"(?<![\w+.,])(?:\+\d{1,3} ?(?:\(0\) ?)?|\(0\d{1,5}\) ?|0)(?:[ /-]?\d){6,}")

STREET = re.compile(
    r"\b[A-ZÄÖÜ][a-zäöüß]*(?:straße|strasse|str\.|weg|allee|platz|gasse|ring|damm|ufer)"
    r"\s+\d{1,4}[a-z]?\b"
    # "Berliner Straße 5": a capitalised word, then the street word on its own.
    r"|\b[A-ZÄÖÜ][a-zäöüß]+\s+(?:Straße|Strasse|Str\.|Allee|Weg|Platz|Gasse|Ring|Damm|Ufer)"
    r"\s+\d{1,4}[a-z]?\b"
    r"|\b\d{1,5}\s+(?:[A-Z][a-z]+\s+){1,3}(?:Street|St\.|Road|Rd\.|Avenue|Ave\.|Lane|Drive|Square)\b"
)

_AMOUNT_DE = r"(?P<de>\d{1,3}(?:\.\d{3})*,\d{2}) ?EUR\b"
_AMOUNT_EN = r"\bEUR ?(?P<en>\d{1,3}(?:,\d{3})*\.\d{2})"
_PERCENT = r"(?P<pct>[+-]?\d+(?:[.,]\d+)?) ?%"
FIGURE = re.compile(rf"{_AMOUNT_DE}|{_AMOUNT_EN}|{_PERCENT}")

TOTAL_LABELS = {"german": "Gesamtvermögen", "english": "Total assets"}

# Case-insensitive: instructions to an assistant, in English and German, and markup.
ATTACK_MARKERS = re.compile(
    r"\bignor(?:e|es|ed|ing|iere|ieren|iert)\b"
    r"|\binstructions?\b|\banweisung(?:en)?\b"
    r"|\bassistant\b|\bassistent(?:in)?\b"
    r"|\bsystem ?prompt\b|\bprompt\b"
    r"|\b(?:ki|llm|chatbot)\b"
    r"|\byou (?:must|are now|should now)\b|\bdu (?:musst|bist jetzt)\b"
    r"|!\[|\]\(|<\s*/?\s*[a-z][a-z0-9-]*(?=[\s>/])|\{\{",
    re.IGNORECASE,
)


def _mod97(compact: str) -> bool:
    rearranged = compact[4:] + compact[:4]
    digits = "".join(str(int(char, 36)) for char in rearranged)
    return int(digits) % 97 == 1


def ibans(text: str) -> list[str]:
    """Every candidate that passes the IBAN check, without spaces, in capitals."""
    found = []
    for match in IBAN_START.finditer(corpus.normalize(text)):
        compact = (match.group(1) + match.group(2).replace(" ", "")).upper()
        length = IBAN_LENGTHS.get(compact[:2], len(compact))
        candidate = compact[:length]
        if len(candidate) == length and _mod97(candidate):
            found.append(candidate)
    return found


def phones(text: str) -> list[str]:
    return [match.group(0) for match in PHONE.finditer(corpus.normalize(text))]


def streets(text: str) -> list[str]:
    return [match.group(0) for match in STREET.finditer(corpus.normalize(text))]


def foreign_hosts(text: str) -> list[str]:
    """Hosts in URLs, e-mail addresses and bare names that are not the staff domain."""
    staff = branding.DOMAINS.staff
    return [
        host for host in corpus.hosts(corpus.normalize(text)) if host.lower().rstrip(".") != staff
    ]


def figures(text: str) -> list[tuple[str, Decimal]]:
    """Amounts as ("EUR", value) and percentages as ("%", value), in text order."""
    found = []
    for match in FIGURE.finditer(text):
        if match.group("de") is not None:
            raw = match.group("de").replace(".", "").replace(",", ".")
            found.append(("EUR", Decimal(raw)))
        elif match.group("en") is not None:
            found.append(("EUR", Decimal(match.group("en").replace(",", ""))))
        else:
            found.append(("%", Decimal(match.group("pct").replace(",", "."))))
    return found


def report_problems(body: str, lang: str) -> list[str]:
    """The report's positions ("- " lines, one amount each) must add up to its total."""
    positions: list[Decimal] = []
    shares: list[tuple[int, Decimal, Decimal]] = []  # (line, amount, stated share)
    totals: list[Decimal] = []
    problems = []
    for number, line in enumerate(body.splitlines(), start=1):
        found = figures(line)
        amounts = [value for unit, value in found if unit == "EUR"]
        percents = [value for unit, value in found if unit == "%"]
        if line.startswith("- "):
            if len(amounts) != 1:
                problems.append(f"line {number}: a position with {len(amounts)} amounts")
            if len(percents) != 1:
                problems.append(f"line {number}: a position with {len(percents)} shares")
            elif len(amounts) == 1:
                shares.append((number, amounts[0], percents[0]))
            positions += amounts
        elif line.startswith(TOTAL_LABELS[lang] + ":"):
            totals += amounts
    if not positions:
        problems.append("no positions")
    if len(totals) != 1:
        problems.append(f"{len(totals)} total lines")
        return problems
    total = totals[0]
    if sum(positions) != total:
        problems.append(f"total {total} is not the sum of the positions {sum(positions)}")
    for number, amount, stated in shares:
        expected = (amount * 100 / total).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
        if total and stated != expected:
            problems.append(f"line {number}: share {stated} %, the amount gives {expected} %")
    return problems


def figure_problems(report: str, note: str) -> list[str]:
    """Figures in the note that the report does not carry; a note without one is a problem."""
    found = figures(note)
    if not found:
        return ["no figure"]
    known = set(figures(report))
    return [
        f"{unit} {value} is not in the report"
        for unit, value in found
        if (unit, value) not in known
    ]


def reserved_domains(text: str) -> list[str]:
    """The attacker and canary domains, anywhere in the text, without regard to case."""
    folded = corpus.normalize(text).lower()
    domains = branding.DOMAINS
    return [domain for domain in (domains.attacker, domains.canary) if domain in folded]


def attack_markers(text: str) -> list[str]:
    """Instructions aimed at an assistant and markup that renders, as found."""
    return [match.group(0) for match in ATTACK_MARKERS.finditer(corpus.normalize(text))]
