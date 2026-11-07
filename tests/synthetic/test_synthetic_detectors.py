"""The corpus detectors and the figure checks, on planted positives and negatives.

The planted IBANs are computed here from made-up account numbers, so the repository holds
no literal that passes the check. The phone numbers are the plan's shapes.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from corpus_checks import (
    figure_problems,
    figures,
    foreign_hosts,
    ibans,
    phones,
    report_problems,
    streets,
)

from app.config import branding


def _iban(country: str, bban: str) -> str:
    """An IBAN with valid check digits for a made-up BBAN (ISO 13616, mod 97-10)."""
    digits = "".join(str(int(char, 36)) for char in bban + country + "00")
    return f"{country}{98 - int(digits) % 97:02d}{bban}"


def _spaced(iban: str) -> str:
    return " ".join(iban[i : i + 4] for i in range(0, len(iban), 4))


# Made-up bank codes and account numbers: 18 digits for DE, 16 for AT, 17 for CH.
DE = _iban("DE", "100000001234567890")
AT = _iban("AT", "1000001234567890")
CH = _iban("CH", "00000001234567890")


def test_planted_ibans_are_valid() -> None:
    # Control: the helper builds what the check accepts, at each country's length.
    assert (len(DE), len(AT), len(CH)) == (22, 20, 21)
    assert ibans(DE) == [DE], DE


@pytest.mark.parametrize(
    "text",
    [
        DE,
        _spaced(DE),
        DE.lower(),
        _spaced(DE).lower(),
        f"Bitte auf {_spaced(AT)} überweisen.",
        f"IBAN: {CH}, BIC folgt",
        f"pay to {_spaced(DE)} und danke",
    ],
)
def test_iban_is_detected(text: str) -> None:
    assert ibans(text), f"not detected: {text!r}"


@pytest.mark.parametrize(
    "text",
    [
        DE[:2] + f"{(int(DE[2:4]) + 1) % 100:02d}" + DE[4:],  # wrong check digits
        "z.B. DE und AT",
        "1.250.000,00 EUR",
        "Nr. 3",
        "Kunde seit 2014, Anlagehorizont 10 Jahre",
    ],
)
def test_iban_negatives(text: str) -> None:
    assert ibans(text) == [], f"false positive on {text!r}"


@pytest.mark.parametrize(
    "text",
    ["+49 30 1234567", "030/1234567", "0171 1234567", "Tel. 0171-1234567", "+41 44 123 45 67"],
)
def test_phone_is_detected(text: str) -> None:
    assert phones(text), f"not detected: {text!r}"


@pytest.mark.parametrize(
    "text",
    [
        "z.B.",
        "1.250.000,00 EUR",
        "EUR 1,250,000.00",
        "Nr. 3",
        "30.10.2026",
        "2026-10-30",
        "05.03.2026",
        "0,45 % pro Jahr",
        "+3,2 %",
        "Kunde seit 2014",
    ],
)
def test_phone_negatives(text: str) -> None:
    assert phones(text) == [], f"false positive on {text!r}"


@pytest.mark.parametrize(
    "text", ["Lindenstraße 12", "Hauptstr. 3a", "Am Uferweg 7", "12 Elm Street", "5 Harbour Road"]
)
def test_street_is_detected(text: str) -> None:
    assert streets(text), f"not detected: {text!r}"


@pytest.mark.parametrize("text", ["Nr. 3", "z.B.", "Strategie bis 2027", "the street ahead"])
def test_street_negatives(text: str) -> None:
    assert streets(text) == [], f"false positive on {text!r}"


def test_foreign_hosts() -> None:
    staff = branding.DOMAINS.staff
    # The attacker's name under another reserved TLD (RFC 6761), not under .example.
    attacker = next(p for p in branding.CAST if p.role is branding.Role.ATTACKER).id
    for text in (
        f"https://{staff}.com/x",
        f"see {staff}.com",
        f"write to someone@{attacker}.test",
        f"{attacker}.test",
        f"https://{branding.DOMAINS.canary}/p?d=1",
        "http://192.0.2.1/",
    ):
        assert foreign_hosts(text), f"not detected: {text!r}"
    # The staff domain itself, as a URL, an address and a bare name, is allowed.
    for text in (f"https://{staff}/produkte/x", f"a.b@{staff}", f"{staff}.", "z.B. Nr. 3"):
        assert foreign_hosts(text) == [], f"false positive on {text!r}"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1.250.000,00 EUR", [("EUR", Decimal("1250000.00"))]),
        ("EUR 1,250,000.00", [("EUR", Decimal("1250000.00"))]),
        ("709.651,62 EUR (12,0 %)", [("EUR", Decimal("709651.62")), ("%", Decimal("12.0"))]),
        ("EUR 709,651.62 (12.0%)", [("EUR", Decimal("709651.62")), ("%", Decimal("12.0"))]),
        ("-1,6 % und +3.2%", [("%", Decimal("-1.6")), ("%", Decimal("3.2"))]),
        ("0,45 % pro Jahr", [("%", Decimal("0.45"))]),
        ("z.B. Nr. 3 am 30.10.2026, 2026-10-30, seit 2014", []),
    ],
)
def test_figure_extractor_reads_both_formats(text: str, expected: list) -> None:
    assert figures(text) == expected


GERMAN_REPORT = (
    "Positionen:\n"
    "- Liquidität: 100.000,00 EUR (10,0 %)\n"
    "- Aktien: 900.000,50 EUR (90,0 %)\n"
    "\n"
    "Gesamtvermögen: 1.000.000,50 EUR\n"
    "Wertentwicklung seit Jahresbeginn: +3,2 %\n"
)
ENGLISH_REPORT = (
    "Positions:\n- Cash: EUR 100,000.00 (10.0%)\n- Equities: EUR 900,000.50 (90.0%)\n\n"
    "Total assets: EUR 1,000,000.50\n"
)


def test_report_checks_pass_on_consistent_reports() -> None:
    # Control for the planted mismatches below.
    assert report_problems(GERMAN_REPORT, "german") == []
    assert report_problems(ENGLISH_REPORT, "english") == []
    note = "Die Aktienquote beträgt 90,0 % (900.000,50 EUR), seit Jahresbeginn +3,2 %."
    assert figure_problems(GERMAN_REPORT, note) == []


def test_planted_mismatches_are_reported() -> None:
    wrong_total = GERMAN_REPORT.replace("1.000.000,50 EUR", "1.000.001,50 EUR")
    assert report_problems(wrong_total, "german") == [
        "total 1000001.50 is not the sum of the positions 1000000.50"
    ]
    no_total = ENGLISH_REPORT.replace("Total assets", "Sum")
    assert report_problems(no_total, "english") == ["0 total lines"]
    assert figure_problems(GERMAN_REPORT, "Liquidität 100.000,01 EUR") == [
        "EUR 100000.01 is not in the report"
    ]
    assert figure_problems(GERMAN_REPORT, "Aktienquote 91,0 %") == ["% 91.0 is not in the report"]
    assert figure_problems(GERMAN_REPORT, "Keine Zahlen am 30.10.2026.") == ["no figure"]
