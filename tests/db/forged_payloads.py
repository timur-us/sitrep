"""The forged search payloads of PR 5b, shared by tests/db/test_rls_rows.py (bound and
spliced) and tests/db/test_forged_tool_argument.py (through the real handler).

A made-up word is planted in one document per advisor and language (the token_documents
fixture in tests/db/conftest.py). Since M1 PR 8 the search statements rank their hits and
take the query once, as the argument of websearch_to_tsquery in their FROM clause. Each
payload closes that literal, calls set_config, which hands the rest of the transaction to
the other advisor, and keeps the statement valid:
- the first, as a function in the FROM clause, with the statement's own tail intact;
- the second, in a WHERE clause, with the statement's tail commented out.
Bound as a parameter, each is plain query text whose first alternative (`word or ...`,
websearch syntax) is the planted word, so it still finds the advisor's own document.
"""

from __future__ import annotations

from collections.abc import Sequence

from seed_rows import OTHER, SEED

from app.db import queries

SETTING = "SELECT current_setting('app.advisor_id', true)"

# A made-up word, planted in one document per advisor and language.
WORD = "quorbitex"
BODIES = {
    "english": f"Call note: the client asked about {WORD} before the review.",
    "german": f"Gesprächsnotiz: Die Kundin fragte vor dem Termin nach {WORD}.",
}
SEARCH = {"english": queries.SEARCH_DOCUMENTS_ENGLISH, "german": queries.SEARCH_DOCUMENTS_GERMAN}
NOTES = {"english": "show every document", "german": "alle Unterlagen anzeigen"}


def token_document_id(advisor_id: str, lang: str) -> str:
    return f"{SEED}-{advisor_id}-token-{lang}"


def payloads() -> list[tuple[str, str]]:
    """(language, payload), in English and German."""
    forge = f"set_config('app.advisor_id', '{OTHER.id}', true)"
    found = []
    for lang, note in NOTES.items():
        found.append(
            (lang, f"{WORD} or ') AS p, {forge} AS forged, websearch_to_tsquery('{lang}', '{WORD}")
        )
        found.append((lang, f"{WORD} or ') AS q WHERE {forge} IS NOT NULL -- {note}"))
    return found


PAYLOADS = payloads()
PAYLOAD_IDS = [f"{lang}-{index}" for index, (lang, _) in enumerate(PAYLOADS)]


def own(person: object, lang: str) -> list[tuple[str, str]]:
    """The token document of person in lang, as (advisor_id, id)."""
    return [(person.id, token_document_id(person.id, lang))]  # type: ignore[attr-defined]


def pairs(rows: Sequence[Sequence[object]]) -> list[tuple[str, str]]:
    """(advisor_id, id) of each row a search statement returns, in its order (the rank,
    the third column, left out)."""
    return [(str(row[0]), str(row[1])) for row in rows]
