"""What HK actually said, last time something like this came up.

HK, 2026-09-18: 내가 어떤 상황에서 어떤 질문을 하는지, 사람들이 놓치고 있는
것에 개입하고, 챙겨야 할 요소들이 뭐가 빠졌는지를 내 에이전트에서 묻고
대답할 수 있도록 하면 좋겠어. 물론, 자상한 버전의 나.

And then, when I said the data was not there yet: 원장 슬랙만 고려하면, 어디에
뭐라고 댓글을 달았는지.. 전부 있잖아. He was right. A thread reply carries the
situation and the intervention as one pair -- the parent message is what people
were saying, his reply is what he pulled on -- and the ledger already links
them. Labelled examples, no annotation needed.

So this module does retrieval and nothing else. It returns real precedents: the
message he was answering, what he wrote, where, and when. No model runs here
and no sentence is generated; the agent that answers reads these and works from
them. That boundary is the same one the digest holds -- this system stores and
retrieves what happened, and anything written in his voice is written by the
agent, out loud, where he can see the precedent it came from.

What this cannot do, said plainly because a tool that overstates itself is
worse than no tool: it holds what he said, not why. It holds nothing he decided
not to say, and of meetings only what a note recorded. Precedent is evidence
about his questions, not a model of his judgement.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

KST_OFFSET_HOURS = 9

# His own messages, ranked against the situation, each with the message it was
# a reply to. `thread_id` is the parent's ts for a reply; `parent_ts` is the
# same value under the legacy spelling, and both appear in the ledger.
#
# The join is to the parent's own ledger row, which is why the orphaned-parent
# sweep matters here: a reply whose parent was never collected comes back with
# no situation, and is reported as such rather than dropped.
_PRECEDENT_SQL = """
    WITH mine AS (
        SELECT reply.ledger_id,
               reply.source_created_at AS said_at,
               coalesce(reply.scope->>'channel_id', reply.scope->>'container') AS channel,
               reply.raw_payload->>'text' AS said,
               reply.raw_payload->>'permalink' AS permalink,
               coalesce(
                   reply.relations->>'thread_id', reply.relations->>'parent_ts'
               ) AS parent_ts,
               word_similarity(%(query)s, reply.raw_payload->>'text') AS score
          FROM ledger_records reply
         WHERE reply.source = 'slack'
           AND reply.entity_type = 'message'
           AND reply.relations->>'author_user_id' = ANY(%(handles)s)
           AND coalesce(reply.raw_payload->>'text', '') <> ''
           AND word_similarity(%(query)s, reply.raw_payload->>'text') >= %(floor)s
    )
    SELECT DISTINCT ON (mine.said, mine.parent_ts)
           mine.said_at,
           mine.channel,
           mine.said,
           mine.permalink,
           mine.parent_ts,
           parent.raw_payload->>'text' AS situation,
           parent.relations->>'author_user_id' AS situation_author,
           mine.score
      FROM mine
      LEFT JOIN ledger_records parent
        ON parent.source = 'slack'
       AND parent.entity_type = 'message'
       AND parent.source_entity_id = mine.parent_ts
     ORDER BY mine.said, mine.parent_ts, mine.score DESC
"""

# Trigram similarity, not a tsvector. Postgres ships no Korean stemmer, so the
# `simple` dictionary tokenises on whitespace and `배포` fails to match
# `배포는` -- the particle is part of the token. A test caught it on the first
# real sentence tried, which is the argument for writing the test with his
# actual words in it. `word_similarity` scores the query against the best
# matching run of words inside the message rather than against the whole of
# it, which is what "this message is about that" means for a long message.
# The same reasoning search.py already arrived at for its fallback matcher.
MATCH_FLOOR = 0.3

# The same query, ranked by meaning instead of by characters. Requires the
# embedding batch to have run; when it has not, `precedents` falls back to the
# trigram query above and says which matcher answered, because "no precedent"
# and "no embedding yet" are different answers.
#
# `<=>` is cosine distance, so smaller is closer; the score is flipped to
# 1 - distance to keep "higher is better" true for both matchers.
_PRECEDENT_VECTOR_SQL = """
    WITH mine AS (
        SELECT reply.ledger_id,
               reply.source_created_at AS said_at,
               coalesce(reply.scope->>'channel_id', reply.scope->>'container') AS channel,
               reply.raw_payload->>'text' AS said,
               reply.raw_payload->>'permalink' AS permalink,
               coalesce(
                   reply.relations->>'thread_id', reply.relations->>'parent_ts'
               ) AS parent_ts,
               1 - (text.embedding <=> %(vector)s::vector) AS score
          FROM ledger_records reply
          JOIN search_documents text ON text.ledger_id = reply.ledger_id
         WHERE reply.source = 'slack'
           AND reply.entity_type = 'message'
           AND reply.relations->>'author_user_id' = ANY(%(handles)s)
           AND text.embedding IS NOT NULL
         ORDER BY text.embedding <=> %(vector)s::vector
         LIMIT %(pool)s
    )
    SELECT DISTINCT ON (mine.said, mine.parent_ts)
           mine.said_at,
           mine.channel,
           mine.said,
           mine.permalink,
           mine.parent_ts,
           parent.raw_payload->>'text' AS situation,
           parent.relations->>'author_user_id' AS situation_author,
           mine.score
      FROM mine
      LEFT JOIN ledger_records parent
        ON parent.source = 'slack'
       AND parent.entity_type = 'message'
       AND parent.source_entity_id = mine.parent_ts
     ORDER BY mine.said, mine.parent_ts, mine.score DESC
