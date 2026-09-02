-- 0002_ledger_v1
-- Standard v1 ledger storage plus the service-derived area.
--
-- Design notes
--   * ledger_records is the system of record for historical observations.
--     Service projections (timeline_events, source_object_*) are derived from
--     it and can be rebuilt without touching legacy files.
--   * Every ledger row carries source_file + source_file_sha256 so any
--     downstream value is traceable back to the exact legacy file (principle 8).
--   * Attribution is never stored here. The derived_* tables below are the
--     service-derived area and are populated by a later phase, not by the
--     legacy loader.
--
-- Idempotent. Contains no data.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ---------------------------------------------------------------- batches

CREATE TABLE IF NOT EXISTS ledger_batches (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source text NOT NULL CHECK (source IN ('slack', 'notion', 'google_calendar')),
    observation_date date,
    file_path text NOT NULL,
    file_sha256 text NOT NULL,
    record_count integer NOT NULL CHECK (record_count >= 0),
    converter_version text NOT NULL,
    loaded_at timestamptz NOT NULL DEFAULT now(),
    load_run_id uuid,
    UNIQUE (source, file_path, file_sha256)
);

COMMENT ON TABLE ledger_batches IS
    'One row per converted ledger JSONL file, hashed so a re-load is detectable.';

-- ------------------------------------------------------------ load runs

CREATE TABLE IF NOT EXISTS ledger_load_runs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source text NOT NULL CHECK (source IN ('slack', 'notion', 'google_calendar')),
    mode text NOT NULL CHECK (mode IN ('dry_run', 'apply')),
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    status text NOT NULL CHECK (status IN ('running', 'succeeded', 'failed', 'rolled_back')),
    ledger_root text NOT NULL,
    counters jsonb NOT NULL DEFAULT '{}'::jsonb,
    error_summary text
);

-- -------------------------------------------------------- ledger records

CREATE TABLE IF NOT EXISTS ledger_records (
    ledger_id uuid PRIMARY KEY,
    schema_version text NOT NULL,
    capture_profile text NOT NULL,
    source text NOT NULL CHECK (source IN ('slack', 'notion', 'google_calendar')),
    entity_type text NOT NULL CHECK (entity_type IN ('message', 'page', 'block', 'comment', 'event')),

    tenant_workspace_id text NOT NULL,
    tenant_status text NOT NULL,

    scope jsonb NOT NULL DEFAULT '{}'::jsonb,
    source_entity_id text NOT NULL,
    source_entity_key jsonb NOT NULL DEFAULT '{}'::jsonb,

    source_revision_id text,
    source_created_at timestamptz,
    source_updated_at timestamptz,
    source_updated_at_status text NOT NULL CHECK (source_updated_at_status IN ('observed', 'unknown')),
    collected_at timestamptz,

    is_deleted boolean,
    deleted_kind text,
    deleted_status text NOT NULL CHECK (deleted_status IN ('observed', 'unknown')),

    raw_payload jsonb NOT NULL,
    content_hash text NOT NULL,
    relations jsonb NOT NULL DEFAULT '{}'::jsonb,

    -- provenance, flattened for indexing plus the full object
    source_file text NOT NULL,
    source_file_sha256 text NOT NULL,
    source_file_kind text,
    record_pointer text NOT NULL,
    legacy_layout_version text NOT NULL,
    converter_version text NOT NULL,
    provenance jsonb NOT NULL DEFAULT '{}'::jsonb,

    coverage jsonb NOT NULL DEFAULT '{}'::jsonb,
    observation_role text NOT NULL
        CHECK (observation_role IN ('historical_observation', 'current_head')),

    observation_window_start date,
    observation_window_end date,
    observation_window jsonb NOT NULL DEFAULT '{}'::jsonb,

    capture_completeness_status text NOT NULL
        CHECK (capture_completeness_status IN ('recorded', 'not_recorded', 'unknown')),
    capture_completeness jsonb NOT NULL DEFAULT '{}'::jsonb,

    supplement_provenance jsonb NOT NULL DEFAULT '{}'::jsonb,
    visibility_routing jsonb NOT NULL DEFAULT '{}'::jsonb,
    denormalized_label_snapshot jsonb NOT NULL DEFAULT '{}'::jsonb,

    batch_id uuid REFERENCES ledger_batches(id),
    inserted_at timestamptz NOT NULL DEFAULT now()
);

COMMENT ON COLUMN ledger_records.source_updated_at_status IS
    'unknown means the legacy capture could not tell an unedited object from an '
    'object whose edit metadata was never collected. Never read a null '
    'source_updated_at as "never edited".';
COMMENT ON COLUMN ledger_records.tenant_workspace_id IS
    'Literal "unknown" when the workspace could not be resolved. Never inferred.';
COMMENT ON COLUMN ledger_records.observation_window_start IS
    'Legacy files are day slices. Summing across days is correct for usage; '
    'de-duplicating by source_entity_id alone is not.';

CREATE INDEX IF NOT EXISTS ledger_records_entity_idx
    ON ledger_records (source, entity_type, source_entity_id, observation_window_start DESC);
CREATE INDEX IF NOT EXISTS ledger_records_window_idx
    ON ledger_records (source, observation_window_start DESC);
