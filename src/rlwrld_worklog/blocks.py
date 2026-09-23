"""Cut a channel's messages into the conversations he took part in.

The unit was wrong before this. One Slack line embedded on its own is a few
words with no context, and 63,092 of those make a space where the nearest
neighbour of any question is noise -- measured, not suspected (2026-09-21:
"배포를 사람이 직접 돌리고 있다" matched "퇴근하고 운동중입니다").

A block is a contiguous run of messages in one channel: consecutive in time,
broken when the room goes quiet for longer than `gap_minutes`, and capped at
`max_messages` so one busy afternoon does not become a single document. Only
runs he spoke in are kept, because a conversation he was not in is not a
precedent for anything.

Each block holds two texts. What other people said is the key -- a question
describes a situation, and that is what the situation text is compared
against. What he said is the payload. Keeping them apart is the whole point:
the earlier version compared situations against his answers, which is matching
a key to a key.

Nothing here is generated. The block text is the messages' own words, in
order, with the speaker's name, and the raw payload remains the record.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Sequence

from .slack_ids import ts_expr

# When a room goes quiet for this long, the next message starts a new
# conversation. Half an hour: long enough that a slow exchange stays one
# block, short enough that this morning and this afternoon are two.
GAP_MINUTES = int(__import__("os").environ.get("WORKLOG_BLOCK_GAP_MINUTES", "30"))

# A ceiling, so a busy channel does not produce one document per day. Also
# keeps a block inside the embedding model's sequence length.
MAX_MESSAGES = int(__import__("os").environ.get("WORKLOG_BLOCK_MAX_MESSAGES", "12"))

# The uuid namespace for block ids. Derived from the channel and the first
# message, so rebuilding under the same rule replaces rather than duplicates.
_NAMESPACE = uuid.UUID("2f6f4d1e-8c1a-4f2b-9e3d-5a7b1c2d3e4f")


@contextmanager
def _needs_migration():
    """Turn "relation does not exist" into the one command that fixes it.

    The tables arrive with a migration, and a migration is a separate step
    that a person has to run. On 2026-09-21 that step was left out of the
    instructions and three commands answered with tracebacks -- which says
    what broke and not what to do, and the difference is a round trip.
    """
    try:
        yield
    except Exception as error:
        if type(error).__name__ != "UndefinedTable":
            raise
        raise SystemExit(
            "이 명령이 쓰는 표가 아직 없음 (0010_conversation_blocks). "
            "먼저: .venv/bin/worklog ledger-migrate --apply"
        ) from error


def block_id_for(
    channel: str, first_ts: str, *, gap: int, cap: int, kind: str = "window"
) -> uuid.UUID:
    """Stable identity: same channel, same first message, same rule, same kind.

    A thread block and a window block can start at the same message and are
    still two different readings of it, so the kind is part of the identity.
    """
    return uuid.uuid5(_NAMESPACE, f"{kind}|{channel}|{first_ts}|{gap}|{cap}")


def render(messages: Sequence[dict[str, Any]], names: dict[str, str]) -> str:
    """The conversation as a person would read it.

    Names rather than handles, because `U07EKRU6F7H: 배포 됐나요` embeds the id
    as if it were a word. An unknown handle keeps its id rather than becoming
    a plausible-looking name.
    """
    lines = []
    for message in messages:
        author = str(message.get("author") or "")
        who = names.get(author, author or "누군가")
        text = " ".join(str(message.get("text") or "").split())
        if text:
            lines.append(f"{who}: {text}")
    return "\n".join(lines)


def cut(
    messages: Sequence[dict[str, Any]],
    *,
    gap_minutes: int = GAP_MINUTES,
    max_messages: int = MAX_MESSAGES,
) -> list[list[dict[str, Any]]]:
    """Split one channel's messages, in time order, into runs.

    A run breaks on a quiet gap or at the cap. Pure function, so the rule can
    be argued about in a test rather than in a database.
    """
    runs: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    previous: datetime | None = None
    for message in messages:
        when = message.get("at")
        if not isinstance(when, datetime):
            continue
        quiet = previous is not None and (when - previous).total_seconds() > gap_minutes * 60
        if current and (quiet or len(current) >= max_messages):
            runs.append(current)
            current = []
        current.append(message)
        previous = when
    if current:
        runs.append(current)
    return runs


@dataclass
class BlockResult:
    dry_run: bool = True
    channels: int = 0
    messages: int = 0
    blocks: int = 0
    blocks_with_him: int = 0
    # Threads followed from one of his replies back to the message that
    # started them. Counted separately because a thread block is reached by
    # structure and a window block by time, and one number hiding both would
    # not say whether following threads found anything.
    thread_blocks: int = 0
    threads_without_parent: int = 0
    seconds: float = 0.0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "channels": self.channels,
            "messages": self.messages,
            "blocks": self.blocks,
            # The ones kept. A conversation he was not in is not a precedent,
            # and the difference between the two numbers says how much of the
            # room's traffic he is actually in.
            "blocks_with_him": self.blocks_with_him,
            "thread_blocks": self.thread_blocks,
            # Threads whose starting message is not in the ledger: the orphans
            # the nightly sweep is recovering. Reported, because a thread with
            # no parent has no situation and is silently unusable otherwise.
            "threads_without_parent": self.threads_without_parent,
            "gap_minutes": GAP_MINUTES,
            "max_messages": MAX_MESSAGES,
            "seconds": self.seconds,
            "errors": self.errors[:20],
        }


_CHANNELS_SQL = """
    SELECT DISTINCT coalesce(scope->>'channel_id', scope->>'container') AS channel
      FROM ledger_records
     WHERE source = 'slack'
       AND relations->>'author_user_id' = ANY(%(handles)s)
       AND coalesce(scope->>'channel_id', scope->>'container') IS NOT NULL
