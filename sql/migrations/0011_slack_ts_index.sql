-- 0011_slack_ts_index
--
-- A Slack row's id is "{workspace}:{channel}:{ts}" but a reply names its
-- parent by the bare ts, so every join between the two has to strip the
-- prefix. Without an index that strip is a sequential scan of every Slack row
-- once per reply -- and the orphan sweep does exactly that join, for every
-- reply in the ledger, every night.
--
-- Partial on source = 'slack' because no other source spells ids this way.

CREATE INDEX IF NOT EXISTS ledger_records_slack_ts_idx
    ON ledger_records (
        (regexp_replace(source_entity_id, '^.*:', ''))
    )
    WHERE source = 'slack';

-- The thread walk asks for "every message in this channel whose parent ts is
-- X", which is the pair, not the ts alone.
CREATE INDEX IF NOT EXISTS ledger_records_slack_parent_idx
    ON ledger_records (
        (coalesce(scope->>'channel_id', scope->>'container')),
        (coalesce(relations->>'thread_id', relations->>'parent_ts'))
    )
    WHERE source = 'slack';

-- The nearby-messages route asks, once per answer, for the messages in this
-- channel in the minutes before it. 5,120 answers took 1,408 seconds on
-- 2026-09-22 -- a quarter of a second each, which is one scan per answer.
CREATE INDEX IF NOT EXISTS ledger_records_slack_channel_time_idx
    ON ledger_records (
        (coalesce(scope->>'channel_id', scope->>'container')),
        source_created_at DESC
    )
    WHERE source = 'slack';
