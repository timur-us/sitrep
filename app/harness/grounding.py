"""The number guard and the citation check (threat-model path 7; M1 plan, PR 9).

check(answer, sources) reports on an answer against this request's tool results:
- every figure in the answer is looked up among the figures of the sources, which it
  reads through source_text(). Figures are amounts, percentages, dates, and numbers
  written with a separator or a scale word, in German, English and Swiss forms, so
  `1.250.000,00 EUR` matches `EUR 1,250,000.00`;
- every record id cited as `[source: id, …]` (or `[Quelle: …]`) is looked up among the
  ids the tools returned, in their Provenance.

The guard proves provenance, not truth. A figure counts as grounded when it appears in a
source, and a source can be an attacker's e-mail (threat-model path 1). The canary scan
and the output policy are M2's. The guard reports and never blocks (plan default 14);
blocking is a policy decision.

The report holds counts and the ids of cited records that the tools returned. It never
holds a figure, a source text, or an id that only the model wrote: such an id is model
output and could carry anything, so it is counted and dropped (ADR 0007, amended in M1
PR 9). A known id is listed once; an unknown one is counted at every mention, and a
marker's parts are split at commas and semicolons. The counts are the model's to steer,
so traces should not carry them raw (a handover to M1 PR 10).

What the guard does not see (stated, not hidden):
- figures written as words ("one million");
- plain integers without a separator, currency, percent sign or scale word: counts,
  years, list numbers;
- dates without a day ("March 2026"), and two-digit years;
- digits grouped with a plain space (`1 250 000`): the groups are read one by one, so
  `EUR 9 999 999` reads as `EUR 9`;
- digits other than ASCII 0-9 (full-width, Arabic-Indic), and a figure split by a
  zero-width character or a soft hyphen;
- a figure inside a citation marker, which counts as a cited id instead;
- a number of more than MAX_NUMBER characters, and a figure glued to such a run;
- which of two numbers a currency between them belongs to: in "2 EUR 9,999,999" it
  goes to the 2, and the 9,999,999 is bare;
- a bare `m`, `k` or `bn` without a currency, which is a unit, not a scale ("a 10m
  hedge").

What it does not compare:
- the sign and the direction: `-3,2 %` and "fell by 3.2%" match;
- whether a figure belongs to the client, the date or the field the answer names;
- the kind, where a source gives a plain number: a source field `3` grounds `3 %` and
  `EUR 3`, and a bare number in an answer matches a source figure of any kind;
- the order of day and month in a slashed date, which reads both ways.

A month or scale word that the patterns match but the tables do not know (a look-alike
letter) reads as no value: the figure counts as unmatched instead of disappearing. A run
over MAX_NUMBER characters does disappear: it is no figure at all.

Rounding counts against a figure: `EUR 1.3 million` does not match
`EUR 1,250,000.00`. A separator that reads both ways (`1.250`) matches either reading.
"""

from __future__ import annotations

import datetime
import decimal
import re
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.harness.context import Untrusted, source_text

# The form the system prompt asks for (app/harness/prompt.py, M1 PR 10).
CITATION_FORM = "[source: <record id>]"
_CITATION = re.compile(r"\[\s*(?:source|quelle)\s*:([^\[\]\n]{0,2000})\]", re.IGNORECASE)
_CITATION_SPLIT = re.compile(r"[,;]")


@dataclass(frozen=True, slots=True)
class GroundingReport:
    """Counts, and the ids of cited records the tools returned, in the order cited."""

    figures: int
    unmatched: int
    citations: tuple[str, ...]
    unknown_citations: int


# --- Numbers -------------------------------------------------------------------------

# Grouping and decimal separators: the comma and the point read both ways; the
# apostrophes (Swiss) and the no-break and thin spaces only group.
_GROUPING_ONLY = "'\u2019\u00a0\u202f\u2009"
_SEPARATORS = ".," + _GROUPING_ONLY
_SPLIT = re.compile(f"[{re.escape(_SEPARATORS)}]")