"""

# One channel at a time, newest first. Whole-table sorts over half a million
# rows are what made the previous scoping query unusable, and a channel is
# small enough to sort in memory.
_MESSAGES_SQL = """
    SELECT DISTINCT ON (source_entity_id)
           source_entity_id,
           source_created_at,
           relations->>'author_user_id' AS author,
           raw_payload->>'text' AS text,
           raw_payload->>'permalink' AS permalink
      FROM ledger_records
     WHERE source = 'slack'
       AND entity_type = 'message'
       AND coalesce(scope->>'channel_id', scope->>'container') = %(channel)s
       AND coalesce(raw_payload->>'text', '') <> ''
     ORDER BY source_entity_id, collected_at DESC
"""

# Threads he replied in, followed from his reply to the message that started
# it. `thread_id` is the parent's ts, and the parent can be any distance back
# in time -- which is exactly what a time window cannot reach.
_THREADS_SQL = f"""
    WITH his_threads AS (
        SELECT DISTINCT
               coalesce(scope->>'channel_id', scope->>'container') AS channel,
               coalesce(relations->>'thread_id', relations->>'parent_ts') AS parent_ts
          FROM ledger_records
         WHERE source = 'slack'
           AND entity_type = 'message'
           AND relations->>'author_user_id' = ANY(%(handles)s)
           AND coalesce(relations->>'thread_id', relations->>'parent_ts') IS NOT NULL
    )
    SELECT DISTINCT ON (his_threads.channel, his_threads.parent_ts, message.source_entity_id)
           his_threads.channel,
           his_threads.parent_ts,
           {ts_expr("message.source_entity_id")} AS ts,
           message.source_created_at,
           message.relations->>'author_user_id' AS author,
           message.raw_payload->>'text' AS text,
           message.raw_payload->>'permalink' AS permalink
      FROM his_threads
      JOIN ledger_records message
        ON message.source = 'slack'
       AND message.entity_type = 'message'
       AND coalesce(message.scope->>'channel_id', message.scope->>'container')
           = his_threads.channel
       AND (
           -- the message that started the thread ...
           {ts_expr("message.source_entity_id")} = his_threads.parent_ts
           -- ... and every reply in it, his own included
           OR coalesce(message.relations->>'thread_id', message.relations->>'parent_ts')
              = his_threads.parent_ts
       )
       AND coalesce(message.raw_payload->>'text', '') <> ''
     ORDER BY his_threads.channel, his_threads.parent_ts, message.source_entity_id,
              message.collected_at DESC
