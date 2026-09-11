-- 0007_org_roster
-- The organisation, as observations rather than as a snapshot.
--
-- The legacy path was Sheet -> collection_config.json, overwritten whole. Two
-- consequences made it unusable for attribution: yesterday's org chart was
-- gone, so an activity collected last month could not be attributed to the
-- team that person was in at the time; and a person who left the sheet left
-- the system, so their past work became unattributable.
--
-- Three decisions this schema keeps, all HK's:
--
--   * Hire and leave dates are NOT stored (2026-09-04, sensitive). Validity
--     is derived from when we read the sheet -- a fact about us, not about
--     the person. There is deliberately no `joined_at` column to fill in
--     later.
--   * Employee numbers are NOT stored. People are keyed by email or name;
--     externals by the slurm/NCloud name `roster_seed_ext` carries.
--   * A person row is never deleted. Absence from the sheet is a status
--     (`absent_from_sheet`), because deleting the row would orphan every
--     past activity of theirs.
--
-- The roster is a dimension, not a collection source: no manifest, no
-- coverage grid, no checkpoint. "The org chart was not collected on
-- 2026-08-15" is not a fact about the world, and putting it on the coverage
-- screen would add a red square that means nothing.

CREATE TABLE IF NOT EXISTS roster_observation (
    observation_id bigserial PRIMARY KEY,
    observed_at timestamptz NOT NULL,
    source text NOT NULL CHECK (source IN ('roster_seed_2', 'roster_seed_ext')),
    -- The workbook's sha256. An unchanged re-read is recognisable as one,
    -- which is what lets the daily batch run every day without the history
    -- growing a new meaningless observation each time.
    source_digest text,
    row_count integer NOT NULL
);

CREATE INDEX IF NOT EXISTS roster_observation_observed_idx
    ON roster_observation (observed_at DESC);

CREATE TABLE IF NOT EXISTS org_person (
    -- Stable across a change of team or employment type, by construction:
    -- the key is derived from email or name and from nothing else.
    person_id text PRIMARY KEY,
    name text NOT NULL,
    first_seen bigint NOT NULL REFERENCES roster_observation(observation_id),
    last_seen bigint NOT NULL REFERENCES roster_observation(observation_id)
);

CREATE TABLE IF NOT EXISTS org_team (
    team_id text PRIMARY KEY,
    name text NOT NULL,
    parent_team_id text REFERENCES org_team(team_id),
    -- Keyed by full path, not by leaf name: two teams can share a name under
    -- different parents, and collapsing them merges two teams on the chart.
    path text NOT NULL UNIQUE,
    depth integer NOT NULL
);

-- Per-observation values. Not attributes of a person: "this is how they
-- looked when we read the sheet".
CREATE TABLE IF NOT EXISTS org_person_state (
    observation_id bigint NOT NULL REFERENCES roster_observation(observation_id),
    person_id text NOT NULL REFERENCES org_person(person_id),
    nickname text,
    title text,
    employment_type text,
    affiliation text NOT NULL
        CHECK (affiliation IN ('internal', 'professor', 'student', 'unknown')),
    access_level text NOT NULL
        CHECK (access_level IN ('staff_equivalent', 'limited', 'unknown')),
    status text NOT NULL
        CHECK (status IN ('active', 'retired', 'absent_from_sheet')),
    team_id text REFERENCES org_team(team_id),
    department_raw text,
    PRIMARY KEY (observation_id, person_id)
);

CREATE INDEX IF NOT EXISTS org_person_state_person_idx
    ON org_person_state (person_id);

-- The join keys: without these, a GitHub login or a Slurm account name
-- cannot be attached to a person at all.
--
-- Values in this table are never exported to a report, a dashboard or a
-- handover record. It exists to answer "whose account is this", and for
-- nothing else.
CREATE TABLE IF NOT EXISTS org_identity (
    kind text NOT NULL CHECK (kind IN (
        'email_official', 'email_personal', 'email_school',
        'github', 'slack', 'notion', 'slurm'
    )),
    value text NOT NULL,
    person_id text NOT NULL REFERENCES org_person(person_id),
    first_seen bigint NOT NULL REFERENCES roster_observation(observation_id),
    last_seen bigint NOT NULL REFERENCES roster_observation(observation_id),
    -- How this mapping came to be. `roster` is read from the sheet;
    -- `resolved` is a person's answer to an unmapped account, which is the
    -- only way a former Slurm name can ever be attached to anyone -- HK
    -- confirmed on 2026-09-11 that no old-to-new name table exists, so an
    -- unrecognised name is either a new person or a former name, and only a
    -- person can say which.
    origin text NOT NULL DEFAULT 'roster' CHECK (origin IN ('roster', 'resolved')),
    PRIMARY KEY (kind, value)
);

CREATE INDEX IF NOT EXISTS org_identity_person_idx ON org_identity (person_id);

-- Accounts seen in collected activity that no roster row claims. Not an
-- error table: it is the queue of questions for a person, and each row is
-- either a new account to add to the sheet or a former name to attach to
-- somebody who is already here.
CREATE TABLE IF NOT EXISTS org_unmapped_account (
    kind text NOT NULL,
    value text NOT NULL,
    first_seen_at timestamptz NOT NULL,
    last_seen_at timestamptz NOT NULL,
    events integer NOT NULL DEFAULT 0,
    -- 'open' is waiting on a person; 'resolved' has an org_identity row;
    -- 'ignored' is a bot or a service account somebody has judged.
    state text NOT NULL DEFAULT 'open'
        CHECK (state IN ('open', 'resolved', 'ignored')),
    resolved_person_id text REFERENCES org_person(person_id),
    note text,
    PRIMARY KEY (kind, value)
);

CREATE INDEX IF NOT EXISTS org_unmapped_account_state_idx
    ON org_unmapped_account (state, events DESC);

COMMENT ON TABLE roster_observation IS
    'One row per roster read. Validity of everything in org_person_state is '
    'derived from these timestamps, because hire and leave dates are not '
    'stored (HK, 2026-09-04).';
COMMENT ON TABLE org_unmapped_account IS
    'Source accounts with activity and no owner. Each is a question for a '
    'person: a new account, or a former name of somebody already here.';
