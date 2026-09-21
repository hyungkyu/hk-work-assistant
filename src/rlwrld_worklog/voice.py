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

# Below this, the nearest thing in the corpus is not about the question.
#
# Measured on 2026-09-21, against 63,092 embedded messages: "배포를 사람이
# 직접 돌리고 있다" came back with "퇴근하고 운동중입니다" and "ㅋㅋㅋ
# 잘드시네요" as its closest situations. Nothing was broken -- one line of
# Slack is short and contextless, so in a space of 63,000 of them everything
# is roughly equidistant and the nearest neighbour is noise.
#
# A tool that shows noise as precedent is worse than one that shows nothing,
# because the noise gets used. So there is a floor, the score is printed, and
# "no precedent close enough" is a real answer.
SCORE_FLOOR = float(__import__("os").environ.get("WORKLOG_PRECEDENT_FLOOR", "0.55"))

# How many nearest messages to pull before folding duplicates and ranking.
# Larger than the limit because one sentence he repeats often collapses to a
# single precedent.
VECTOR_POOL = 60

# How long after a message he can speak and still be answering it. Slack
# conversations are not turn-taking protocols; ten minutes is wide enough to
# catch a reply typed after reading, narrow enough that the next topic in the
# channel is not counted as an answer to this one.
ANSWER_WINDOW_MINUTES = 10

# The query is a situation, so the situation is what gets matched. The first
# version embedded his replies and compared the situation against those --
# asking "what does this look like" of the answers instead of the questions.
# On 2026-09-21 that returned "가는 중." and "ㅋㅋㅋㅋ 알아서 해요" for a
# question about deployments being run by hand, which is the retrieval
# equivalent of matching a key to a key.
#
# And a situation is not only a thread parent. Most of what he writes is not a
# thread reply at all -- 60 of 68 precedents came back with no parent -- so the
# situation is the conversation immediately before he spoke: the thread parent
# when there is one, otherwise the messages in that channel in the minutes
# before his own.
_SITUATION_SQL = """
    WITH situation AS (
        SELECT other.ledger_id,
               other.source_created_at AS said_at,
               coalesce(other.scope->>'channel_id', other.scope->>'container') AS channel,
               other.raw_payload->>'text' AS situation_text,
               other.relations->>'author_user_id' AS situation_author,
               other.source_entity_id AS situation_ts,
               1 - (doc.embedding <=> %(vector)s::vector) AS score
          FROM ledger_records other
          JOIN search_documents doc ON doc.ledger_id = other.ledger_id
         WHERE other.source = 'slack'
           AND other.entity_type = 'message'
           AND other.relations->>'author_user_id' <> ALL(%(handles)s)
           AND doc.embedding IS NOT NULL
         ORDER BY doc.embedding <=> %(vector)s::vector
         LIMIT %(pool)s
    )
    SELECT DISTINCT ON (situation.ledger_id)
           mine.source_created_at AS said_at,
           situation.channel,
           mine.raw_payload->>'text' AS said,
           mine.raw_payload->>'permalink' AS permalink,
           situation.situation_ts,
           situation.situation_text,
           situation.situation_author,
           situation.score,
           -- How the two were connected. A thread link is Slack's own
           -- statement that this answers that; anything else is this query
           -- inferring it from time and place, and the reader is told which.
           CASE
               WHEN coalesce(mine.relations->>'thread_id', mine.relations->>'parent_ts')
                    = situation.situation_ts THEN 'thread'
               ELSE 'nearby'
           END AS link,
           -- HK, 2026-09-21: 여럿이 오갈 경우, 바로 직전 대화가 아닐 수는
           -- 있어. So the pairing is not asserted, it is measured: how many
           -- messages, from how many people, sat between the two. One person
           -- and nothing in between is a conversation; nine messages from
           -- four people is a guess, and the line says so instead of looking
           -- the same as the first case.
           (SELECT count(*)
              FROM ledger_records between_them
             WHERE between_them.source = 'slack'
               AND between_them.entity_type = 'message'
               AND coalesce(between_them.scope->>'channel_id',
                            between_them.scope->>'container') = situation.channel
               AND between_them.source_created_at > situation.said_at
               AND between_them.source_created_at < mine.source_created_at
           ) AS between_count,
           (SELECT count(DISTINCT between_them.relations->>'author_user_id')
              FROM ledger_records between_them
             WHERE between_them.source = 'slack'
               AND between_them.entity_type = 'message'
               AND coalesce(between_them.scope->>'channel_id',
                            between_them.scope->>'container') = situation.channel
               AND between_them.source_created_at > situation.said_at
               AND between_them.source_created_at < mine.source_created_at
           ) AS between_speakers
      FROM situation
      JOIN ledger_records mine
        ON mine.source = 'slack'
       AND mine.entity_type = 'message'
       AND mine.relations->>'author_user_id' = ANY(%(handles)s)
       AND coalesce(mine.scope->>'channel_id', mine.scope->>'container')
           = situation.channel
       AND (
           -- He replied in the thread that message started ...
           coalesce(mine.relations->>'thread_id', mine.relations->>'parent_ts')
               = situation.situation_ts
           -- ... or he spoke next, in the same place, soon after.
           OR (
               mine.source_created_at > situation.said_at
               AND mine.source_created_at
                   < situation.said_at + make_interval(mins => %(window)s)
           )
       )
       AND coalesce(mine.raw_payload->>'text', '') <> ''
     ORDER BY situation.ledger_id, mine.source_created_at
"""



