-- The app schema. The runner has run SET ROLE sitrep_owner, so the owner owns it.
-- No tables yet: they arrive in 0002 (PR 5b), with row-level security.

CREATE SCHEMA sitrep;
REVOKE ALL ON SCHEMA sitrep FROM PUBLIC;
GRANT USAGE ON SCHEMA sitrep TO sitrep_app;
