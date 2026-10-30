-- Roles, database and extension (ADR 0008). Run once by the image's entrypoint, as the
-- superuser, when the data directory is created; psql stops at the first error.
--
-- Three roles, every attribute written out:
--   sitrep_owner     NOLOGIN; owns the database, the schemas and everything in them.
--   sitrep_migrator  LOGIN; holds sitrep_owner WITH INHERIT FALSE, so it has none of the
--                    owner's rights until it runs SET ROLE sitrep_owner (app/db/migrate.py).
--   sitrep_app       LOGIN; no attributes, no memberships, owns nothing.
-- The schemas themselves come from db/migrations, applied by the migrator.

CREATE ROLE sitrep_owner NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
CREATE ROLE sitrep_migrator LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT NOREPLICATION NOBYPASSRLS;
CREATE ROLE sitrep_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT NOREPLICATION NOBYPASSRLS;

GRANT sitrep_owner TO sitrep_migrator WITH INHERIT FALSE, SET TRUE, ADMIN FALSE;

CREATE DATABASE sitrep OWNER sitrep_owner;

-- The other databases take no connections from the login roles.
REVOKE ALL ON DATABASE postgres FROM PUBLIC;
REVOKE ALL ON DATABASE template1 FROM PUBLIC;

\connect sitrep

-- vector is not a trusted extension: only the superuser can create it.
CREATE EXTENSION vector SCHEMA public;

-- On PostgreSQL 15 and later, public belongs to pg_database_owner, that is, to the
-- database owner. It goes to the superuser, so that sitrep_owner owns only what the
-- migrations create. PUBLIC keeps USAGE on it (for the vector type), and has no CREATE.
ALTER SCHEMA public OWNER TO postgres;

-- No CONNECT and no TEMPORARY for PUBLIC; CONNECT for the two login roles only.
REVOKE ALL ON DATABASE sitrep FROM PUBLIC;
GRANT CONNECT ON DATABASE sitrep TO sitrep_app, sitrep_migrator;

ALTER ROLE sitrep_app IN DATABASE sitrep SET search_path = sitrep, public;
ALTER ROLE sitrep_migrator IN DATABASE sitrep SET search_path = sitrep, public;
