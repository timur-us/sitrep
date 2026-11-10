"""The statements the app's routes and the loader run, each a module-level constant
(ADR 0008).

Two kinds of SQL live elsewhere: the pool guard and the advisor context are string
literals in app/db/session.py, the one file that may name the advisor setting, and
app/db/migrate.py composes its own DDL from app/db/names.py. tests/test_sql_hygiene.py
holds the rest of app/ to this file and to literals: `.execute()` and its relatives take
a string literal or a constant from here, values are always bound parameters, and no
statement is built from strings at runtime.

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

# The signed-in advisor's mandates (app/api/mandates.py). No advisor in the statement:
# row-level security scopes it to the advisor of the transaction.
LIST_MANDATES = "SELECT m.id, m.family FROM mandates m ORDER BY m.id"

# The loader (app/db/load.py; ADR 0010), as the owner, one advisor per transaction under
# owner_advisor_scope(): row-level security scopes each statement to that advisor, and
# the bound advisor_id says the same again. No statement names trust, which the
# database generates from origin, and none truncates (TRUNCATE ignores row-level
# security). Children are deleted before their parents; the advisor row is upserted, so
# rows that hang off it elsewhere stay.
LOAD_UPSERT_ADVISOR = (
    "INSERT INTO advisors (advisor_id, display_name) VALUES (%s, %s)"
    " ON CONFLICT (advisor_id) DO UPDATE SET display_name = EXCLUDED.display_name"
)
LOAD_DELETE_CHUNKS = "DELETE FROM chunks WHERE advisor_id = %s"
LOAD_DELETE_DOCUMENTS = "DELETE FROM documents WHERE advisor_id = %s"
LOAD_DELETE_MANDATE_CONTACTS = "DELETE FROM mandate_contacts WHERE advisor_id = %s"
LOAD_DELETE_MANDATES = "DELETE FROM mandates WHERE advisor_id = %s"
LOAD_INSERT_MANDATE = "INSERT INTO mandates (advisor_id, id, family) VALUES (%s, %s, %s)"
LOAD_INSERT_MANDATE_CONTACT = (
    "INSERT INTO mandate_contacts (advisor_id, mandate_id, email) VALUES (%s, %s, %s)"
)
LOAD_INSERT_DOCUMENT = (
    "INSERT INTO documents (advisor_id, id, mandate_id, title, body, lang, origin)"
    " VALUES (%s, %s, %s, %s, %s, %s, %s)"
)
LOAD_INSERT_CHUNK = (
    "INSERT INTO chunks (advisor_id, id, document_id, ordinal, body, embedding,"
    " embedding_model, embedding_revision) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
)
# The global product sheets, in a transaction of their own after the advisors.
LOAD_DELETE_PRODUCT_SHEETS = "DELETE FROM product_sheets"
LOAD_INSERT_PRODUCT_SHEET = (
    "INSERT INTO product_sheets (id, title, body, lang, url, origin, embedding,"
    " embedding_model, embedding_revision) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
)
