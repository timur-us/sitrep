-- The app schema. The runner has run SET ROLE sitrep_owner, so the owner owns it.
-- No tables yet: they arrive in 0002 (PR 5b), with row-level security.

CREATE SCHEMA sitrep;
REVOKE ALL ON SCHEMA sitrep FROM PUBLIC;
GRANT USAGE ON SCHEMA sitrep TO sitrep_app;

-- PostgreSQL grants EXECUTE on new functions and USAGE on new types to PUBLIC by default.
-- Not for the owner's objects: each grant to the app role is written out (PR 5b on).
ALTER DEFAULT PRIVILEGES REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
ALTER DEFAULT PRIVILEGES REVOKE USAGE ON TYPES FROM PUBLIC;
