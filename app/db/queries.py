"""The statements the app's routes and the loader run, each a module-level constant
(ADR 0008).

Two kinds of SQL live elsewhere: the pool guard and the advisor context are string
literals in app/db/session.py, the one file that may name the advisor setting, and
app/db/migrate.py composes its own DDL from app/db/names.py. tests/test_sql_hygiene.py
holds the rest of app/ to this file and to literals: `.execute()` and its relatives take
a string literal or a constant from here, values are always bound parameters, and no
statement is built from strings at runtime.

Retrieval (ADR 0013) is the READ handlers' (app/harness/handlers.py), each statement run
in an advisor transaction; none names an advisor, since row-level security scopes it.
Each ranked list returns at most 20 rows (handlers.CANDIDATES), best first, ties by id.
Rows of an advisor-scoped table start with advisor_id, which the handlers compare with
the advisor of the transaction.

Full-text search has one statement per language (ADR 0005). Each takes the query once,
as the argument of the two-argument websearch_to_tsquery with its own configuration, and
keeps to the documents (or product sheets) in its language, so an English query never
matches a German document through a shared stem.
"""

SEARCH_DOCUMENTS_ENGLISH = (
    "SELECT d.advisor_id, d.id, ts_rank_cd(d.tsv, q) AS rank"
    " FROM documents d, websearch_to_tsquery('english', %s) AS q"
    " WHERE d.lang = 'english' AND d.tsv @@ q"
    " ORDER BY rank DESC, d.id LIMIT 20"
)

SEARCH_DOCUMENTS_GERMAN = (
    "SELECT d.advisor_id, d.id, ts_rank_cd(d.tsv, q) AS rank"
    " FROM documents d, websearch_to_tsquery('german', %s) AS q"
    " WHERE d.lang = 'german' AND d.tsv @@ q"
    " ORDER BY rank DESC, d.id LIMIT 20"
)

# The same, within one mandate: the query, then the mandate id.
SEARCH_MANDATE_DOCUMENTS_ENGLISH = (
    "SELECT d.advisor_id, d.id, ts_rank_cd(d.tsv, q) AS rank"
    " FROM documents d, websearch_to_tsquery('english', %s) AS q"
    " WHERE d.lang = 'english' AND d.mandate_id = %s AND d.tsv @@ q"
    " ORDER BY rank DESC, d.id LIMIT 20"
)

SEARCH_MANDATE_DOCUMENTS_GERMAN = (
    "SELECT d.advisor_id, d.id, ts_rank_cd(d.tsv, q) AS rank"
    " FROM documents d, websearch_to_tsquery('german', %s) AS q"
    " WHERE d.lang = 'german' AND d.mandate_id = %s AND d.tsv @@ q"
    " ORDER BY rank DESC, d.id LIMIT 20"
)

# The global product sheets (ADR 0010), read in any advisor transaction.
SEARCH_SHEETS_ENGLISH = (
    "SELECT s.id, ts_rank_cd(s.tsv, q) AS rank"
    " FROM product_sheets s, websearch_to_tsquery('english', %s) AS q"
    " WHERE s.lang = 'english' AND s.tsv @@ q"
    " ORDER BY rank DESC, s.id LIMIT 20"
)

SEARCH_SHEETS_GERMAN = (
    "SELECT s.id, ts_rank_cd(s.tsv, q) AS rank"
    " FROM product_sheets s, websearch_to_tsquery('german', %s) AS q"
    " WHERE s.lang = 'german' AND s.tsv @@ q"
    " ORDER BY rank DESC, s.id LIMIT 20"
)

# The vector lists: cosine distance to the query's vector (bound in pgvector's text
# form), among vectors of the query's model and revision only. An HNSW scan goes on
# past the rows the policies hide (hnsw.iterative_scan, app/db/session.py); in
# relaxed_order its rows may come a little out of order, so they are sorted again outside
# the MATERIALIZED query, as pgvector's README does it. The `+ 0` matters: PostgreSQL 17
# takes the CTE's own order as given and would only sort within equal distances
# (an incremental sort, measured), which keeps an out-of-order row where it is.
NEAREST_CHUNKS = (
    "WITH nearest AS MATERIALIZED ("
    "SELECT c.advisor_id, c.document_id, c.embedding <=> %s::vector AS distance"
    " FROM chunks c WHERE c.embedding_model = %s AND c.embedding_revision = %s"
    " ORDER BY distance LIMIT 20)"
    " SELECT n.advisor_id, n.document_id, n.distance FROM nearest n"
    " ORDER BY n.distance + 0, n.document_id"
)