@dataclass
class Precedent:
    said_at: datetime | None
    channel: str | None
    said: str
    permalink: str | None
    situation: str | None
    situation_author: str | None
    score: float
    # "thread" when Slack itself linked the two, "nearby" when this query
    # inferred it from time and place, "none" when there is no situation.
    link: str = "none"
    between_count: int = 0
    between_speakers: int = 0

    @property
    def certainty(self) -> str:
        """How much weight the pairing can carry.

        Named rather than scored: a number invites averaging, and the three
        cases are different in kind. A thread link is Slack's own record. A
        quiet gap is a conversation. A busy gap is a guess, and a guess shown
        as a precedent is the failure this whole tool exists to avoid.
        """
        if not self.situation:
            return "상황 없음"
        if self.link == "thread":
            return "스레드"
        if self.between_speakers > 1 or self.between_count > 3:
            return f"추정 · 사이 {self.between_count}건/{self.between_speakers}명"
        return "직후"

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
            "link": self.link,
            "between_count": self.between_count,
            "between_speakers": self.between_speakers,
            "certainty": self.certainty,
            "score": round(float(self.score), 4),
        }


@dataclass
class PrecedentResult:
    query: str
    found: list[Precedent] = field(default_factory=list)
    without_situation: int = 0
    person_id: str = ""
    # Near misses that were dropped for being too far away. Reported so an
    # empty answer can be told from an unasked question.
    below_floor: int = 0
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
            "below_floor": self.below_floor,
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
                    _SITUATION_SQL,
                    {
                        "vector": "[" + ",".join(repr(value) for value in vector) + "]",
                        "handles": handles,
                        "pool": VECTOR_POOL,
                        "window": ANSWER_WINDOW_MINUTES,
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
            # The trigram query joins the thread parent directly, so a
            # situation it returns is always Slack's own link; it has no
            # column to say so.
            link=str(row[8]) if len(row) > 8 else ("thread" if row[5] else "none"),
            between_count=int(row[9]) if len(row) > 9 else 0,
            between_speakers=int(row[10]) if len(row) > 10 else 0,
        )
        for row in rows
    ]
    # Ranked here rather than in SQL: DISTINCT ON has to order by its own key
    # first, so the database cannot also return them best-first.
    # Thread links first, then quiet pairings, then the crowded ones -- and
    # only then by similarity. A close match whose pairing is a guess is worth
    # less than a slightly worse match Slack itself linked.
    _order = {"thread": 2, "nearby": 1, "none": 0}
    found.sort(
        key=lambda item: (
            _order.get(item.link, 0),
            -min(item.between_count, 10),
            item.score,
        ),
        reverse=True,
    )
    result.without_situation = sum(1 for item in found if not item.has_situation)
    # Only for the vector matcher: the trigram score is a different quantity
    # on a different scale, and one floor cannot mean the same thing in both.
    if result.matcher == "embedding":
        result.below_floor = sum(1 for item in found if item.score < SCORE_FLOOR)
        found = [item for item in found if item.score >= SCORE_FLOOR]
    result.found = found[:limit]
    return result
