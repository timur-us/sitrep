"""The number guard and the citation check (M1 plan, PR 9; threat-model path 7).

check(answer, sources) finds the figures in an answer (amounts, percentages, dates, in
German and English forms) and looks each one up among the figures of this request's tool
results, read through source_text(). It also checks every cited record id against the ids
those results carry. The report holds counts and seen ids only; it reports, it never
blocks (plan default 14).
"""

from __future__ import annotations

import dataclasses
import datetime
import decimal
import random
import time
import typing
from typing import Any

import pytest

from app.harness.context import Provenance, Untrusted, source_text, wrap
from app.harness.grounding import CITATION_FORM, GroundingReport, check
from data.synthetic import generate


def source(value: object, *record_ids: str, tool: str = "search_client_docs") -> Untrusted[Any]:
    return Untrusted(value, Provenance(tool=tool, record_ids=record_ids or ("doc-1",)))


def report(answer: str, *sources: Untrusted[Any]) -> GroundingReport:
    return check(answer, sources)


# The generator's own forms (data/synthetic/generate.py: money, percent, long_date).
GERMAN_SOURCE = "Gesamtvermögen 1.250.000,00 EUR, Aktienquote 42,5 %, Stand 05.03.2026."
ENGLISH_SOURCE = "Total assets EUR 1,250,000.00, equity share 42.5%, as of 5 March 2026."


# --- The plan's tests ----------------------------------------------------------------


def test_a_figure_from_the_sources_passes() -> None:
    result = report("The client holds EUR 1,250,000.00.", source(ENGLISH_SOURCE))
    assert (result.figures, result.unmatched) == (1, 0)


def test_a_figure_from_nowhere_is_counted() -> None:
    result = report("The client holds EUR 1,250,000.00 and EUR 75,000.00.", source(ENGLISH_SOURCE))
    assert (result.figures, result.unmatched) == (2, 1)


def test_a_german_amount_matches_its_english_form() -> None:
    assert report("EUR 1,250,000.00", source(GERMAN_SOURCE)).unmatched == 0
    assert report("1.250.000,00 EUR", source(ENGLISH_SOURCE)).unmatched == 0
    assert report("1.250.000,00 EUR", source(GERMAN_SOURCE)).figures == 1


def test_a_citation_of_an_unseen_id_is_counted() -> None:
    result = report(
        "Fine [source: doc-1]. Made up [source: doc-404].",
        source(ENGLISH_SOURCE, "doc-1", "doc-2"),
    )
    assert result.unknown_citations == 1
    assert result.citations == ("doc-1",)


def test_a_figure_only_in_an_attackers_mail_still_counts_as_grounded() -> None:
    """The guard proves provenance, not truth: a figure an attacker planted in a mail is
    in the sources, so it passes. The canary scan and the output policy are M2's."""
    planted = source("Please confirm the transfer of EUR 9,999,999.00 today.", "rt-mail-1")
    result = report("The client asked to transfer EUR 9,999,999.00.", planted)
    assert (result.figures, result.unmatched) == (1, 0)


def test_the_docstring_says_provenance_not_truth() -> None:
    from app.harness import grounding

    doc = " ".join((grounding.__doc__ or "").split())
    assert "provenance, not truth" in doc


# --- Figures -------------------------------------------------------------------------