"""

_WRITE_SQL = """
    INSERT INTO conversation_blocks (
        block_id, source, channel, person_id, started_at, ended_at,
        gap_minutes, max_messages, message_count, speaker_count,
        situation_text, his_text, his_first_at, permalink, kind
    ) VALUES (
        %(block_id)s, 'slack', %(channel)s, %(person_id)s, %(started_at)s,
        %(ended_at)s, %(gap_minutes)s, %(max_messages)s, %(message_count)s,
        %(speaker_count)s, %(situation_text)s, %(his_text)s, %(his_first_at)s,
        %(permalink)s, %(kind)s
    )
    ON CONFLICT (block_id) DO UPDATE SET
        situation_text = excluded.situation_text,
        his_text = excluded.his_text,
        message_count = excluded.message_count,
        speaker_count = excluded.speaker_count,
        ended_at = excluded.ended_at,
        his_first_at = excluded.his_first_at,
        permalink = excluded.permalink,
        built_at = now(),
        -- A person's judgement survives a rebuild. The text may be recut and
        -- the vector is dropped, but "this pair is not a precedent" was a
        -- decision about the pair, and bringing it back as pending would ask
        -- the same question again.
        -- A rewritten block is a different document, so its vector is no
        -- longer about its text. Clearing it is what makes the embedding
        -- batch pick it up again; keeping it would leave a stale key that
        -- still ranks.
        embedding = NULL,
        embedding_model = NULL,
        embedded_at = NULL
"""


def build_blocks(
    database_url: str,
    person_id: str,
    *,
    names: dict[str, str] | None = None,
    gap_minutes: int = GAP_MINUTES,
    max_messages: int = MAX_MESSAGES,
    apply: bool = False,
) -> BlockResult:
    """Cut every channel he speaks in into blocks, and keep the ones with him."""
    from datetime import timezone

    import psycopg

    began = datetime.now(timezone.utc)
    result = BlockResult(dry_run=not apply)

    from .reconcile import person_handles

    with _needs_migration(), psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            handles = person_handles(cursor, person_id)
            cursor.execute(_CHANNELS_SQL, {"handles": handles})
            channels = [str(row[0]) for row in cursor.fetchall()]
        result.channels = len(channels)
        handle_set = set(handles)
        lookup = names or {}

        for channel in channels:
            with connection.cursor() as cursor:
                cursor.execute(_MESSAGES_SQL, {"channel": channel})
                rows = cursor.fetchall()
            messages = sorted(
                (
                    {
                        "ts": str(row[0]),
                        "at": row[1],
                        "author": row[2],
                        "text": row[3],
                        "permalink": row[4],
                    }
                    for row in rows
                    if row[1] is not None
                ),
                key=lambda item: item["at"],
            )
            result.messages += len(messages)

            for run in cut(messages, gap_minutes=gap_minutes, max_messages=max_messages):
                result.blocks += 1
                his = [item for item in run if str(item["author"]) in handle_set]
                if not his:
                    continue
                others = [item for item in run if str(item["author"]) not in handle_set]
                if not others:
                    # He spoke into an empty room, or to himself. There is no
                    # situation here, so there is nothing to match against.
                    continue
                result.blocks_with_him += 1
                if not apply:
                    continue
                with connection.cursor() as cursor:
                    cursor.execute(
                        _WRITE_SQL,
                        {
                            "block_id": block_id_for(
                                channel,
                                run[0]["ts"],
                                gap=gap_minutes,
                                cap=max_messages,
                                kind="window",
                            ),
                            "channel": channel,
                            "person_id": person_id,
                            "started_at": run[0]["at"],
                            "ended_at": run[-1]["at"],
                            "gap_minutes": gap_minutes,
                            "max_messages": max_messages,
                            "message_count": len(run),
                            "speaker_count": len({str(item["author"]) for item in run}),
                            "situation_text": render(others, lookup),
                            "his_text": render(his, lookup),
                            "his_first_at": his[0]["at"],
                            "permalink": his[0]["permalink"],
                            "kind": "window",
                        },
                    )
            if apply:
                # Per channel, so an interrupted run keeps the channels it
                # finished rather than starting over.
                connection.commit()

        # Threads, followed rather than guessed at. HK: 내가 쓴글이 댓글이면
        # 원글을 찾고... A thread's parent can be days before the reply, so no
        # window around his message reaches it; `thread_id` says exactly which
        # message he was answering, and this is the one path in the system that
        # needs no inference at all.
        with connection.cursor() as cursor:
            cursor.execute(_THREADS_SQL, {"handles": handles})
            thread_rows = cursor.fetchall()

        threads: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for row in thread_rows:
            if row[3] is None:
                continue
            threads.setdefault((str(row[0]), str(row[1])), []).append(
                {
                    "ts": str(row[2]),
                    "at": row[3],
                    "author": row[4],
                    "text": row[5],
                    "permalink": row[6],
                }
            )

        for (channel, parent_ts), messages in threads.items():
            messages.sort(key=lambda item: item["at"])
            his = [item for item in messages if str(item["author"]) in handle_set]
            others = [item for item in messages if str(item["author"]) not in handle_set]
            if not his:
                continue
            if not any(item["ts"] == parent_ts for item in messages):
                # The message that started the thread is not in the ledger --
                # one of the orphans the nightly sweep is recovering. His
                # reply is here and the question it answered is not, so there
                # is no situation to match and the count says so.
                result.threads_without_parent += 1
                continue
            if not others:
                continue
            result.thread_blocks += 1
            if not apply:
                continue
            # Capped like a window block: a 200-reply thread is not one
            # situation, and the model cannot read it in one go either.
            kept = messages[: max_messages * 2]
            kept_his = [item for item in kept if str(item["author"]) in handle_set]
            kept_others = [item for item in kept if str(item["author"]) not in handle_set]
            if not kept_his or not kept_others:
                continue
            with connection.cursor() as cursor:
                cursor.execute(
                    _WRITE_SQL,
                    {
                        "block_id": block_id_for(
                            channel,
                            parent_ts,
                            gap=gap_minutes,
                            cap=max_messages,
                            kind="thread",
                        ),
                        "channel": channel,
                        "person_id": person_id,
                        "started_at": kept[0]["at"],
                        "ended_at": kept[-1]["at"],
                        "gap_minutes": gap_minutes,
                        "max_messages": max_messages,
                        "message_count": len(kept),
                        "speaker_count": len({str(item["author"]) for item in kept}),
                        "situation_text": render(kept_others, lookup),
                        "his_text": render(kept_his, lookup),
                        "his_first_at": kept_his[0]["at"],
                        "permalink": kept_his[0]["permalink"],
                        "kind": "thread",
                    },
                )
        if apply:
            connection.commit()

    result.seconds = round((datetime.now(timezone.utc) - began).total_seconds(), 1)
    return result


# ---------------------------------------------------------------- review

_REVIEW_LIST_SQL = """
    SELECT block_id::text,
           kind,
           channel,
           started_at,
           his_first_at,
           message_count,
           speaker_count,
           situation_text,
           his_text,
           permalink,
           review,
           reviewed_by,
           reviewed_at,
           review_note
      FROM conversation_blocks
     WHERE person_id = %(person_id)s
       AND (%(review)s = 'all' OR review = %(review)s)
     ORDER BY started_at DESC
     LIMIT %(limit)s OFFSET %(offset)s
