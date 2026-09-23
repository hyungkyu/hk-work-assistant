"""Recover Slack replies whose parent thread is missing from the ledger.

The gap this closes (HK, 2026-09-14): an ordinary reply — not a mention, not a
broadcast — posted inside a collection window to a thread whose parent predates
every window. `conversations.history` omits it (the parent is out of range),
`search.messages` never returns it (it is not a mention), and pre-window parent
discovery cannot reach a parent older than its scan floor. So the reply lands in
the ledger with a `parent_ts` pointing at a message that has no row of its own —
an orphan.

There is exactly one path that reaches such a thread regardless of the parent's
age: ask Slack for the thread by its parent ts (`conversations.replies`). This
module finds the orphaned parents in the ledger and hands them to the collector
as a seed list. It reads the ledger only; the fetching, archiving and loading
are the ordinary collector and load pipeline, unchanged.

One-time by nature: once the parents are recorded, they are no longer orphans,
so a second run finds fewer, and eventually none.
"""

from __future__ import annotations

from typing import Any

from .slack_ids import ts_expr

# A reply names its parent in `relations.parent_ts`; the parent, if present,
# is a row whose `source_entity_id` equals that ts. An orphan is a reply whose
# parent ts matches no row. `scope.container` is the channel the thread lives
# in, which is what `conversations.replies` needs alongside the ts.
_ORPHAN_SQL = f"""
    SELECT DISTINCT reply.scope->>'container' AS channel,
                    reply.relations->>'parent_ts' AS parent_ts
      FROM ledger_records reply
     WHERE reply.source = 'slack'
       AND (reply.relations->>'is_thread_reply')::boolean IS TRUE
       AND reply.relations->>'parent_ts' IS NOT NULL
       AND reply.scope->>'container' IS NOT NULL
       AND NOT EXISTS (
           SELECT 1
             FROM ledger_records parent
            WHERE parent.source = 'slack'
              AND {ts_expr("parent.source_entity_id")} = reply.relations->>'parent_ts'
       )
"""


def orphan_thread_parents(database_url: str) -> dict[str, set[str]]:
    """{channel_id: {parent_ts, ...}} for every reply whose parent has no row.

    The seed list for a targeted sweep. Deterministic and idempotent: it is a
    plain read, and a parent recovered by one sweep drops out of the next.
    """
    import psycopg

    parents: dict[str, set[str]] = {}
    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(_ORPHAN_SQL)
            for channel, parent_ts in cursor.fetchall():
                if channel and parent_ts:
                    parents.setdefault(str(channel), set()).add(str(parent_ts))
    return parents


def summarize(parents: dict[str, set[str]]) -> dict[str, Any]:
    return {
        "channels": len(parents),
        "parents": sum(len(values) for values in parents.values()),
    }
