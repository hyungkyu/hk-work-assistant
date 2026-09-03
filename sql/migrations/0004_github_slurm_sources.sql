-- 0004_github_slurm_sources
-- Make the ledger and the service projection able to hold GitHub and Slurm.
--
-- Why this migration exists
--   * The GitHub and Slurm collectors are ports of the legacy weekly tooling.
--     Their records are activities like any other source, but every `source`
--     CHECK written in the baseline and in 0002 enumerates only the three
--     sources that existed then, so an insert from either collector fails.
--   * `ledger_records.entity_type` allowed only the five activity types plus
--     the five dimension types from 0003. GitHub contributes six activity
--     types and Slurm one, so the CHECK is widened the same way 0003 widened
--     it for dimension entities.
--   * The widening is backward compatible: every source and entity type the
--     0002/0003 loader could write is still accepted, and no existing row
--     changes.
--
-- Scope note
--   `job_step` (Slurm `.batch` / `.extern` rows) is deliberately NOT added
--   here. Those rows carry the real resource usage (MaxRSS, TRESUsageIn*) and
--   the raw archive preserves them, but they are sub-resources of a job, not
--   activities in their own right: projecting them onto the timeline would
--   multiply each job into two or three timeline events. Giving them a ledger
--   entity type needs a third category alongside activity and dimension, and
--   that is a schema decision, not a port detail. Until it is made, steps live
--   in the raw archive only.
--
-- Idempotent. Contains no data.

-- --------------------------------------------------------- ledger `source`

ALTER TABLE ledger_batches DROP CONSTRAINT IF EXISTS ledger_batches_source_check;
ALTER TABLE ledger_batches
    ADD CONSTRAINT ledger_batches_source_check
    CHECK (source IN ('slack', 'notion', 'google_calendar', 'github', 'slurm'));

ALTER TABLE ledger_load_runs DROP CONSTRAINT IF EXISTS ledger_load_runs_source_check;
ALTER TABLE ledger_load_runs
    ADD CONSTRAINT ledger_load_runs_source_check
    CHECK (source IN ('slack', 'notion', 'google_calendar', 'github', 'slurm'));

ALTER TABLE ledger_records DROP CONSTRAINT IF EXISTS ledger_records_source_check;
ALTER TABLE ledger_records
    ADD CONSTRAINT ledger_records_source_check
    CHECK (source IN ('slack', 'notion', 'google_calendar', 'github', 'slurm'));

ALTER TABLE ledger_extracted_text DROP CONSTRAINT IF EXISTS ledger_extracted_text_source_check;
ALTER TABLE ledger_extracted_text
    ADD CONSTRAINT ledger_extracted_text_source_check
    CHECK (source IN ('slack', 'notion', 'google_calendar', 'github', 'slurm'));

-- ---------------------------------------------------- ledger `entity_type`

ALTER TABLE ledger_records DROP CONSTRAINT IF EXISTS ledger_records_entity_type_check;
ALTER TABLE ledger_records
    ADD CONSTRAINT ledger_records_entity_type_check
    CHECK (entity_type IN (
        -- activity entities, projected onto the service timeline
        'message', 'page', 'block', 'comment', 'event',
        -- GitHub activity entities
        'commit', 'pull_request', 'review', 'review_comment',
        'issue', 'issue_comment',
        -- Slurm activity entities
        'job',
        -- dimension entities, ledger only
        'user', 'usergroup', 'conversation', 'calendar', 'data_source',
        'repository'
    ));

COMMENT ON COLUMN ledger_records.entity_type IS
    'Activity entities (message, page, block, comment, event; GitHub commit, '
    'pull_request, review, review_comment, issue, issue_comment; Slurm job) are '
    'projected onto timeline_events. Dimension entities (user, usergroup, '
    'conversation, calendar, data_source, repository) describe the containers and '
    'actors those activities reference and stay in the ledger only. Slurm step rows '
    'are held in the raw archive and have no ledger entity type yet.';

-- ------------------------------------------- service projection `source`
-- The baseline CHECKs predate both sources. timeline_events was already
-- rewritten once, at the end of 0002, to admit 'notion'; it is rewritten here
-- rather than edited there so 0002's checksum stays stable.

ALTER TABLE sync_runs DROP CONSTRAINT IF EXISTS sync_runs_source_check;
ALTER TABLE sync_runs
    ADD CONSTRAINT sync_runs_source_check
    CHECK (source IN ('slack', 'google_calendar', 'notion', 'github', 'slurm'));

ALTER TABLE raw_objects DROP CONSTRAINT IF EXISTS raw_objects_source_check;
ALTER TABLE raw_objects
    ADD CONSTRAINT raw_objects_source_check
    CHECK (source IN ('slack', 'google_calendar', 'notion', 'github', 'slurm'));

ALTER TABLE identities DROP CONSTRAINT IF EXISTS identities_source_check;
ALTER TABLE identities
    ADD CONSTRAINT identities_source_check
    CHECK (source IN ('slack', 'google_calendar', 'notion', 'github', 'slurm'));

ALTER TABLE timeline_events DROP CONSTRAINT IF EXISTS timeline_events_source_check;
ALTER TABLE timeline_events
    ADD CONSTRAINT timeline_events_source_check
    CHECK (source IN ('slack', 'google_calendar', 'notion', 'github', 'slurm'));

ALTER TABLE source_object_observations
    DROP CONSTRAINT IF EXISTS source_object_observations_source_check;
ALTER TABLE source_object_observations
    ADD CONSTRAINT source_object_observations_source_check
    CHECK (source IN ('slack', 'google_calendar', 'notion', 'github', 'slurm'));