MATCHES = {
    # Amounts.
    "en amount, no cents": ("EUR 1,250,000", ENGLISH_SOURCE),
    "de amount, no cents": ("1.250.000 EUR", ENGLISH_SOURCE),
    "euro sign": ("€1,250,000.00", GERMAN_SOURCE),
    "euro sign after": ("1.250.000,00 €", ENGLISH_SOURCE),
    "swiss grouping": ("EUR 1'250'000.00", GERMAN_SOURCE),
    "million": ("EUR 1.25 million", ENGLISH_SOURCE),
    "mio": ("1,25 Mio. EUR", GERMAN_SOURCE),
    "attached m": ("€1.25m", ENGLISH_SOURCE),
    "bare grouped number": ("assets of 1,250,000", ENGLISH_SOURCE),
    # Percentages: the sign is not compared.
    "en percent": ("an equity share of 42.5%", GERMAN_SOURCE),
    "de percent": ("eine Aktienquote von 42,5 %", ENGLISH_SOURCE),
    "prozent": ("42,5 Prozent", ENGLISH_SOURCE),
    "percent word": ("42.5 percent", GERMAN_SOURCE),
    "signed source": ("fell by 3.2%", "Wertentwicklung -3,2 %"),
    # Dates.
    "de date": ("am 05.03.2026", ENGLISH_SOURCE),
    "de date, short": ("am 5.3.2026", ENGLISH_SOURCE),
    "de long date": ("am 5. März 2026", ENGLISH_SOURCE),
    "en date": ("on 5 March 2026", GERMAN_SOURCE),
    "en date, month first": ("on March 5, 2026", GERMAN_SOURCE),
    "en date, abbreviated": ("on 5th Mar 2026", GERMAN_SOURCE),
    "iso date": ("on 2026-03-05", GERMAN_SOURCE),
}


@pytest.mark.parametrize(("answer", "text"), list(MATCHES.values()), ids=list(MATCHES))
def test_forms_that_match(answer: str, text: str) -> None:
    result = report(answer, source(text))
    assert (result.figures, result.unmatched) == (1, 0), result


MISSES = {
    "another amount": ("EUR 1,350,000.00", ENGLISH_SOURCE),
    "another currency": ("CHF 1,250,000.00", ENGLISH_SOURCE),
    "rounded": ("EUR 1.3 million", ENGLISH_SOURCE),
    "another percent": ("42.6%", ENGLISH_SOURCE),
    "an amount is not a percent": ("1,250,000%", ENGLISH_SOURCE),
    "another day": ("on 6 March 2026", ENGLISH_SOURCE),
    "day and month swapped": ("am 03.05.2026", ENGLISH_SOURCE),
    "an impossible date": ("am 31.02.2026", ENGLISH_SOURCE),
    "a bare decimal": ("version 7.25", ENGLISH_SOURCE),
    "a malformed amount": ("EUR 1,25,0000", ENGLISH_SOURCE),
}


@pytest.mark.parametrize(("answer", "text"), list(MISSES.values()), ids=list(MISSES))
def test_forms_that_do_not_match(answer: str, text: str) -> None:
    result = report(answer, source(text))
    assert (result.figures, result.unmatched) == (1, 1), result


NOT_FIGURES = {
    "a count": "There are 3 call notes and 12 mails.",
    "a year": "The mandate dates from 2019.",
    "a record id": "See family-note-2 and product-7.",
    "a time": "The call is at 10:30.",
    "an ordinal": "the 2nd review",
    "a phone number": "+41 44 668 18 00",
    "a malformed bare number": "steps 1.2.3",
}


@pytest.mark.parametrize("answer", list(NOT_FIGURES.values()), ids=list(NOT_FIGURES))
def test_what_is_not_a_figure(answer: str) -> None:
    assert report(answer, source(ENGLISH_SOURCE)).figures == 0


def test_an_ambiguous_separator_matches_either_reading() -> None:
    # "1.250" is 1250 in German and 1.25 in English.
    assert report("EUR 1.250", source("EUR 1,250.00")).unmatched == 0
    assert report("EUR 1.250", source("1,25 EUR")).unmatched == 0
    assert report("EUR 1.250", source("EUR 12.50")).unmatched == 1


def test_a_currency_from_the_answer_meets_a_bare_source_number() -> None:
    assert report("EUR 1,250,000", source({"total": 1250000})).unmatched == 0


def test_each_figure_counts_once_per_mention() -> None:
    result = report("EUR 1,250,000.00 and again EUR 1,250,000.00 and 9.9%", source(ENGLISH_SOURCE))
    assert (result.figures, result.unmatched) == (3, 1)


