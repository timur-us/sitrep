-- NAIVE (PR 5b, commit 1): the tables without row-level security, keys without
-- advisor_id, one text-search language, no vector dimension. Commit 3 replaces this file
-- before merge; the tests of commit 2 go red on it.

CREATE TABLE sitrep.advisors (
    advisor_id text PRIMARY KEY,
    display_name text NOT NULL
);

CREATE TABLE sitrep.mandates (
    id text PRIMARY KEY,
    advisor_id text NOT NULL REFERENCES sitrep.advisors (advisor_id),
    family text NOT NULL
);

CREATE TABLE sitrep.documents (
    id text PRIMARY KEY,
    advisor_id text NOT NULL,
    mandate_id text NOT NULL REFERENCES sitrep.mandates (id),
    title text NOT NULL,
    body text NOT NULL,
    lang text,
    tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', title || ' ' || body)) STORED
);

CREATE TABLE sitrep.chunks (
    id text PRIMARY KEY,
    advisor_id text NOT NULL,
    document_id text NOT NULL REFERENCES sitrep.documents (id),
    ordinal integer NOT NULL,
    body text NOT NULL,
    embedding public.vector NOT NULL,
    embedding_model text NOT NULL,
    embedding_revision text NOT NULL,
    UNIQUE (document_id, ordinal)
);

GRANT SELECT ON sitrep.advisors, sitrep.mandates, sitrep.documents, sitrep.chunks TO sitrep_app;