_CURRENCIES = {"€": "EUR", "$": "USD", "£": "GBP", "euro": "EUR", "euros": "EUR"}
_CURRENCY = r"euros?|EUR|CHF|USD|GBP|€|\$|£"
_PERCENT = r"%|prozent\w*|per\s?cent\w*"
_SCALE_WORDS = {
    "tausend": 3,
    "thousand": 3,
    "tsd": 3,
    "millionen": 6,
    "million": 6,
    "millions": 6,
    "mio": 6,
    "milliarde": 9,
    "milliarden": 9,
    "billion": 9,
    "billions": 9,
    "mrd": 9,
}
_SCALE_LETTERS = {"k": 3, "m": 6, "bn": 9}
_SCALE_WORD = "|".join(sorted(_SCALE_WORDS, key=len, reverse=True))

# At most this many characters in one number: a longer run is no figure. The cap keeps
# every scan short (review finding sec-2) and every value inside _EXACT's precision.
MAX_NUMBER = 48
_EXACT = decimal.Context(prec=2 * MAX_NUMBER)  # private: the thread's context rounds

_FIGURE = re.compile(
    rf"""
    (?<![\w.,'\u2019])
    # A number starts after a digit and a no-break or thin space only behind a
    # currency: "3\u00a0$5" is a dollar figure (review round 2, N-1).
    (?:(?P<pre>{_CURRENCY})\s?|(?<![0-9][\u00a0\u202f\u2009]))
    (?P<num>[0-9][0-9{re.escape(_SEPARATORS)}]{{0,{MAX_NUMBER - 1}}}+)
    (?:\s?(?P<word>{_SCALE_WORD})\.?(?!\w)|(?P<letter>bn|m|k)(?!\w))?
    (?:\s?(?P<post>{_CURRENCY}|{_PERCENT}))?
    (?!\w)
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _grouping(groups: list[str]) -> bool:
    """Whether groups read as digit grouping: a head of 1-3 digits without a leading
    zero, then groups of exactly 3."""
    head = groups[0]
    return 1 <= len(head) <= 3 and head[0] != "0" and all(len(g) == 3 for g in groups[1:])


def _values(raw: str) -> frozenset[Decimal]:
    """Every value the digits can be read as; empty when they read as none."""
    token = raw.rstrip(_SEPARATORS)
    separators = [char for char in token if not char.isdigit()]
    parts = _SPLIT.split(token)
    if not separators:
        return frozenset({Decimal(token)})
    last = separators[-1]
    if len(set(separators)) == 1:
        if last in _GROUPING_ONLY or len(separators) > 1:
            return frozenset({Decimal("".join(parts))}) if _grouping(parts) else frozenset()
        whole, fraction = parts
        readings = {Decimal(f"{whole}.{fraction}")}
        if _grouping(parts):
            readings.add(Decimal(whole + fraction))
        return frozenset(readings)
    # Mixed: the last separator is the decimal one, and occurs once; the rest group.
    if last in _GROUPING_ONLY or separators.count(last) != 1:
        return frozenset()
    if len(set(separators[:-1])) != 1 or not _grouping(parts[:-1]):
        return frozenset()
    return frozenset({Decimal("".join(parts[:-1]) + "." + parts[-1])})


# --- Dates ---------------------------------------------------------------------------

_MONTHS = {
    **dict.fromkeys(("january", "januar", "jan", "jän"), 1),
    **dict.fromkeys(("february", "februar", "feb"), 2),
    **dict.fromkeys(("march", "märz", "maerz", "mar", "mär", "mrz"), 3),
    **dict.fromkeys(("april", "apr"), 4),
    **dict.fromkeys(("may", "mai"), 5),
    **dict.fromkeys(("june", "juni", "jun"), 6),
    **dict.fromkeys(("july", "juli", "jul"), 7),
    **dict.fromkeys(("august", "aug"), 8),
    **dict.fromkeys(("september", "sept", "sep"), 9),
    **dict.fromkeys(("october", "oktober", "oct", "okt"), 10),
    **dict.fromkeys(("november", "nov"), 11),
    **dict.fromkeys(("december", "dezember", "dec", "dez"), 12),
}
_MONTH = "|".join(sorted(_MONTHS, key=len, reverse=True))
_MONTH_NAME = rf"(?P<month>{_MONTH})(?![a-zäöü])\.?"
_DAY_SUFFIX = r"(?:st|nd|rd|th)"

_ISO = re.compile(
    r"(?<![0-9])(?P<year>[0-9]{4})-(?P<month>[0-9]{1,2})-(?P<day>[0-9]{1,2})(?![0-9])"
)
_DOTTED = re.compile(
    r"(?<![0-9.])(?P<day>[0-9]{1,2})\.(?P<month>[0-9]{1,2})\.(?P<year>[0-9]{4})(?![0-9])"
)
_SLASHED = re.compile(
    r"(?<![0-9/])(?P<first>[0-9]{1,2})/(?P<second>[0-9]{1,2})/(?P<year>[0-9]{4})(?![0-9])"
)
_DAY_MONTH = re.compile(
    rf"(?<![0-9])(?P<day>[0-9]{{1,2}})(?:\.|{_DAY_SUFFIX})?\s+(?:of\s+)?{_MONTH_NAME},?\s+"
    r"(?P<year>[0-9]{4})(?![0-9])",
    re.IGNORECASE,
)
_MONTH_DAY = re.compile(
    rf"(?<![a-zäöü]){_MONTH_NAME}\s+(?P<day>[0-9]{{1,2}}){_DAY_SUFFIX}?,?\s+"
    r"(?P<year>[0-9]{4})(?![0-9])",
    re.IGNORECASE,
)


def _date(year: str, month: str | int, day: str) -> datetime.date | None:
    try:
        return datetime.date(int(year), int(month), int(day))
    except ValueError:
        return None


def _fold(word: str) -> str:
    """word as the tables spell it. The patterns match case-insensitively by Unicode
    rules, so a long s or a dotless i matches too; casefold() and NFKC bring most such
    words back, and a word still unknown reads as no value (review finding sec-1)."""
    return unicodedata.normalize("NFKC", word).casefold()


def _named(match: re.Match[str]) -> Iterable[datetime.date | None]:
    month = _MONTHS.get(_fold(match["month"]))
    yield None if month is None else _date(match["year"], month, match["day"])


def _numeric(match: re.Match[str]) -> Iterable[datetime.date | None]:
    yield _date(match["year"], match["month"], match["day"])


def _slashed(match: re.Match[str]) -> Iterable[datetime.date | None]:
    # Day first (Europe) or month first (US): either reading.
    yield _date(match["year"], match["second"], match["first"])
    yield _date(match["year"], match["first"], match["second"])


_DATES: tuple[tuple[re.Pattern[str], Callable[[re.Match[str]], Iterable[Any]]], ...] = (
    (_ISO, _numeric),
    (_DOTTED, _numeric),
    (_SLASHED, _slashed),
    (_DAY_MONTH, _named),
    (_MONTH_DAY, _named),
)


# --- Extraction and matching ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Figure:
    kind: str  # "money", "percent", "number" or "date"
    currency: str = ""
    numbers: frozenset[Decimal] = frozenset()
    dates: frozenset[datetime.date] = frozenset()


@dataclass(slots=True)
class _Index:
    """The figures of the sources, by what an answer's figure may match."""

    money: set[tuple[str, Decimal]] = field(default_factory=set)
    percent: set[Decimal] = field(default_factory=set)
    bare: set[Decimal] = field(default_factory=set)
    numeric: set[Decimal] = field(default_factory=set)
    dates: set[datetime.date] = field(default_factory=set)

    def add(self, figure: _Figure) -> None:
        if figure.kind == "date":
            self.dates |= figure.dates
            return
        self.numeric |= figure.numbers
        if figure.kind == "money":
            self.money |= {(figure.currency, value) for value in figure.numbers}
        elif figure.kind == "percent":
            self.percent |= figure.numbers
        else:
            self.bare |= figure.numbers

    def grounds(self, figure: _Figure) -> bool:
        if figure.kind == "date":
            return not figure.dates.isdisjoint(self.dates)
        if figure.kind == "money":
            return any(
                (figure.currency, value) in self.money or value in self.bare
                for value in figure.numbers
            )
        if figure.kind == "percent":
            return any(value in self.percent or value in self.bare for value in figure.numbers)
        return not figure.numbers.isdisjoint(self.numeric)


def _blank(text: str, pattern: re.Pattern[str], found: list[re.Match[str]]) -> str:
    """text with every match of pattern replaced by spaces, the matches kept in found."""

    def keep(match: re.Match[str]) -> str:
        found.append(match)
        return " " * len(match.group())

    return pattern.sub(keep, text)


def _figures(text: str, *, plain: bool) -> list[_Figure]:
    """The figures in text. plain: a bare integer counts too (the sources' side)."""
    figures: list[_Figure] = []
    for pattern, read in _DATES:
        matches: list[re.Match[str]] = []
        text = _blank(text, pattern, matches)
        for match in matches:
            dates = frozenset(day for day in read(match) if day is not None)
            figures.append(_Figure("date", dates=dates))
    for match in _FIGURE.finditer(text):
        figure = _number(match, plain=plain)
        if figure is not None:
            figures.append(figure)
    return figures


def _number(match: re.Match[str], *, plain: bool) -> _Figure | None:
    raw = match["num"].rstrip(_SEPARATORS)
    post = match["post"] or ""
    percent = bool(post) and not re.fullmatch(_CURRENCY, post, re.IGNORECASE)
    currency = match["pre"] or ("" if percent else post)
    exponent: int | None = 0
    if match["word"]:
        exponent = _SCALE_WORDS.get(_fold(match["word"]).rstrip("."))
    elif match["letter"] and currency:
        # A bare letter is a scale beside a currency only: "10m" alone is ten metres.
        exponent = _SCALE_LETTERS.get(_fold(match["letter"]))
    # An unknown scale word (a folded look-alike) reads as no value, so the figure counts
    # as unmatched instead of disappearing (review finding sec-1).
    values = (
        frozenset(value.scaleb(exponent, _EXACT) for value in _values(raw))
        if exponent is not None
        else frozenset()
    )
    if percent:
        return _Figure("percent", numbers=values)
    if currency:
        code = _CURRENCIES.get(_fold(currency), currency.upper())
        return _Figure("money", currency=code, numbers=values)
    if exponent is None:
        return _Figure("number")
    # A bare number is a figure when it has a separator or a scale; an unreadable one
    # (1.2.3) is not. The sources' side keeps plain integers, which an answer's
    # "EUR 1,250,000" may come from.
    if not values or not (plain or exponent or not raw.isdigit()):
        return None
    return _Figure("number", numbers=values)


def check(answer: str, sources: Iterable[Untrusted[Any]]) -> GroundingReport:
    """The grounding of answer against this request's tool results; see the module."""
    if type(answer) is not str:
        raise TypeError(f"check() takes the answer as a str, not {type(answer).__name__}")
    sources = tuple(sources)  # read three times below (review finding sec-5)
    for item in sources:
        if type(item) is not Untrusted:
            raise TypeError(f"check() takes Untrusted sources, not {type(item).__name__}")

    seen = {record_id for item in sources for record_id in item.provenance.record_ids}
    cited: list[str] = []
    unknown = 0
    markers: list[re.Match[str]] = []
    text = _blank(answer, _CITATION, markers)
    for marker in markers:
        for part in _CITATION_SPLIT.split(marker.group(1)):
            record_id = part.strip()
            if not record_id:
                continue
            if record_id not in seen:
                unknown += 1
            elif record_id not in cited:
                cited.append(record_id)

    index = _Index()
    for item in sources:
        for figure in _figures(source_text(item), plain=True):
            index.add(figure)
    figures = _figures(text, plain=False)
    unmatched = sum(1 for figure in figures if not index.grounds(figure))
    return GroundingReport(
        figures=len(figures),
        unmatched=unmatched,
        citations=tuple(cited),
        unknown_citations=unknown,
    )