def test_figures_inside_a_citation_are_not_figures() -> None:
    result = report("Noted [source: note-2026-03-05, doc-1].", source(ENGLISH_SOURCE, "doc-1"))
    assert result.figures == 0


def test_structured_results_are_read_through_their_fields() -> None:
    @dataclasses.dataclass(frozen=True)
    class Portfolio:
        as_of: datetime.date
        report: str

    value = Portfolio(as_of=datetime.date(2026, 3, 5), report="Liquidität\n12,5 %")
    result = report("As of 5 March 2026, cash was 12.5%.", source(value, tool="get_portfolio"))
    assert (result.figures, result.unmatched) == (2, 0)


def test_a_figure_glued_to_a_newline_in_json_still_counts() -> None:
    # As JSON the report reads "...\n12,5 %": an escape would glue the figure to "n".
    hits = [{"excerpt": "Aktien\n42,5 %"}, {"excerpt": "Cash\nEUR 50,000.00"}]
    result = report("42.5% and EUR 50,000.00", source(hits))
    assert (result.figures, result.unmatched) == (2, 0)


def test_the_sources_are_all_of_this_requests_results() -> None:
    first = source("EUR 1,250,000.00", "doc-1")
    second = source("42,5 %", "doc-2", tool="get_portfolio")
    result = report("EUR 1,250,000.00 and 42.5% [source: doc-1, doc-2]", first, second)
    assert (result.figures, result.unmatched, result.unknown_citations) == (2, 0, 0)
    assert result.citations == ("doc-1", "doc-2")


def test_no_sources_means_every_figure_is_unmatched() -> None:
    result = report("EUR 1,250,000.00 on 5 March 2026 [source: doc-1]")
    assert (result.figures, result.unmatched, result.unknown_citations) == (2, 2, 1)


# --- Citations -----------------------------------------------------------------------


def test_citations_are_listed_once_in_the_order_cited() -> None:
    sources = source(ENGLISH_SOURCE, "doc-1", "doc-2", "doc-3")
    result = report("a [source: doc-3] b [source: doc-1; doc-3] c [Source: doc-1]", sources)
    assert result.citations == ("doc-3", "doc-1")
    assert result.unknown_citations == 0


def test_the_german_marker_is_read_too() -> None:
    result = report("Laut Notiz [Quelle: doc-2].", source(ENGLISH_SOURCE, "doc-1", "doc-2"))
    assert result.citations == ("doc-2",)


def test_every_unknown_mention_is_counted() -> None:
    result = report("[source: x-1] [source: x-1, x-2] [source: doc-1]", source("", "doc-1"))
    assert result.unknown_citations == 3
    assert result.citations == ("doc-1",)


def test_ids_compare_exactly() -> None:
    result = report("[source: DOC-1] [source: doc-1 ]", source("", "doc-1"))
    assert result.citations == ("doc-1",)
    assert result.unknown_citations == 1


def test_the_citation_form_is_one_the_check_reads() -> None:
    answer = CITATION_FORM.replace("<record id>", "doc-1")
    assert report(answer, source("", "doc-1")).citations == ("doc-1",)


# --- The report holds counts and seen ids, never figures or model text ---------------


def test_the_report_carries_no_figure_and_no_model_written_id() -> None:
    invented = "cnry-id-7f3a"
    result = report(
        f"EUR 4,321,987.65 and 13.7% on 9 April 2026 [source: {invented}] [source: doc-1]",
        source(ENGLISH_SOURCE, "doc-1"),
    )
    shown = repr(result) + str(dataclasses.asdict(result))
    for text in ("4,321,987.65", "4321987.65", "13.7", "2026", invented):
        assert text not in shown, text
    assert result.citations == ("doc-1",)


def test_the_report_fields_are_counts_and_ids() -> None:
    # The resolved types, not the annotation strings (review finding test-10).
    assert typing.get_type_hints(GroundingReport) == {
        "figures": int,
        "unmatched": int,
        "citations": tuple[str, ...],
        "unknown_citations": int,
    }
    result = report("", source(""))
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.figures = 1  # type: ignore[misc]