CREATE INDEX IF NOT EXISTS ledger_records_source_file_idx
    ON ledger_records (source_file);
CREATE INDEX IF NOT EXISTS ledger_records_content_hash_idx
    ON ledger_records (content_hash);
CREATE INDEX IF NOT EXISTS ledger_records_capture_profile_idx
    ON ledger_records (capture_profile);
CREATE INDEX IF NOT EXISTS ledger_records_raw_payload_gin_idx
    ON ledger_records USING gin (raw_payload);
CREATE INDEX IF NOT EXISTS ledger_records_scope_gin_idx
    ON ledger_records USING gin (scope);

-- ------------------------------------------------------- extracted text

CREATE TABLE IF NOT EXISTS ledger_extracted_text (
    artifact_id uuid PRIMARY KEY,
    schema_version text NOT NULL,
    ledger_id uuid REFERENCES ledger_records(ledger_id) ON DELETE CASCADE,
    source text NOT NULL CHECK (source IN ('slack', 'notion', 'google_calendar')),
    kind text NOT NULL,
    text_content text NOT NULL,
    text_sha256 text NOT NULL,
    char_length integer NOT NULL,
    byte_length integer NOT NULL,
    extractor text NOT NULL,
    source_ref jsonb NOT NULL DEFAULT '{}'::jsonb,
    provenance jsonb NOT NULL DEFAULT '{}'::jsonb,
    batch_id uuid REFERENCES ledger_batches(id),
    inserted_at timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE ledger_extracted_text IS
    'Collector-extracted text that the live API cannot return again: Notion '
    '_blocks_text for pages whose block originals were never stored, and '
    'Gemini meeting notes for meetings the collecting account did not attend. '
    'Preserved verbatim; never rewritten.';

CREATE INDEX IF NOT EXISTS ledger_extracted_text_ledger_idx
    ON ledger_extracted_text (ledger_id);
CREATE INDEX IF NOT EXISTS ledger_extracted_text_kind_idx
    ON ledger_extracted_text (source, kind);

-- --------------------------------------------------- service-derived area
-- Populated by the service layer, never by the legacy ledger loader.
-- Kept separate so an inferred value can never be mistaken for an observation.

CREATE TABLE IF NOT EXISTS derived_attribution (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    ledger_id uuid REFERENCES ledger_records(ledger_id) ON DELETE CASCADE,
    bucket text NOT NULL,
    subject_external_id text NOT NULL,
    subject_person_id uuid REFERENCES people(id),
    method text NOT NULL,
    is_inferred boolean NOT NULL,
    confidence numeric(4,3) CHECK (confidence BETWEEN 0 AND 1),
    computed_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (ledger_id, bucket, subject_external_id, method)
);

COMMENT ON COLUMN derived_attribution.is_inferred IS
    'True for anything the legacy pipeline guessed, such as Notion '
    'assignments.assigned_by, which used a page last editor as the assigner.';

CREATE TABLE IF NOT EXISTS roster_identity_link (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    person_id uuid REFERENCES people(id),
    source text NOT NULL CHECK (source IN ('slack', 'notion', 'google_calendar', 'github', 'slurm')),
    source_user_id text NOT NULL,
    match_key text NOT NULL,
    nickname text,
    matched boolean NOT NULL,
    observed_at timestamptz,
    UNIQUE (source, source_user_id, match_key)
);

COMMENT ON COLUMN roster_identity_link.match_key IS
    'Each source matched people on a different key: slack_uid, notion_user_id, '
    'gcal nickname, github login, slurm username.';

CREATE TABLE IF NOT EXISTS computed_metrics (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    ledger_id uuid REFERENCES ledger_records(ledger_id) ON DELETE CASCADE,
    name text NOT NULL,
    value numeric,
    value_text text,
    formula_version text NOT NULL,
    computed_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (ledger_id, name, formula_version)
);

-- ------------------------------------------------------------------ views

CREATE OR REPLACE VIEW ledger_coverage_by_day AS
SELECT
    source,
    entity_type,
    observation_window_start AS observation_date,
    count(*) AS record_count,
    count(DISTINCT source_entity_id) AS distinct_entities,
    count(*) FILTER (WHERE tenant_status = 'unknown') AS tenant_unknown,
    count(*) FILTER (WHERE source_updated_at_status = 'unknown') AS updated_at_unknown,
    count(*) FILTER (WHERE deleted_status = 'unknown') AS deleted_unknown,
    count(*) FILTER (WHERE capture_completeness_status <> 'recorded') AS completeness_not_recorded
FROM ledger_records
GROUP BY source, entity_type, observation_window_start;

COMMENT ON VIEW ledger_coverage_by_day IS
    'Per-day ledger counts with the unknown columns kept visible, so a gap is '
    'never read as a clean day.';

-- ------------------------------------------------ baseline constraint fix
-- The baseline timeline_events CHECK omits 'notion', so a Notion projection
-- fails on insert. sync_runs, raw_objects, and identities already allow it.

ALTER TABLE timeline_events DROP CONSTRAINT IF EXISTS timeline_events_source_check;
ALTER TABLE timeline_events
    ADD CONSTRAINT timeline_events_source_check
    CHECK (source IN ('slack', 'google_calendar', 'github', 'notion'));
