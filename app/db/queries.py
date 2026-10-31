"""Every SQL statement the app runs, each a module-level constant (ADR 0008).

tests/test_sql_hygiene.py holds app/ to this file and to literals: `.execute()` and its
relatives take a string literal or a constant from here, values are always bound
parameters, and no statement is built from strings at runtime. The advisor context is
not here: app/db/session.py alone sets it.

Full-text search has one statement per language (ADR 0005). Each uses the two-argument
websearch_to_tsquery with its own configuration and keeps to documents in its language,
so an English query never matches a German document through a shared stem.
"""

SEARCH_DOCUMENTS_ENGLISH = (
    "SELECT d.advisor_id, d.id FROM documents d"
    " WHERE d.lang = 'english' AND d.tsv @@ websearch_to_tsquery('english', %s)"
    " ORDER BY d.advisor_id, d.id"
)

SEARCH_DOCUMENTS_GERMAN = (
    "SELECT d.advisor_id, d.id FROM documents d"
    " WHERE d.lang = 'german' AND d.tsv @@ websearch_to_tsquery('german', %s)"
    " ORDER BY d.advisor_id, d.id"
)
