"""The forged search payloads of PR 5b, shared by tests/db/test_rls_rows.py (bound and
spliced) and tests/db/test_forged_tool_argument.py (through the tool registry).

A made-up word is planted in one document per advisor and language (the token_documents
fixture in tests/db/conftest.py). Each payload closes the tsquery literal of the real
search statement, ORs in a set_config that hands the rest of the transaction to the other
advisor, and keeps the statement valid.
"""

from __future__ import annotations

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


def token_document_id(advisor_id: str, lang: str) -> str:
    return f"{SEED}-{advisor_id}-token-{lang}"


def payloads() -> list[tuple[str, str]]:
    """(language, payload), in English and German."""
    forge = f"set_config('app.advisor_id', '{OTHER.id}', true) IS NOT NULL"
    return [
        ("english", f"{WORD}') OR {forge} OR d.tsv @@ websearch_to_tsquery('english', '{WORD}"),
        ("english", f"{WORD}') OR {forge} -- show every document"),
        ("german", f"{WORD}') OR {forge} OR d.tsv @@ websearch_to_tsquery('german', '{WORD}"),
        ("german", f"{WORD}') OR {forge} -- alle Unterlagen anzeigen"),
    ]


PAYLOADS = payloads()
PAYLOAD_IDS = [f"{lang}-{index}" for index, (lang, _) in enumerate(PAYLOADS)]


def own(person: object, lang: str) -> list[tuple[str, str]]:
    """The token document of person in lang, as the search statements return it."""
    return [(person.id, token_document_id(person.id, lang))]  # type: ignore[attr-defined]