def test_check_takes_untrusted_sources_only() -> None:
    with pytest.raises(TypeError):
        check("EUR 1,250,000.00", [ENGLISH_SOURCE])  # type: ignore[list-item]


# --- source_text(): the guard reads the result, not wrap()'s rendering ---------------


def test_source_text_is_not_escaped() -> None:
    item = source("R&D <5 % > 2026")
    assert source_text(item) == "R&D <5 % > 2026"
    assert "&amp;" in wrap(item)


def test_source_text_reads_the_leaves_of_a_structure() -> None:
    value = {"title": "Bericht", "rows": [{"amount": 1250000, "date": datetime.date(2026, 3, 5)}]}
    lines = source_text(source(value)).splitlines()
    assert lines == ["Bericht", "1250000", "2026-03-05"]


def test_source_text_takes_an_untrusted_only() -> None:
    with pytest.raises(TypeError):
        source_text("EUR 1,250,000.00")  # type: ignore[arg-type]


# --- The generator's forms, both ways, and the cost ----------------------------------

FORMATTERS = {
    "money": lambda n, lang: generate.money(n, lang),
    "whole euros": lambda n, lang: generate.whole_euros(n, lang),
    "percent": lambda n, lang: generate.percent(n, lang),
    "signed percent": lambda n, lang: generate.percent(n - 500, lang, signed=True),
    "basis points": lambda n, lang: generate.basis_points(n, lang),
    "long date": lambda n, lang: generate.long_date(
        datetime.date(2020, 1, 1) + datetime.timedelta(days=n % 3000), lang
    ),
}


@pytest.mark.parametrize("form", list(FORMATTERS.values()), ids=list(FORMATTERS))
def test_every_generated_form_grounds_its_other_language(form: Any) -> None:
    rng = random.Random(9)  # noqa: S311 - a fixed sample of numbers, not a secret
    numbers = [0, 1, 999, 1000, 1001, 100_000, 125_000_000] + [
        rng.randrange(10**10) for _ in range(200)
    ]
    other = {"german": "english", "english": "german"}
    for number in numbers:
        for lang in generate.LANGS:
            text = form(number, lang)
            result = report(text, source(form(number, other[lang])))
            assert (result.figures, result.unmatched) == (1, 0), (text, result)


def test_another_generated_amount_does_not_ground() -> None:
    for lang in generate.LANGS:
        answer = generate.money(125_000_000, lang)
        assert report(answer, source(generate.money(125_000_100, lang))).unmatched == 1


def best_of_three(text: str) -> float:
    times = []
    for _ in range(3):
        started = time.perf_counter()
        result = check(text, [source(text)])
        times.append(time.perf_counter() - started)
        assert isinstance(result, GroundingReport)
    return min(times)


# (text for n, n): n is sized so that the small run takes some 20 ms or more, which
# keeps the ratio out of timer noise (review round 2).
HOSTILE = {
    # Review finding sec-2: no-break spaces between digits restarted the scan at every
    # digit, which made the cost quadratic.
    "no-break spaces": (lambda n: "1\u00a0" * n + "x", 20_000),
    "thin spaces": (lambda n: "1\u2009" * n + "x", 20_000),
    "narrow no-break spaces": (lambda n: "1\u202f" * n + "x", 20_000),
    "apostrophes": (lambda n: "1'" * n + "x", 20_000),
    "points": (lambda n: "1." * n + "x", 20_000),
    "unclosed markers": (lambda n: "[source:" * n + " 1,0" * n, 5_000),
    "months": (lambda n: " 5 März" * n, 5_000),
    "currencies": (lambda n: "EUR " * n + "5", 20_000),
}


