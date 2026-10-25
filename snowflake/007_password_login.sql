-- Migrates an account provisioned before email/password login (specs.md §0, §2.1).
-- Safe to rerun. Existing guest USERS rows have no email/password; delete them or leave them unused.
USE ROLE HACKATHON_OWNER;
USE DATABASE HACKATHON_DB;
USE SCHEMA APP;

ALTER TABLE USERS ADD COLUMN IF NOT EXISTS PASSWORD_HASH VARCHAR(512);
ALTER TABLE CLUBS DROP COLUMN IF EXISTS IS_VERIFIED;
GRANT SELECT, INSERT ON TABLE CLUBS TO ROLE HACKATHON_INGEST;