"""

_REVIEW_COUNTS_SQL = """
    SELECT review, count(*)
      FROM conversation_blocks
     WHERE person_id = %(person_id)s
     GROUP BY review
"""

_REVIEW_SET_SQL = """
    UPDATE conversation_blocks
       SET review = %(review)s,
           reviewed_by = %(actor)s,
           reviewed_at = now(),
           review_note = %(note)s
     WHERE block_id = %(block_id)s::uuid
"""

REVIEW_STATES = ("pending", "kept", "dropped")


def list_blocks(
    database_url: str,
    person_id: str,
    *,
    review: str = "pending",
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """Blocks for a person to judge, newest first, with the counts beside them.

    The counts are what turn a review queue into a finite task: 'pending: 800'
    is a different afternoon from 'pending: 40', and the difference should be
    visible before starting rather than discovered halfway.
    """
    import psycopg

    if review not in REVIEW_STATES + ("all",):
        raise ValueError(f"unknown review state {review!r}")

    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                _REVIEW_LIST_SQL,
                {
                    "person_id": person_id,
                    "review": review,
                    "limit": max(1, min(int(limit), 200)),
                    "offset": max(0, int(offset)),
                },
            )
            rows = cursor.fetchall()
            cursor.execute(_REVIEW_COUNTS_SQL, {"person_id": person_id})
            counts = {str(row[0]): int(row[1]) for row in cursor.fetchall()}

    return {
        "counts": {state: counts.get(state, 0) for state in REVIEW_STATES},
        "blocks": [
            {
                "block_id": row[0],
                "kind": row[1],
                "channel": row[2],
                "started_at": row[3].isoformat() if row[3] else None,
                "his_first_at": row[4].isoformat() if row[4] else None,
                "message_count": row[5],
                "speaker_count": row[6],
                "situation_text": row[7],
                "his_text": row[8],
                "permalink": row[9],
                "review": row[10],
                "reviewed_by": row[11],
                "reviewed_at": row[12].isoformat() if row[12] else None,
                "review_note": row[13],
            }
            for row in rows
        ],
    }


def set_review(
    database_url: str,
    block_id: str,
    review: str,
    *,
    actor: str,
    note: str | None = None,
) -> bool:
    """Record one judgement. Returns False when the block is not there.

    Never deletes. A dropped block stays, so a rebuild cannot resurrect a pair
    somebody has already ruled out -- and so the ruling itself remains as
    evidence about what counts as a precedent.
    """
    import psycopg

    if review not in REVIEW_STATES:
        raise ValueError(f"unknown review state {review!r}")

    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                _REVIEW_SET_SQL,
                {
                    "block_id": block_id,
                    "review": review,
                    "actor": actor,
                    "note": note,
                },
            )
            changed = cursor.rowcount
        connection.commit()
    return bool(changed)


# ------------------------------------------------------- candidate pairing

# A Slack permalink, which is how a quoted message arrives. HK, 2026-09-21:
# 쓰레드를 인용/옮겨서 댓글을 다는 경우도 있어서. The link names the message
# exactly, so this is structure rather than inference -- the same class of
# clue as `thread_id`, and it is sitting in the text of his own message.
_PERMALINK = __import__("re").compile(
    r"/archives/([A-Z0-9]+)/p(\d{10})(\d{6})"
)


def quoted_refs(text: str | None) -> list[tuple[str, str]]:
    """(channel, ts) for every Slack message his own text links to.

    `p1789638401411289` is the ts with its dot removed, so it goes back in:
    1789638401.411289. Getting that wrong produces a ts that matches nothing
    and looks exactly like "no quote", which is why it has a test.
    """
    if not isinstance(text, str):
        return []
    found = []
    for channel, seconds, fraction in _PERMALINK.findall(text):
        pair = (channel, f"{seconds}.{fraction}")
        if pair not in found:
            found.append(pair)
    return found


# How much each route is trusted, before a person says otherwise. These are
# starting points, not findings: a thread link is Slack's own record, a quoted
# permalink is his own pointer, and a nearby message is this system guessing.
# His corrections are stored against the candidates they corrected, so the
# order can later be measured instead of asserted.
BASIS_SCORE = {"thread": 1.0, "quote": 0.9, "window": 0.6}

_ANSWERS_SQL = """
    SELECT DISTINCT ON (answer.source_entity_id)
           answer.ledger_id,
           answer.source_entity_id,
           answer.source_created_at,
           coalesce(answer.scope->>'channel_id', answer.scope->>'container') AS channel,
           answer.raw_payload->>'text' AS text,
           answer.raw_payload->>'permalink' AS permalink,
           coalesce(answer.relations->>'thread_id', answer.relations->>'parent_ts') AS parent_ts
      FROM ledger_records answer
     WHERE answer.source = 'slack'
       AND answer.entity_type = 'message'
       AND answer.relations->>'author_user_id' = ANY(%(handles)s)
       AND coalesce(answer.raw_payload->>'text', '') <> ''
       AND answer.source_created_at >= %(since)s
     ORDER BY answer.source_entity_id, answer.collected_at DESC
