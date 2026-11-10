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
from typing import Any

import pytest

from app.harness.context import Provenance, Untrusted, source_text, wrap
from app.harness.grounding import CITATION_FORM, GroundingReport, check


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
    fields = {field.name: field.type for field in dataclasses.fields(GroundingReport)}
    assert fields == {
        "figures": "int",
        "unmatched": "int",
        "citations": "tuple[str, ...]",
        "unknown_citations": "int",
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