"""

# How many nearest messages to pull before folding duplicates and ranking.
# Larger than the limit because one sentence he repeats often collapses to a
# single precedent.
VECTOR_POOL = 60



@dataclass
class Precedent:
    said_at: datetime | None
    channel: str | None
    said: str
    permalink: str | None
    situation: str | None
    situation_author: str | None
    score: float

    @property
    def has_situation(self) -> bool:
        """Whether the thing he was answering is in the ledger at all.

        False for a reply whose parent predates every collection window. Those
        are the 622 the nightly sweep is recovering, and until it has, they are
        answers with the question missing -- still his words, but no longer
        evidence about when he says them.
        """
        return bool(self.situation)

    def as_dict(self) -> dict[str, Any]:
        return {
            "said_at": self.said_at.isoformat() if self.said_at else None,
            "channel": self.channel,
            "said": self.said,
            "permalink": self.permalink,
            "situation": self.situation,
            "situation_author": self.situation_author,
            "has_situation": self.has_situation,
            "score": round(float(self.score), 4),
        }


@dataclass
class PrecedentResult:
    query: str
    found: list[Precedent] = field(default_factory=list)
    without_situation: int = 0
    person_id: str = ""
    # "embedding" or "trigram". Printed, because a thin result means something
    # different in each case: with trigrams it usually means the words differ,
    # and the fix is to run the embedding batch.
    matcher: str = "trigram"

    def as_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "person_id": self.person_id,
            "precedents": [item.as_dict() for item in self.found],
            # Reported, not hidden: a run where most precedents have no
            # situation is a run whose evidence is half missing, and the reader
            # has to know that before leaning on it.
            "without_situation": self.without_situation,
            "matcher": self.matcher,
        }


def precedents(
    database_url: str,
    person_id: str,
    query: str,
    *,
    limit: int = 8,
    names: dict[str, str] | None = None,
    embedder: Any = None,
) -> PrecedentResult:
    """The closest things he has actually said, with what he was answering.

    With an embedder, matched by meaning; without one, by characters. The
    result says which, so a thin answer can be read correctly.
    """
    import psycopg

    text = (query or "").strip()
    if not text:
        raise ValueError("a precedent search needs a situation to match")

    from .reconcile import person_handles

    result = PrecedentResult(query=text, person_id=person_id)
    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            handles = person_handles(cursor, person_id)
            rows = []
            if embedder is not None:
                vector = embedder.embed([text])[0]
                cursor.execute(
                    _PRECEDENT_VECTOR_SQL,
                    {
                        "vector": "[" + ",".join(repr(value) for value in vector) + "]",
                        "handles": handles,
                        "pool": VECTOR_POOL,
                    },
                )
                rows = cursor.fetchall()
                result.matcher = "embedding"
            if not rows:
                # No embedder, or nothing embedded yet. Characters, and the
                # result says so.
                result.matcher = "trigram"
                # Transaction-local so a loose threshold here never leaks into
                # the next query in this session.
                cursor.execute(
                    "SELECT set_config('pg_trgm.similarity_threshold', %s, true)",
                    (str(MATCH_FLOOR),),
                )
                cursor.execute(
                    _PRECEDENT_SQL,
                    {"query": text, "handles": handles, "floor": MATCH_FLOOR},
                )
                rows = cursor.fetchall()

    lookup = names or {}
    found = [
        Precedent(
            said_at=row[0],
            channel=row[1],
            said=str(row[2]),
            permalink=row[3],
            situation=row[5],
            situation_author=lookup.get(str(row[6]), row[6]) if row[6] else None,
            score=row[7] or 0.0,
        )
        for row in rows
    ]
    # Ranked here rather than in SQL: DISTINCT ON has to order by its own key
    # first, so the database cannot also return them best-first.
    found.sort(key=lambda item: (item.has_situation, item.score), reverse=True)
    result.without_situation = sum(1 for item in found if not item.has_situation)
    result.found = found[:limit]
    return result
