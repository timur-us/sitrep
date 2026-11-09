-- The loader's schema (ADR 0010; M1 plan, PR 7). The runner has run SET ROLE sitrep_owner.
--
-- origin says where a text comes from: the loader derives it from the record's type
-- through data/synthetic/generate.py's ORIGIN. Each table's CHECK allows exactly the
-- origins ADR 0010's yaml block gives that table (tests/db/test_loader.py pins the two).
-- trust is a STORED generated column, so no writer sets it: an INSERT or UPDATE that
-- names it fails. It is 'internal' only for the internal origins it lists, so an origin
-- added later without its trust reads 'untrusted' (fail closed). Trust is provenance,
-- never permission: every retrieval hit is still Untrusted in the harness (rule 9).
--
-- Attack documents (M1 PR 17) carry ids starting with 'rt-', in any case: in documents
-- only, and only as mailbox. No other table holds such an id.

-- Rows that exist before this file (test leftovers: nothing loaded the corpus before)
-- take 'mailbox', the untrusted origin. The default goes again at once, so every writer
-- names the origin.
ALTER TABLE sitrep.documents
    ADD COLUMN origin text NOT NULL DEFAULT 'mailbox'
        CONSTRAINT documents_origin_check
        CHECK (origin IN ('kyc_register', 'portfolio_system', 'call_notes', 'mailbox'));
ALTER TABLE sitrep.documents ALTER COLUMN origin DROP DEFAULT;
ALTER TABLE sitrep.documents
    ADD COLUMN trust text GENERATED ALWAYS AS (
        CASE WHEN origin IN ('kyc_register', 'portfolio_system', 'call_notes')
            THEN 'internal' ELSE 'untrusted' END
    ) STORED;
ALTER TABLE sitrep.documents
    ADD CONSTRAINT documents_attack_id_check CHECK (id !~* '^rt-' OR origin = 'mailbox');
ALTER TABLE sitrep.mandates
    ADD CONSTRAINT mandates_attack_id_check CHECK (id !~* '^rt-');
ALTER TABLE sitrep.advisors
    ADD CONSTRAINT advisors_attack_id_check CHECK (advisor_id !~* '^rt-');

-- Each mandate's recipients, from the mandate record's client_email, never from an
-- e-mail's headers: the input of M2's recipient allowlist (PR 8 sec-2). advisor_id is in
-- every key, as in 0002. One lower-case address per row, without spaces or line breaks;
-- the loader also holds it to the staff domain.
CREATE TABLE sitrep.mandate_contacts (
    advisor_id text NOT NULL,
    mandate_id text NOT NULL CONSTRAINT mandate_contacts_attack_id_check
        CHECK (mandate_id !~* '^rt-'),
    email text NOT NULL CONSTRAINT mandate_contacts_email_check
        CHECK (email ~ '^[a-z0-9._+-]+@[a-z0-9-]+(\.[a-z0-9-]+)+$'),
    PRIMARY KEY (advisor_id, mandate_id, email),
    FOREIGN KEY (advisor_id, mandate_id) REFERENCES sitrep.mandates (advisor_id, id)
);

ALTER TABLE sitrep.mandate_contacts ENABLE ROW LEVEL SECURITY;
ALTER TABLE sitrep.mandate_contacts FORCE ROW LEVEL SECURITY;
CREATE POLICY advisor_scope ON sitrep.mandate_contacts FOR ALL TO PUBLIC
    USING (advisor_id = NULLIF(current_setting('app.advisor_id', true), ''))
    WITH CHECK (advisor_id = NULLIF(current_setting('app.advisor_id', true), ''));

-- The bank's product sheets: the one global table, with no advisor_id, and the catalog
-- test's only exemption, by name (PR 5b product sheets; PR 8 sec-8 and spec-4). No
-- foreign key into or out of it. No policy anywhere admits advisor_id IS NULL.
-- - The app role reads every sheet, inside an advisor transaction only: with no advisor,
--   or the '' a reused connection carries, it reads nothing.
-- - The owner alone writes, for every command, since FORCE binds it and no policy means
--   default deny; the loader replaces the sheets by DELETE, never TRUNCATE.
-- - url is the exact https URL, with no port, query, fragment or user part: the input of
--   M2's link allowlist (PR 8 sec-3). The loader holds the host to the staff domain.
-- - One embedding per sheet: a sheet over one passage is refused by the loader, never cut.
CREATE TABLE sitrep.product_sheets (
    id text NOT NULL CONSTRAINT product_sheets_attack_id_check CHECK (id !~* '^rt-'),
    title text NOT NULL,
    body text NOT NULL,
    lang text NOT NULL CHECK (lang IN ('german', 'english')),
    url text NOT NULL CONSTRAINT product_sheets_url_check
        CHECK (url ~ '^https://[a-z0-9-]+(\.[a-z0-9-]+)+(/[a-z0-9-]+)+$'),
    origin text NOT NULL CONSTRAINT product_sheets_origin_check
        CHECK (origin IN ('product_catalogue')),
    trust text GENERATED ALWAYS AS (
        CASE WHEN origin IN ('product_catalogue') THEN 'internal' ELSE 'untrusted' END
    ) STORED,
    tsv tsvector GENERATED ALWAYS AS (
        CASE lang
            WHEN 'german' THEN to_tsvector('german', title || ' ' || body)
            WHEN 'english' THEN to_tsvector('english', title || ' ' || body)
        END
    ) STORED,
    embedding public.vector(384) NOT NULL,
    embedding_model text NOT NULL,
    embedding_revision text NOT NULL,
    PRIMARY KEY (id),
    UNIQUE (url)
);

ALTER TABLE sitrep.product_sheets ENABLE ROW LEVEL SECURITY;
ALTER TABLE sitrep.product_sheets FORCE ROW LEVEL SECURITY;
CREATE POLICY advisor_read ON sitrep.product_sheets FOR SELECT TO sitrep_app
    USING (NULLIF(current_setting('app.advisor_id', true), '') IS NOT NULL);
CREATE POLICY owner_write ON sitrep.product_sheets FOR ALL TO sitrep_owner
    USING (true) WITH CHECK (true);

GRANT SELECT ON sitrep.mandate_contacts, sitrep.product_sheets TO sitrep_app;
