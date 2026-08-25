CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS sync_runs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source text NOT NULL CHECK (source IN ('slack', 'google_calendar', 'github')),
    environment text NOT NULL CHECK (environment IN ('test', 'production')),
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    status text NOT NULL CHECK (status IN ('running', 'succeeded', 'failed', 'partial')),
    checkpoint jsonb NOT NULL DEFAULT '{}'::jsonb,
    counters jsonb NOT NULL DEFAULT '{}'::jsonb,
    error_summary text
);

CREATE TABLE IF NOT EXISTS raw_objects (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    sync_run_id uuid NOT NULL REFERENCES sync_runs(id),
    source text NOT NULL CHECK (source IN ('slack', 'google_calendar', 'github')),
    external_id text NOT NULL,
    version_key text NOT NULL,
    archive_path text NOT NULL,
    sha256 text NOT NULL,
    observed_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source, external_id, version_key, sha256)
);

CREATE TABLE IF NOT EXISTS people (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    display_name text,
    primary_email text,
    is_bot boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS identities (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    person_id uuid NOT NULL REFERENCES people(id),
    source text NOT NULL CHECK (source IN ('slack', 'google_calendar', 'github')),
    external_id text NOT NULL,
    handle text,
    email text,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (source, external_id)
);

CREATE TABLE IF NOT EXISTS timeline_events (
    event_id uuid PRIMARY KEY,
    source text NOT NULL CHECK (source IN ('slack', 'google_calendar', 'github')),
    event_type text NOT NULL,
    external_id text NOT NULL,
    actor_external_id text,
    actor_person_id uuid REFERENCES people(id),
    occurred_at timestamptz NOT NULL,
    updated_at timestamptz,
    ingested_at timestamptz NOT NULL,
    container_id text,
    thread_id text,
    permalink text,
    classifications text[] NOT NULL DEFAULT ARRAY['unclassified']::text[],
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    raw_object_id uuid REFERENCES raw_objects(id),
    UNIQUE (source, event_type, external_id, event_id)
);

CREATE INDEX IF NOT EXISTS timeline_events_occurred_at_idx
    ON timeline_events (occurred_at DESC);
CREATE INDEX IF NOT EXISTS timeline_events_actor_idx
    ON timeline_events (actor_person_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS timeline_events_container_idx
    ON timeline_events (source, container_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS timeline_events_payload_gin_idx
    ON timeline_events USING gin (payload);
CREATE INDEX IF NOT EXISTS timeline_events_classifications_gin_idx
    ON timeline_events USING gin (classifications);

CREATE TABLE IF NOT EXISTS mentions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    event_id uuid NOT NULL REFERENCES timeline_events(event_id) ON DELETE CASCADE,
    target_external_id text NOT NULL,
    target_person_id uuid REFERENCES people(id),
    kind text NOT NULL CHECK (kind IN ('direct', 'user_group', 'channel', 'here', 'everyone')),
    direction text NOT NULL CHECK (direction IN ('to_self', 'from_self', 'other', 'group', 'broadcast')),
    priority integer NOT NULL CHECK (priority BETWEEN 0 AND 100)
);

CREATE INDEX IF NOT EXISTS mentions_target_idx
    ON mentions (target_person_id, priority DESC);
CREATE INDEX IF NOT EXISTS mentions_event_idx
    ON mentions (event_id);

CREATE TABLE IF NOT EXISTS action_candidates (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    event_id uuid NOT NULL REFERENCES timeline_events(event_id) ON DELETE CASCADE,
    kind text NOT NULL CHECK (
        kind IN ('request', 'promise', 'decision', 'question', 'response', 'schedule_change')
    ),
    rule_id text NOT NULL,
    confidence numeric(4,3) NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    state text NOT NULL DEFAULT 'open' CHECK (state IN ('open', 'resolved', 'dismissed')),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (event_id, kind, rule_id)
);

