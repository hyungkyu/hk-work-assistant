-- 0010_conversation_blocks
--
-- What a situation actually is.
--
-- Measured on 2026-09-21: 63,092 Slack messages embedded one per row, and the
-- nearest neighbours of "배포를 사람이 직접 돌리고 있다" were "퇴근하고
-- 운동중입니다" and "ㅋㅋㅋ 잘드시네요". Nothing was broken. One line of chat
-- carries almost no meaning on its own, so in a space of sixty thousand of
-- them everything sits at roughly the same distance and the closest match is
-- noise.
--
-- A conversation carries meaning. This table holds contiguous runs of messages
-- in one channel -- who was talking, about what, and what HK said inside that
-- run. The situation text and his own text are kept apart on purpose: the
-- query is a situation, so the situation is what gets embedded and compared,
-- and his words are the answer the search returns rather than part of the key.
--
-- Derived, not preserved. `ledger_records` is the record; a better blocking
-- rule rewrites these rows, which is why the rule's parameters are stored on
-- each row -- a block built under a different window is visible as such
-- instead of being silently mixed with the others.

CREATE TABLE IF NOT EXISTS conversation_blocks (
    block_id uuid PRIMARY KEY,
    source text NOT NULL,
    channel text NOT NULL,
    -- text, not uuid: org_person keys people by text and a mismatch here
    -- would be a foreign key that cannot be declared.
    person_id text REFERENCES org_person(person_id) ON DELETE CASCADE,
    started_at timestamptz NOT NULL,
    ended_at timestamptz NOT NULL,
    -- The blocking rule this row was built under. Two rows built under
    -- different rules are different objects, and comparing them would be
    -- comparing two questions.
    gap_minutes integer NOT NULL,
    max_messages integer NOT NULL,
    message_count integer NOT NULL,
    speaker_count integer NOT NULL,
    -- What other people said, rendered as "name: text" lines in time order.
    -- This is the key: it is what a question about a situation is compared
    -- against.
    situation_text text NOT NULL,
    -- What HK said inside this run, same rendering. The payload.
    his_text text NOT NULL,
    his_first_at timestamptz,
    permalink text,
    built_at timestamptz NOT NULL DEFAULT now(),
    embedding vector(1024),
    embedding_model text,
    embedded_at timestamptz
);

COMMENT ON TABLE conversation_blocks IS
    'Contiguous Slack exchanges in which one person spoke, with what others '
    'said and what they said held apart. Derived from ledger_records; a '
    'better blocking rule rewrites these rows. The situation text is what '
    'the precedent search embeds and compares -- single messages are too '
    'short for a vector to separate topics (measured 2026-09-21).';

CREATE INDEX IF NOT EXISTS conversation_blocks_person_idx
    ON conversation_blocks (person_id, started_at DESC);

CREATE INDEX IF NOT EXISTS conversation_blocks_unembedded_idx
    ON conversation_blocks (built_at)
    WHERE embedding IS NULL;

CREATE INDEX IF NOT EXISTS conversation_blocks_rule_idx
    ON conversation_blocks (gap_minutes, max_messages);
