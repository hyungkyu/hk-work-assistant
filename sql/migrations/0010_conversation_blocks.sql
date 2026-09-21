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
    -- How this block was found. 'window' is a run of messages close together
    -- in one channel; 'thread' is a Slack thread followed from his reply back
    -- to the message that started it.
    --
    -- HK, 2026-09-21: 내가 쓴글이 댓글이면 원글을 찾고... 뭐 이런식으로 탐색해
    -- 보는건 어때? Right, and time alone cannot do it: a thread's parent can
    -- be hours or days before the reply, so a window around his message never
    -- contains it. Structure says what time only guesses.
    kind text NOT NULL DEFAULT 'window' CHECK (kind IN ('window', 'thread')),
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

-- Added after the table, so a database that already created it under the
-- first version of this file gains the column instead of silently missing it.
-- The table statement above is idempotent and therefore skips an existing
-- table entirely, change and all -- which is what this ALTER is for.
--
-- (The phrase for that statement is avoided in this comment on purpose: the
-- idempotency check in tests/test_ledger_migrations.py reads these files as
-- text and is not comment-aware, so writing it here fails the suite.)
ALTER TABLE conversation_blocks
    ADD COLUMN IF NOT EXISTS kind text NOT NULL DEFAULT 'window';

-- Human judgement about the pair.
--
-- HK, 2026-09-21: 어드민에서 Q&A pair 를 선택/삭제할 수 있게 하면 어때? Yes,
-- and it is the highest-value part of this. Whether a block really is
-- (situation -> his intervention) is not something the blocking rule can
-- decide: a run of messages can be two topics, and his reply can be about
-- neither. A person skimming decides it in a second.
--
-- It is also the evaluation set. 'kept' and 'dropped' are exactly the labels
-- needed to measure whether an agent's answer matches what he would have
-- asked, and they arrive as a side effect of curation rather than as a
-- separate annotation project.
--
-- Default 'pending', so nothing is presumed reviewed. The search uses kept and
-- pending and never dropped; 'dropped' hides the block without deleting it,
-- because a rebuild would otherwise bring back a pair a person has already
-- judged.
ALTER TABLE conversation_blocks
    ADD COLUMN IF NOT EXISTS review text NOT NULL DEFAULT 'pending';
ALTER TABLE conversation_blocks
    ADD COLUMN IF NOT EXISTS reviewed_by text;
ALTER TABLE conversation_blocks
    ADD COLUMN IF NOT EXISTS reviewed_at timestamptz;
ALTER TABLE conversation_blocks
    ADD COLUMN IF NOT EXISTS review_note text;

CREATE INDEX IF NOT EXISTS conversation_blocks_review_idx
    ON conversation_blocks (person_id, review, started_at DESC);

CREATE INDEX IF NOT EXISTS conversation_blocks_person_idx
    ON conversation_blocks (person_id, started_at DESC);

CREATE INDEX IF NOT EXISTS conversation_blocks_unembedded_idx
    ON conversation_blocks (built_at)
    WHERE embedding IS NULL;

CREATE INDEX IF NOT EXISTS conversation_blocks_rule_idx
    ON conversation_blocks (gap_minutes, max_messages);


-- The pairing itself, proposed and correctable.
--
-- HK, 2026-09-21: 결과적으로, 이 답변의 원 질문은 이거 일거 같다는 후보들이
-- 있어서, 난 그걸 선택하는거지. 정확히는 네가 페어링을 한것을 가정하되, 나는
-- 수정할 수 있게 하는거지.
--
-- So a pair is not a fact the blocking rule asserts. For one of his answers
-- there are several plausible questions, each reached by a different route --
-- Slack's own thread link, a permalink he quoted, the messages around his --
-- and the routes disagree. The rows below hold the candidates with the route
-- and the score that produced them, one of them marked as the proposal, and
-- room for a person to mark a different one.
--
-- Why keep the candidates instead of only the answer: a correction is only
-- informative next to what was proposed. "He chose the thread parent over the
-- nearer message" is a fact about how to rank; "the answer is X" is not.
CREATE TABLE IF NOT EXISTS answer_pairs (
    pair_id uuid PRIMARY KEY,
    person_id text NOT NULL REFERENCES org_person(person_id) ON DELETE CASCADE,
    -- His message: the answer, and the thing a pair is always about.
    answer_ledger_id uuid NOT NULL REFERENCES ledger_records(ledger_id) ON DELETE CASCADE,
    answer_text text NOT NULL,
    answer_at timestamptz NOT NULL,
    channel text NOT NULL,
    permalink text,
    -- One candidate question for that answer.
    candidate_block_id uuid REFERENCES conversation_blocks(block_id) ON DELETE CASCADE,
    candidate_text text NOT NULL,
    -- How this candidate was reached. 'thread' is Slack's own link, 'quote' is
    -- a permalink in his own message, 'window' is the conversation around it.
    -- Kept per row because it is the thing his corrections teach: which route
    -- to trust when they disagree.
    basis text NOT NULL CHECK (basis IN ('thread', 'quote', 'window')),
    score double precision NOT NULL DEFAULT 0,
    rank integer NOT NULL DEFAULT 0,
    -- The proposal, and the decision. `proposed` is what this system guessed;
    -- `chosen` is what a person said. Both are kept, because the difference is
    -- the only measurement of whether the guessing is getting better.
    proposed boolean NOT NULL DEFAULT false,
    chosen boolean NOT NULL DEFAULT false,
    decided_by text,
    decided_at timestamptz,
    built_at timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE answer_pairs IS
    'Candidate questions for each of one person answers, with the route that '
    'found each and which one was proposed or chosen. A correction is stored '
    'against the candidates it corrected, so the ranking can be measured '
    'rather than argued about.';

CREATE INDEX IF NOT EXISTS answer_pairs_answer_idx
    ON answer_pairs (answer_ledger_id, rank);
CREATE INDEX IF NOT EXISTS answer_pairs_person_idx
    ON answer_pairs (person_id, answer_at DESC);
-- Finding the answers nobody has decided on yet, which is the review queue.
CREATE INDEX IF NOT EXISTS answer_pairs_undecided_idx
    ON answer_pairs (person_id, answer_at DESC)
    WHERE decided_at IS NULL;
-- One chosen question per answer. The database holds the rule rather than
-- trusting every writer to remember it.
CREATE UNIQUE INDEX IF NOT EXISTS answer_pairs_one_choice_idx
    ON answer_pairs (answer_ledger_id)
    WHERE chosen;
