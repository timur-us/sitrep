-- The mandate-scoped tables, with row-level security (ADR 0008; threat-model path 2).
-- The runner has run SET ROLE sitrep_owner, so the owner owns them.
--
-- Every table carries advisor_id, and every key and unique constraint includes it: a key
-- another advisor holds can never cause a unique or foreign-key violation, so no write
-- can probe for one (referential-integrity checks bypass row-level security).
--
-- One policy per table: a row is visible and writable only while the transaction's
-- app.advisor_id names its advisor. Unset (NULL) or empty ('', what a reused connection
-- reads after a transaction-local setting) matches no row: fail closed. FORCE binds the
-- owner too; only a superuser or a BYPASSRLS role passes, and the app role is neither.
--
-- documents.lang is 'german' or 'english' (ADR 0005), not regconfig, which pg_upgrade
-- refuses in user tables; the generated tsv uses the two-argument to_tsvector per
-- language. chunks.embedding has the model's dimension (app/config/embedding.py), and
-- each vector records the model and revision that made it.

CREATE TABLE sitrep.advisors (
    advisor_id text NOT NULL,
    display_name text NOT NULL,
    PRIMARY KEY (advisor_id)
);

CREATE TABLE sitrep.mandates (
    advisor_id text NOT NULL REFERENCES sitrep.advisors (advisor_id),
    id text NOT NULL,
    family text NOT NULL,
    PRIMARY KEY (advisor_id, id)
);

CREATE TABLE sitrep.documents (
    advisor_id text NOT NULL,
    id text NOT NULL,
    mandate_id text NOT NULL,
    title text NOT NULL,
    body text NOT NULL,
    lang text NOT NULL CHECK (lang IN ('german', 'english')),
    tsv tsvector GENERATED ALWAYS AS (
        CASE lang
            WHEN 'german' THEN to_tsvector('german', title || ' ' || body)
            WHEN 'english' THEN to_tsvector('english', title || ' ' || body)
        END
    ) STORED,
    PRIMARY KEY (advisor_id, id),
    FOREIGN KEY (advisor_id, mandate_id) REFERENCES sitrep.mandates (advisor_id, id)
);

CREATE TABLE sitrep.chunks (
    advisor_id text NOT NULL,
    id text NOT NULL,
    document_id text NOT NULL,
    ordinal integer NOT NULL CHECK (ordinal >= 0),
    body text NOT NULL,
    embedding public.vector(384) NOT NULL,
    embedding_model text NOT NULL,
    embedding_revision text NOT NULL,
    PRIMARY KEY (advisor_id, id),
    UNIQUE (advisor_id, document_id, ordinal),
    FOREIGN KEY (advisor_id, document_id) REFERENCES sitrep.documents (advisor_id, id)
);

CREATE INDEX chunks_embedding ON sitrep.chunks USING hnsw (embedding public.vector_cosine_ops);

ALTER TABLE sitrep.advisors ENABLE ROW LEVEL SECURITY;
ALTER TABLE sitrep.advisors FORCE ROW LEVEL SECURITY;
CREATE POLICY advisor_scope ON sitrep.advisors FOR ALL TO PUBLIC
    USING (advisor_id = NULLIF(current_setting('app.advisor_id', true), ''))
    WITH CHECK (advisor_id = NULLIF(current_setting('app.advisor_id', true), ''));

ALTER TABLE sitrep.mandates ENABLE ROW LEVEL SECURITY;
ALTER TABLE sitrep.mandates FORCE ROW LEVEL SECURITY;
CREATE POLICY advisor_scope ON sitrep.mandates FOR ALL TO PUBLIC
    USING (advisor_id = NULLIF(current_setting('app.advisor_id', true), ''))
    WITH CHECK (advisor_id = NULLIF(current_setting('app.advisor_id', true), ''));

ALTER TABLE sitrep.documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE sitrep.documents FORCE ROW LEVEL SECURITY;
CREATE POLICY advisor_scope ON sitrep.documents FOR ALL TO PUBLIC
    USING (advisor_id = NULLIF(current_setting('app.advisor_id', true), ''))
    WITH CHECK (advisor_id = NULLIF(current_setting('app.advisor_id', true), ''));

ALTER TABLE sitrep.chunks ENABLE ROW LEVEL SECURITY;
ALTER TABLE sitrep.chunks FORCE ROW LEVEL SECURITY;
CREATE POLICY advisor_scope ON sitrep.chunks FOR ALL TO PUBLIC
    USING (advisor_id = NULLIF(current_setting('app.advisor_id', true), ''))
    WITH CHECK (advisor_id = NULLIF(current_setting('app.advisor_id', true), ''));

-- SELECT only in M0; the first writes arrive in M1 with their own tables.
GRANT SELECT ON sitrep.advisors, sitrep.mandates, sitrep.documents, sitrep.chunks TO sitrep_app;