"""

_ONE_MESSAGE_SQL = f"""
    SELECT DISTINCT ON (source_entity_id)
           source_entity_id,
           relations->>'author_user_id' AS author,
           raw_payload->>'text' AS text
      FROM ledger_records
     WHERE source = 'slack' AND entity_type = 'message'
       AND coalesce(scope->>'channel_id', scope->>'container') = %(channel)s
       AND {ts_expr("source_entity_id")} = %(ts)s
     ORDER BY source_entity_id, collected_at DESC
"""

# The messages other people sent just before he spoke, nearest first. Not one
# message: HK, 2026-09-21: 여럿이 오갈 경우, 바로 직전 대화가 아닐 수는 있어.
# So several are offered and he picks.
_NEARBY_SQL = """
    SELECT source_entity_id,
           relations->>'author_user_id' AS author,
           raw_payload->>'text' AS text,
           source_created_at
      FROM ledger_records
     WHERE source = 'slack' AND entity_type = 'message'
       AND coalesce(scope->>'channel_id', scope->>'container') = %(channel)s
       AND relations->>'author_user_id' <> ALL(%(handles)s)
       AND coalesce(raw_payload->>'text', '') <> ''
       AND source_created_at < %(before)s
       AND source_created_at > %(before)s - make_interval(mins => %(window)s)
     ORDER BY source_created_at DESC
     LIMIT %(nearby)s
