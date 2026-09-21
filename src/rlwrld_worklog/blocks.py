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
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Sequence

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
_THREADS_SQL = """
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
           message.source_entity_id,
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
           message.source_entity_id = his_threads.parent_ts
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

    with psycopg.connect(database_url) as connection:
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
