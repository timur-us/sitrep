-- NAIVE, fail-first commit of M0 PR 5a: the roles with default attributes, the app role
-- owning the database and the schema, and the migrator creating its schema as itself.
-- The next commit replaces this file with the real role layout (ADR 0008).

CREATE ROLE sitrep_owner;
CREATE ROLE sitrep_migrator LOGIN;
CREATE ROLE sitrep_app LOGIN;

CREATE DATABASE sitrep OWNER sitrep_app;
GRANT CREATE ON DATABASE sitrep TO sitrep_migrator;

\connect sitrep

CREATE SCHEMA sitrep AUTHORIZATION sitrep_app;