"""

_PAIR_WRITE_SQL = """
    INSERT INTO answer_pairs (
        pair_id, person_id, answer_ledger_id, answer_text, answer_at, channel,
        permalink, candidate_text, basis, score, rank, proposed
    ) VALUES (
        %(pair_id)s, %(person_id)s, %(answer_ledger_id)s, %(answer_text)s,
        %(answer_at)s, %(channel)s, %(permalink)s, %(candidate_text)s,
        %(basis)s, %(score)s, %(rank)s, %(proposed)s
    )
    ON CONFLICT (pair_id) DO UPDATE SET
        candidate_text = excluded.candidate_text,
        score = excluded.score,
        rank = excluded.rank,
        -- `proposed` is this system's guess and may change as the ranking
        -- changes. `chosen` is a person's decision and is never overwritten
        -- by a rebuild: that is the whole point of keeping the two apart.
        proposed = excluded.proposed
"""

# How many nearby messages to offer per answer. Enough that the real question
# is usually among them, few enough that reviewing one answer is a glance.
NEARBY_CANDIDATES = 3


@dataclass
class PairResult:
    dry_run: bool = True
    answers: int = 0
    candidates: int = 0
    by_basis: dict[str, int] = field(default_factory=dict)
    answers_without_candidate: int = 0
    seconds: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "answers": self.answers,
            "candidates": self.candidates,
            "by_basis": dict(sorted(self.by_basis.items())),
            # Answers nothing could be paired with: no thread link, no quote,
            # and nobody else speaking in the minutes before. Reported, because
            # they are the ones a person cannot review and the agent cannot use.
            "answers_without_candidate": self.answers_without_candidate,
            "seconds": self.seconds,
        }


def propose_pairs(
    database_url: str,
    person_id: str,
    *,
    since: datetime | None = None,
    names: dict[str, str] | None = None,
    apply: bool = False,
) -> PairResult:
    """For each of his answers, the questions it might have been answering.

    Three routes, offered together and ranked rather than resolved: the thread
    Slack links it to, a permalink he quoted, and the messages other people
    sent just before. A person picks, and the pick is stored next to what was
    proposed -- which is the only way to tell later whether the proposing is
    getting better.
    """
    from datetime import timedelta, timezone

    import psycopg

    from .voice import ANSWER_WINDOW_MINUTES

    began = datetime.now(timezone.utc)
    result = PairResult(dry_run=not apply)
    lookup = names or {}
    since = since or (datetime.now(timezone.utc) - timedelta(days=365))

    from .reconcile import person_handles

    with _needs_migration(), psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            handles = person_handles(cursor, person_id)
            cursor.execute(_ANSWERS_SQL, {"handles": handles, "since": since})
            answers = cursor.fetchall()

        for ledger_id, ts, said_at, channel, text, permalink, parent_ts in answers:
            if not channel or said_at is None:
                continue
            result.answers += 1
            candidates: list[dict[str, Any]] = []

            def add(basis: str, other_ts: str, author: Any, other_text: Any) -> None:
                if not other_text or str(other_ts) == str(ts):
                    return
                who = lookup.get(str(author), str(author or "누군가"))
                candidates.append(
                    {
                        "basis": basis,
                        "ts": str(other_ts),
                        "text": f"{who}: {' '.join(str(other_text).split())}",
                        "score": BASIS_SCORE[basis],
                    }
                )

            with connection.cursor() as cursor:
                if parent_ts:
                    cursor.execute(
                        _ONE_MESSAGE_SQL, {"channel": channel, "ts": str(parent_ts)}
                    )
                    row = cursor.fetchone()
                    if row:
                        add("thread", row[0], row[1], row[2])

                for quoted_channel, quoted_ts in quoted_refs(text):
                    cursor.execute(
                        _ONE_MESSAGE_SQL, {"channel": quoted_channel, "ts": quoted_ts}
                    )
                    row = cursor.fetchone()
                    if row:
                        add("quote", row[0], row[1], row[2])

                cursor.execute(
                    _NEARBY_SQL,
                    {
                        "channel": channel,
                        "handles": handles,
                        "before": said_at,
                        "window": ANSWER_WINDOW_MINUTES,
                        "nearby": NEARBY_CANDIDATES,
                    },
                )
                for index, row in enumerate(cursor.fetchall()):
                    # Each step further back is a little less likely to be the
                    # thing he answered, and a lot less certain once other
                    # people have spoken in between.
                    penalty = 0.05 * index
                    before = len(candidates)
                    add("window", row[0], row[1], row[2])
                    if len(candidates) > before:
                        candidates[-1]["score"] -= penalty

            if not candidates:
                result.answers_without_candidate += 1
                continue

            candidates.sort(key=lambda item: item["score"], reverse=True)
            for rank, candidate in enumerate(candidates):
                result.candidates += 1
                result.by_basis[candidate["basis"]] = (
                    result.by_basis.get(candidate["basis"], 0) + 1
                )
                if not apply:
                    continue
                with connection.cursor() as cursor:
                    cursor.execute(
                        _PAIR_WRITE_SQL,
                        {
                            "pair_id": uuid.uuid5(
                                _NAMESPACE,
                                f"pair|{ts}|{candidate['basis']}|{candidate['ts']}",
                            ),
                            "person_id": person_id,
                            "answer_ledger_id": ledger_id,
                            "answer_text": " ".join(str(text).split()),
                            "answer_at": said_at,
                            "channel": channel,
                            "permalink": permalink,
                            "candidate_text": candidate["text"],
                            "basis": candidate["basis"],
                            "score": candidate["score"],
                            "rank": rank,
                            "proposed": rank == 0,
                        },
                    )
            if apply:
                connection.commit()

    result.seconds = round((datetime.now(timezone.utc) - began).total_seconds(), 1)
    return result


_PAIR_QUEUE_SQL = """
    SELECT answer_ledger_id::text,
           answer_text,
           answer_at,
           channel,
           permalink,
           json_agg(
               json_build_object(
                   'pair_id', pair_id::text,
                   'text', candidate_text,
                   'basis', basis,
                   'score', round(score::numeric, 3),
                   'rank', rank,
                   'proposed', proposed,
                   'chosen', chosen
               ) ORDER BY rank
           ) AS candidates,
           bool_or(decided_at IS NOT NULL) AS decided
      FROM answer_pairs
     WHERE person_id = %(person_id)s
     GROUP BY answer_ledger_id, answer_text, answer_at, channel, permalink
    HAVING (%(state)s = 'all')
        OR (%(state)s = 'undecided' AND NOT bool_or(decided_at IS NOT NULL))
        OR (%(state)s = 'decided' AND bool_or(decided_at IS NOT NULL))
     ORDER BY answer_at DESC
     LIMIT %(limit)s OFFSET %(offset)s
