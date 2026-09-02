-- 0003_live_capture
-- Make the standard v1 ledger able to hold a live official-API capture run.
--
-- Why this migration exists
--   * Live runs preserve the containers and actors that activities point at:
--     Slack users, usergroups and conversations, Notion users and data
--     sources, and Google calendars. Rule 2 requires the service database to
--     be rebuildable from ledger data, and a message row without its channel
--     and its author is not rebuildable. The 0002 CHECK allowed only the five
--     activity entity types, so it is widened here.
--   * The widening is backward compatible: every entity type the 0002 loader
--     could write is still accepted, and no existing row changes.
--   * Dimension rows are never projected onto timeline_events. They live in
--     ledger_records only, which is why no timeline constraint moves here.
--
-- Idempotent. Contains no data.

ALTER TABLE ledger_records DROP CONSTRAINT IF EXISTS ledger_records_entity_type_check;
ALTER TABLE ledger_records
    ADD CONSTRAINT ledger_records_entity_type_check
    CHECK (entity_type IN (
        -- activity entities, projected onto the service timeline
        'message', 'page', 'block', 'comment', 'event',
        -- dimension entities, ledger only
        'user', 'usergroup', 'conversation', 'calendar', 'data_source'
    ));

COMMENT ON COLUMN ledger_records.entity_type IS
    'Activity entities (message, page, block, comment, event) are projected onto '
    'timeline_events. Dimension entities (user, usergroup, conversation, calendar, '
    'data_source) describe the containers and actors those activities reference and '
    'stay in the ledger only.';

COMMENT ON COLUMN ledger_records.capture_profile IS
    'How the record was captured. A profile beginning with "live-" is an official-API '
    'observation and loads at origin_priority 100; a "legacy-" profile loads at 20, or '
    'at 10 for the Slack thread-store supplement. A legacy re-run can therefore never '
    'demote a live head.';

-- Per-run coverage, so a capture gap is visible in SQL and not only in the
-- run manifest on disk.
CREATE OR REPLACE VIEW ledger_live_capture_runs AS
SELECT
    source,
    provenance ->> 'collector_run_id' AS collector_run_id,
    capture_profile,
    entity_type,
    min(observation_window_start) AS observation_date,
    count(*) AS record_count,
    count(DISTINCT source_entity_id) AS distinct_entities,
    count(*) FILTER (WHERE is_deleted) AS deleted_records,
    count(*) FILTER (WHERE (capture_completeness ->> 'truncated')::boolean) AS truncated_records,
    max((capture_completeness ->> 'rate_limit_hits')::integer) AS rate_limit_hits,
    count(*) FILTER (WHERE coverage -> 'permission_gap' IS NOT NULL
                       AND coverage -> 'permission_gap' <> 'null'::jsonb) AS records_with_permission_gap
FROM ledger_records
WHERE capture_profile LIKE 'live-%'
GROUP BY source, provenance ->> 'collector_run_id', capture_profile, entity_type;

COMMENT ON VIEW ledger_live_capture_runs IS
    'One row per live capture run, profile and entity type, with the truncation, '
    'rate-limit and permission-gap counters kept visible so a partial run is never '
    'read as a complete one.';