# The same, within one mandate: the vector, the mandate id, the model, the revision.
NEAREST_MANDATE_CHUNKS = (
    "WITH nearest AS MATERIALIZED ("
    "SELECT c.advisor_id, c.document_id, c.embedding <=> %s::vector AS distance"
    " FROM chunks c JOIN documents d"
    " ON d.advisor_id = c.advisor_id AND d.id = c.document_id"
    " WHERE d.mandate_id = %s AND c.embedding_model = %s AND c.embedding_revision = %s"
    " ORDER BY distance LIMIT 20)"
    " SELECT n.advisor_id, n.document_id, n.distance FROM nearest n"
    " ORDER BY n.distance + 0, n.document_id"
)

# Six sheets, one vector each: no index, an exact distance.
NEAREST_SHEETS = (
    "SELECT s.id, s.embedding <=> %s::vector AS distance FROM product_sheets s"
    " WHERE s.embedding_model = %s AND s.embedding_revision = %s"
    " ORDER BY distance, s.id LIMIT 20"
)

# What a document hit shows, for the ids the fusion kept, in one statement and so one
# snapshot: its details, trust from its own column (ADR 0010, never inferred from an
# id), and its stored passage nearest the query, of the query's model (NULL if it has
# none). The model, the revision, the vector, then the ids.
DOCUMENT_HITS = (
    "SELECT d.advisor_id, d.id, d.mandate_id, d.title, d.lang, d.trust, p.body"
    " FROM documents d LEFT JOIN LATERAL ("
    "SELECT c.body FROM chunks c WHERE c.advisor_id = d.advisor_id AND c.document_id = d.id"
    " AND c.embedding_model = %s AND c.embedding_revision = %s"
    " ORDER BY c.embedding <=> %s::vector, c.ordinal LIMIT 1) p ON true"
    " WHERE d.id = ANY(%s)"
)

SHEET_DETAILS = (
    "SELECT s.id, s.title, s.lang, s.trust, s.body FROM product_sheets s WHERE s.id = ANY(%s)"
)

# A mandate the model names, looked up inside the advisor transaction: another
# advisor's mandate is not there, like one that exists nowhere.
MANDATE_VISIBLE = "SELECT m.advisor_id, m.id FROM mandates m WHERE m.id = %s"

# The mandate's portfolio report, from the portfolio system only, so no e-mail can pose
# as one. The corpus holds one per mandate; were there more, the first by id, since no
# document carries a date the database stores.
PORTFOLIO_REPORT = (
    "SELECT d.advisor_id, d.id, d.mandate_id, d.title, d.lang, d.body FROM documents d"
    " WHERE d.mandate_id = %s AND d.origin = 'portfolio_system' ORDER BY d.id LIMIT 1"
)

# The signed-in advisor's mandates (app/api/mandates.py). No advisor in the statement:
# row-level security scopes it to the advisor of the transaction.
LIST_MANDATES = "SELECT m.id, m.family FROM mandates m ORDER BY m.id"

# The title, kind and trust of each record an answer cites (POST /api/ask, M1 PR 12;
# trust: M1 PR 12b), after the loop, in the advisor's own transaction: row-level
# security scopes the documents, and the advisor_id column lets the caller refuse a row
# of another advisor (a second line). The trust is the record's generated column (ADR
# 0010), read with its title in one statement, so the two come from one snapshot.
CITED_DOCUMENTS = (
    "SELECT d.advisor_id, d.id, d.title, d.trust FROM documents d WHERE d.id = ANY(%s)"
)
CITED_SHEETS = "SELECT s.id, s.title, s.trust FROM product_sheets s WHERE s.id = ANY(%s)"

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