"""

_PAIR_COUNTS_SQL = """
    SELECT count(*) FILTER (WHERE decided) AS decided,
           count(*) FILTER (WHERE NOT decided) AS undecided
      FROM (
          SELECT answer_ledger_id, bool_or(decided_at IS NOT NULL) AS decided
            FROM answer_pairs
           WHERE person_id = %(person_id)s
           GROUP BY answer_ledger_id
      ) AS per_answer
"""

PAIR_STATES = ("undecided", "decided", "all")


def pair_queue(
    database_url: str,
    person_id: str,
    *,
    state: str = "undecided",
    limit: int = 25,
    offset: int = 0,
) -> dict[str, Any]:
    """His answers with their candidate questions, newest first.

    One row per answer, candidates nested in proposal order, so reviewing is
    reading an answer and picking from a short list rather than judging rows
    one at a time.
    """
    import psycopg

    if state not in PAIR_STATES:
        raise ValueError(f"unknown state {state!r}")

    with _needs_migration(), psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                _PAIR_QUEUE_SQL,
                {
                    "person_id": person_id,
                    "state": state,
                    "limit": max(1, min(int(limit), 100)),
                    "offset": max(0, int(offset)),
                },
            )
            rows = cursor.fetchall()
            cursor.execute(_PAIR_COUNTS_SQL, {"person_id": person_id})
            counts = cursor.fetchone() or (0, 0)

    return {
        "counts": {"decided": int(counts[0] or 0), "undecided": int(counts[1] or 0)},
        "answers": [
            {
                "answer_ledger_id": row[0],
                "answer_text": row[1],
                "answer_at": row[2].isoformat() if row[2] else None,
                "channel": row[3],
                "permalink": row[4],
                "candidates": row[5],
                "decided": bool(row[6]),
            }
            for row in rows
        ],
    }


def choose_pair(
    database_url: str,
    answer_ledger_id: str,
    pair_id: str | None,
    *,
    actor: str,
) -> bool:
    """Record which candidate was the question -- or that none of them was.

    `pair_id` None means "none of these": the answer is marked decided with
    nothing chosen, so it leaves the queue and never becomes a precedent. That
    is a real answer and the most common correction a person can make, so it
    has to be one click rather than an omission.
    """
    import psycopg

    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM answer_pairs WHERE answer_ledger_id = %s::uuid",
                (answer_ledger_id,),
            )
            if not int(cursor.fetchone()[0]):
                return False
            # Cleared first, because the unique index allows one chosen row per
            # answer and a correction is a move rather than an addition.
            cursor.execute(
                "UPDATE answer_pairs SET chosen = false "
                " WHERE answer_ledger_id = %s::uuid",
                (answer_ledger_id,),
            )
            if pair_id:
                cursor.execute(
                    "UPDATE answer_pairs SET chosen = true "
                    " WHERE pair_id = %s::uuid AND answer_ledger_id = %s::uuid",
                    (pair_id, answer_ledger_id),
                )
                if not cursor.rowcount:
                    connection.rollback()
                    return False
            cursor.execute(
                "UPDATE answer_pairs SET decided_by = %s, decided_at = now() "
                " WHERE answer_ledger_id = %s::uuid",
                (actor, answer_ledger_id),
            )
        connection.commit()
    return True


def agreement(database_url: str, person_id: str) -> dict[str, Any]:
    """How often the proposal was the one he chose.

    The measurement the correction data exists for. Nothing here tunes the
    ranking automatically -- that would be a model trained on a few hundred
    clicks -- but "the top candidate was right 71% of the time, and when it was
    wrong the answer was usually the thread parent" is a fact the ranking can
    be changed against, deliberately.
    """
    import psycopg

    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT count(*) FILTER (WHERE chosen AND proposed) AS agreed,
                       count(*) FILTER (WHERE chosen AND NOT proposed) AS corrected,
                       count(DISTINCT answer_ledger_id) FILTER (
                           WHERE decided_at IS NOT NULL
                       ) AS decided,
                       count(DISTINCT answer_ledger_id) FILTER (
                           WHERE decided_at IS NOT NULL AND NOT EXISTS (
                               SELECT 1 FROM answer_pairs inner_pairs
                                WHERE inner_pairs.answer_ledger_id
                                      = answer_pairs.answer_ledger_id
                                  AND inner_pairs.chosen
                           )
                       ) AS none_of_these
                  FROM answer_pairs
                 WHERE person_id = %s
                """,
                (person_id,),
            )
            agreed, corrected, decided, none_of_these = cursor.fetchone()
            cursor.execute(
                "SELECT basis, count(*) FROM answer_pairs "
                " WHERE person_id = %s AND chosen GROUP BY basis",
                (person_id,),
            )
            chosen_basis = {str(row[0]): int(row[1]) for row in cursor.fetchall()}

    agreed, corrected = int(agreed or 0), int(corrected or 0)
    total = agreed + corrected
    return {
        "decided_answers": int(decided or 0),
        "agreed": agreed,
        "corrected": corrected,
        # None, not 0.0, when nothing has been decided yet: a rate over zero
        # decisions is not a low rate, it is an unanswered question.
        "agreement_rate": round(agreed / total, 3) if total else None,
        "none_of_these": int(none_of_these or 0),
        "chosen_basis": dict(sorted(chosen_basis.items())),
    }
