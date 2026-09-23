"""The one place that knows a Slack row's id is not its timestamp.

Measured on 2026-09-22, on HK's database: `blocks --apply` found 1,674 threads
he had replied in and built 0 thread blocks -- every single one reported as
"the message that started it is missing". A hundred percent is never a data
problem. The converters write

    source_entity_id = f"{workspace_id}:{channel_id}:{ts}"

(legacy_slack.py, live.py) while `relations.parent_ts` and
`relations.thread_id` hold the bare ts, so every query that joined one to the
other matched nothing and said "absent" when it meant "spelled differently".

Four queries carried that join -- the thread walk and the quoted-permalink
lookup in blocks.py, the precedent parent in voice.py, and the orphan seed in
slack_sweep.py. The last one is the expensive one: the nightly sweep has been
re-fetching threads the ledger already holds, because every reply looked like
an orphan.

So the comparison lives here once, spelled as "everything after the last
colon", which is the ts for a composite id and the whole string for a bare
one. Both shapes exist: the tests write bare ids, and a converter could
change again.
"""

from __future__ import annotations


def ts_expr(column: str) -> str:
    """SQL for the Slack ts inside a `source_entity_id`.

    Matched by the expression index in migration 0011, so a join on it is an
    index lookup rather than a scan of half a million rows.
    """
    return f"regexp_replace({column}, '^.*:', '')"


def ts_of(source_entity_id: str) -> str:
    """The Python side of the same rule, for ids already in hand."""
    return source_entity_id.rsplit(":", 1)[-1]
