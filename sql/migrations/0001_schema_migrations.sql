-- 0001_schema_migrations
-- Migration bookkeeping. Applied before every other migration.
-- Idempotent: safe to run against a database that already has the baseline
-- objects from sql/schema.sql.

CREATE TABLE IF NOT EXISTS schema_migrations (
    version text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now(),
    checksum text NOT NULL,
    applied_by text NOT NULL DEFAULT current_user
);

COMMENT ON TABLE schema_migrations IS
    'One row per applied migration file under sql/migrations.';