@pytest.mark.parametrize(("build", "n"), list(HOSTILE.values()), ids=list(HOSTILE))
def test_hostile_text_is_checked_in_linear_time(build: Any, n: int) -> None:
    small, large = best_of_three(build(n)), best_of_three(build(4 * n))
    # Four times the text: about four times the time when linear, sixteen when quadratic.
    assert large < small * 9, (small, large)


# --- Review round 1 (findings test-4 to test-14, sec-1, sec-5, sec-6) ----------------

MORE_MATCHES = {
    # test-4: the other currencies and scales.
    "dollar sign": ("$1,250,000.00", "USD 1,250,000.00"),
    "pound sign": ("£1,250,000", "GBP 1,250,000.00"),
    "euro word": ("1.250.000 Euro", "EUR 1,250,000.00"),
    "euros word": ("1,250,000 euros", "1.250.000,00 EUR"),
    "mrd": ("1,2 Mrd. EUR", "EUR 1,200,000,000.00"),
    "milliarden": ("1,2 Milliarden EUR", "EUR 1,200,000,000.00"),
    "billion": ("EUR 1.2 billion", "1.200.000.000,00 EUR"),
    "bn": ("€1.2bn", "1.200.000.000,00 EUR"),
    "tausend": ("50 Tausend EUR", "EUR 50,000.00"),
    "tsd": ("50 Tsd. EUR", "EUR 50,000.00"),
    "thousand": ("EUR 50 thousand", "EUR 50,000.00"),
    "k": ("€50k", "EUR 50,000.00"),
    "scale on an ambiguous point": ("1.250 Mio. EUR", "EUR 1,250,000,000.00"),
    # test-7: separators.
    "comma grouping": ("EUR 12,345", "12.345,00 EUR"),
    "point grouping": ("100.000 EUR", "EUR 100,000.00"),
    # test-8: slashed dates, both readings, and a timestamp in a source.
    "slashed, day first": ("on 5/3/2026", "5 March 2026"),
    "slashed, month first": ("on 03/05/2026", "5 March 2026"),
    "iso timestamp in a source": ("on 5 March 2026", "2026-03-05T10:00:00Z"),
    # sec-1: a long s folds back to s, so the month reads as itself.
    "a folded month": ("am 5 \u017fep 2026", "5 September 2026"),
    # Review round 2: the Kelvin sign folds to k under NFKC.
    "a kelvin sign in a month": ("am 5 O\u212aT 2026", "5 October 2026"),
}


@pytest.mark.parametrize(("answer", "text"), list(MORE_MATCHES.values()), ids=list(MORE_MATCHES))
def test_more_forms_that_match(answer: str, text: str) -> None:
    result = report(answer, source(text))
    assert (result.figures, result.unmatched) == (1, 0), result


MORE_MISSES = {
    # test-5: one kind does not stand for another.
    "money for a percent": ("EUR 42.5", "42,5 %"),
    "a percent for money": ("42.5%", "EUR 42.50"),
    "dollars for euros": ("$1,250,000.00", "EUR 1,250,000.00"),
    "euros for francs": ("EUR 1,250,000.00", "CHF 1,250,000.00"),
    # test-4: the exponent matters.
    "mrd is not mio": ("1,2 Mrd. EUR", "EUR 1,200,000.00"),
    # test-7: readings that do not exist.
    "a decimal comma is not grouping": ("EUR 1,5", "EUR 15.00"),
    "a leading zero is not grouping": ("EUR 0.250", "EUR 250.00"),
    "a mixed malformed amount": ("EUR 1,250.000,5", "EUR 1,250,000.50"),
    # test-8 and sec-1: no reading, still a figure.
    "an impossible slashed date": ("on 13/13/2026", "5 March 2026"),
    "a folded scale": ("1,2 M\u0131o. EUR", "EUR 1,200,000.00"),
    "a folded scale with a dot": ("1,2 M\u0130o. EUR", "EUR 1,200,000.00"),
    # Review round 2: mutants that survived round 1.
    "a dotless i in a month": ("am 5 Apr\u0131l 2026", "5 April 2026"),
    "a folded scale without a currency": ("1,2 M\u0131o.", "1.200.000"),
    # Review round 2, N-1: a digit and a no-break space before a currency sign.
    "a currency after a spaced digit": ("Konto 3\u00a0$5,000,000", "EUR 5,000,000.00"),
}


