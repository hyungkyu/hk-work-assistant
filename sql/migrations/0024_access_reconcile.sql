-- 0024_access_reconcile
--
-- Bring a database that ran the first 0023 to the shape of the second.
--
-- The first 0023 seeded rows in the migration and kept the roles in a table;
-- it went red on 통합데브 (a migration carries no literal data) before it was
-- deployed, so only test databases ran it. The rewritten 0023 keeps the roles
-- in code and writes the defaults at first read. A database that has the old
-- one keeps its old checksum, so this file does the difference, and is a
-- no-op everywhere else.

CREATE TABLE IF NOT EXISTS admin_access_meta (
    key        text PRIMARY KEY,
    written_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE admin_user_role DROP CONSTRAINT IF EXISTS admin_user_role_role_id_fkey;
ALTER TABLE admin_grant DROP CONSTRAINT IF EXISTS admin_grant_role_id_fkey;
DROP TABLE IF EXISTS admin_role;