@pytest.mark.parametrize(("answer", "text"), list(MORE_MISSES.values()), ids=list(MORE_MISSES))
def test_more_forms_that_do_not_match(answer: str, text: str) -> None:
    result = report(answer, source(text))
    assert (result.figures, result.unmatched) == (1, 1), result


MORE_NOT_FIGURES = {
    # test-6: a unit letter without a currency is not a scale.
    "metres": "a 10m hedge",
    "a count in thousands": "3k followers",
    "a two-digit year": "am 5.3.26",
    "month and year": "in March 2026",
    "an iban": "CH93 0076 2011 6238 5295 7",
}


@pytest.mark.parametrize("answer", list(MORE_NOT_FIGURES.values()), ids=list(MORE_NOT_FIGURES))
def test_more_of_what_is_not_a_figure(answer: str) -> None:
    assert report(answer, source(ENGLISH_SOURCE)).figures == 0


FOLDED = (
    "am 5 \u017fep 2026",
    "1,2 M\u0131o. EUR",
    "5 tau\u017fend EUR",
    "1,2 M\u0130o. EUR",
    "am 5 Apr\u0131l 2026",
    "1,2 M\u0131o.",
)


@pytest.mark.parametrize("text", FOLDED)
def test_a_folded_word_in_a_source_does_not_raise(text: str) -> None:
    # sec-1: a planted mail with such a word broke every check() that retrieved it.
    result = report("EUR 1,250,000.00", source(text), source(ENGLISH_SOURCE))
    assert (result.figures, result.unmatched) == (1, 0)


def test_check_never_raises_on_odd_text() -> None:
    # sec-1: any str answer and any str source give a report.
    pieces = [
        "5",
        "1.250",
        ",",
        ".",
        "'",
        "\u00a0",
        " ",
        "EUR",
        "€",
        "%",
        "Mio.",
        "m",
        "k",
        "\u017f",
        "\u0131",
        "\u0130",
        "\u212a",
        "ep",
        "ept",
        "März",
        "\u0307",
        "2026",
        "5 ",
        " 2026",
        "\u017fep",
        "M\u0131o",
        "tau\u017fend",
        "M\u0130o",
        "-",
        "/",
        "[source:",
        "]",
        "\n",
        "Prozent",
        "Mrd",
        "bn",
        "0",
        "9" * 50,
        "\u200b",
    ]
    rng = random.Random(7)  # noqa: S311 - a fixed sample of texts, not a secret
    for _ in range(2_000):
        text = "".join(rng.choice(pieces) for _ in range(rng.randrange(1, 30)))
        result = check(text, [source(text)])
        assert result.unmatched <= result.figures


def test_a_long_figure_is_compared_exactly() -> None:
    # sec-7: no rounding to the decimal context's 28 digits.
    long_a = "EUR 1" + ",000" * 9 + ".000000001"
    long_b = "EUR 1" + ",000" * 9 + ".000000002"
    assert report(long_a, source(long_b)).unmatched == 1
    assert report(long_a, source(long_a)).unmatched == 0


def test_sources_may_be_any_iterable() -> None:
    # sec-5: a generator was read empty after the first pass.
    one = source(ENGLISH_SOURCE, "doc-1")
    result = check("EUR 1,250,000.00 [source: doc-1]", (item for item in [one]))
    assert (result.unmatched, result.citations, result.unknown_citations) == (0, ("doc-1",), 0)


def test_check_takes_a_str_answer_only() -> None:
    # test-11.
    for answer in (b"EUR 5", None, 5):
        with pytest.raises(TypeError, match="answer as a str"):
            check(answer, [])  # type: ignore[arg-type]


class LooksUntrusted:
    provenance = Provenance(tool="search_client_docs", record_ids=("doc-1",))
    _value = ENGLISH_SOURCE


def test_a_look_alike_source_is_refused() -> None:
    with pytest.raises(TypeError):
        check("EUR 1,250,000.00", [LooksUntrusted()])  # type: ignore[list-item]


def test_a_model_written_figure_or_canary_id_never_reaches_the_report() -> None:
    # test-10: an invented id that is itself a figure, or a canary.
    answer = "[source: EUR 9,999,999.00] [source: cnry-7f3a-exfil] [source: doc-1]"
    result = report(answer, source(ENGLISH_SOURCE, "doc-1"))
    # The parts of a marker are split at commas: "EUR 9", "999", "999.00" and the canary.
    assert (result.figures, result.unknown_citations, result.citations) == (0, 4, ("doc-1",))
    shown = repr(result) + str(dataclasses.asdict(result))
    for text in ("9,999,999", "9999999", "cnry", "exfil"):
        assert text not in shown, text


MARKERS = {
    # test-9: (answer, unknown citations, cited ids, figures)
    "empty": ("[source: ] [source:,;]", 0, (), 0),
    "unterminated": ("[source: doc-1 and EUR 75,000.00", 0, (), 1),
    "too long": ("[source: " + "x" * 2_001 + "]", 0, (), 0),
    "space before the colon": ("[Source : doc-1]", 0, ("doc-1",), 0),
    "spaces inside": ("[ source:   doc-1 ,  doc-1 ]", 0, ("doc-1",), 0),
    "a newline inside": ("[source: doc-1\n]", 0, (), 0),
    "nested is no marker": ("[source: [doc-1]]", 0, (), 0),
    "a zero-width character": ("[source: doc\u200b-1]", 1, (), 0),
}


@pytest.mark.parametrize(
    ("answer", "unknown", "cited", "figures"), list(MARKERS.values()), ids=list(MARKERS)
)
def test_marker_shapes(answer: str, unknown: int, cited: tuple[str, ...], figures: int) -> None:
    result = report(answer, source("EUR 75,000.00", "doc-1"))
    assert (result.unknown_citations, result.citations, result.figures) == (unknown, cited, figures)


def test_a_source_without_record_ids_grounds_figures_but_no_citation() -> None:
    bare = Untrusted("EUR 1,250,000.00", Provenance(tool="get_portfolio", record_ids=()))
    result = check("EUR 1,250,000.00 [source: doc-1]", [bare])
    assert (result.unmatched, result.unknown_citations) == (0, 1)


def test_a_generated_amount_has_its_literal_value() -> None:
    # test-13: the round trip, pinned to literals as well.
    assert generate.money(125_000_000, "german") == "1.250.000,00 EUR"
    assert report("EUR 1,250,000.00", source(generate.money(125_000_000, "german"))).unmatched == 0
    assert report("EUR 1,250,000.01", source(generate.money(125_000_000, "german"))).unmatched == 1


LEAVES = {
    # test-12: (value, lines)
    "floats in fixed point": (
        [1250000.0, 1.25e16, 1e-07, 1.5],
        ["1250000.0", "12500000000000000", "0.0000001", "1.5"],
    ),
    "no bool, no None": ({"a": True, "b": None, "c": "x"}, ["x"]),
    "decimal": ([decimal.Decimal("1.50")], ["1.50"]),
    "a tuple, in order": (("a", "b", "c", "d"), ["a", "b", "c", "d"]),
    "nan and infinity": ([float("nan"), float("inf")], []),
    "keys are left out": ({"EUR 5,000.00": "x"}, ["x"]),
    "deep": ([[[[["x"]]]]], ["x"]),
}


@pytest.mark.parametrize(("value", "lines"), list(LEAVES.values()), ids=list(LEAVES))
def test_source_text_leaves(value: object, lines: list[str]) -> None:
    text = source_text(source(value))
    assert (text.splitlines() if text else []) == lines
