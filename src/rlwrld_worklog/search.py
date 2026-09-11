"""Search the collected text, in the database that already holds it.

One corpus: `search_documents`, one row per collected entity, extracted from
`ledger_records.raw_payload` at load time by ledger/extract_text.py (the 938
preserved legacy documents are copied in by migration 0006). Every hit carries
its ledger provenance, which is the difference between a search result and a
claim.

This module's first version searched `ledger_extracted_text` in the belief
that it held everything collected; that table is preservation-only and held
938 Notion documents. The premise was wrong, not the matchers -- the matchers
moved corpus unchanged.

Two matchers, because Korean needs both:

  * **words** -- the `simple` tsvector. Postgres ships no Korean stemmer and
    `english` would stem Korean wrongly, so `simple` lowercases and matches
    whole words. Fast, ranked by `ts_rank_cd`, and blind to agglutination:
    a search for `수집` does not match `수집을`.
  * **substring** -- trigram similarity, which does match `수집을`, at the cost
    of matching things a person would not call a match.

`auto` runs words first and falls back to substring only when words found
nothing. That order is deliberate: the ranked matcher answers when it can, and
the loose one is what stops "no results" from being the wrong answer. Every
result says which matcher produced it, because a substring hit and a word hit
are not equally strong evidence and a reader is entitled to know which they
have.

This module only reads. Nothing here writes to the database.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Sequence

MATCHERS = ("auto", "words", "substring")
DEFAULT_LIMIT = 25
MAX_LIMIT = 200

# Below this trigram similarity a "match" is noise. 0.1 is loose on purpose:
# the substring matcher runs when the word matcher already found nothing, so
# its job is to offer candidates, not to be certain about them.
DEFAULT_SIMILARITY = 0.1

# How much of the matched text to return. Enough to judge a hit without
# shipping a whole Notion page through an API response.
SNIPPET_CHARS = 320


@dataclass
class SearchResult:
    query: str
    matcher: str
    hits: list[dict[str, Any]] = field(default_factory=list)
    total_scanned: int = 0
    fell_back: bool = False
    took_ms: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "matcher": self.matcher,
            "hits": self.hits,
            "count": len(self.hits),
            "fell_back": self.fell_back,
            "took_ms": self.took_ms,
        }


def _snippet(text: str, query: str) -> str:
    """The matched text around the first occurrence, or the head of it.

    Not `ts_headline`: that re-parses with a text-search configuration and
    under `simple` it marks whole words only, so a Korean substring hit comes
    back with nothing highlighted. Finding the substring here is both simpler
    and honest about what matched.
    """
    if not text:
        return ""
    lowered = text.lower()
    position = lowered.find(query.strip().lower())
    if position < 0:
        return text[:SNIPPET_CHARS].strip()
    start = max(0, position - SNIPPET_CHARS // 3)
    end = min(len(text), start + SNIPPET_CHARS)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return f"{prefix}{text[start:end].strip()}{suffix}"


_SELECT = """
    SELECT t.doc_id, t.source, t.entity_type, char_length(t.text_content),
           COALESCE(t.occurred_at, t.indexed_at) AS happened_at,
           jsonb_build_object('external_id', t.external_id,
                              'ledger_id', t.ledger_id) AS source_ref,
           t.text_content, {score} AS score
      FROM search_documents t
     WHERE {predicate}
       {filters}
     ORDER BY score DESC, happened_at DESC
     LIMIT %(limit)s
"""


def _filters(sources: Sequence[str], since: datetime | None, until: datetime | None) -> str:
    clauses = []
    if sources:
        clauses.append("AND t.source = ANY(%(sources)s)")
    if since is not None:
        clauses.append("AND COALESCE(t.occurred_at, t.indexed_at) >= %(since)s")
    if until is not None:
        clauses.append("AND COALESCE(t.occurred_at, t.indexed_at) < %(until)s")
    return "\n       ".join(clauses)


def _run(cursor, sql: str, parameters: dict[str, Any], query: str, matcher: str) -> list[dict]:
    cursor.execute(sql, parameters)
    hits = []
    for row in cursor.fetchall():
        (doc_id, source, entity_type, char_length, happened_at, source_ref, text, score) = row
        hits.append(
            {
                "artifact_id": str(doc_id),
                "source": source,
                "kind": entity_type,
                "char_length": char_length,
                "inserted_at": happened_at.isoformat() if happened_at else None,
                "source_ref": source_ref or {},
                "snippet": _snippet(text or "", query),
                "score": float(score) if score is not None else None,
                "matcher": matcher,
            }
        )
    return hits


def search_text(
    database_url: str,
    query: str,
    *,
    matcher: str = "auto",
    sources: Sequence[str] = (),
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = DEFAULT_LIMIT,
    similarity: float = DEFAULT_SIMILARITY,
) -> SearchResult:
    import time

    import psycopg

    if matcher not in MATCHERS:
        raise ValueError(f"unknown matcher {matcher!r}; expected one of {', '.join(MATCHERS)}")
    text = (query or "").strip()
    if not text:
        raise ValueError("a search needs a query")
    limit = max(1, min(int(limit), MAX_LIMIT))

    parameters: dict[str, Any] = {
        "query": text,
        # Escaped, so a query containing % or _ searches for those characters
        # rather than becoming a wildcard the person did not type.
        "like": "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%",
        "limit": limit,
        "similarity": similarity,
    }
    if sources:
        parameters["sources"] = list(sources)
    if since is not None:
        parameters["since"] = since
    if until is not None:
        parameters["until"] = until
    filters = _filters(sources, since, until)

    words_sql = _SELECT.format(
        score="ts_rank_cd(t.search_tsv, plainto_tsquery('simple', %(query)s))",
        predicate="t.search_tsv @@ plainto_tsquery('simple', %(query)s)",
        filters=filters,
    )
    # ILIKE, not `similarity()`. Trigram similarity normalises over *both*
    # strings, so a two-character query against a page-long document scores
    # near zero and the `%` operator returns nothing at any sane threshold --
    # measured, not assumed. `ILIKE '%...%'` is what "substring" actually
    # means, and the gin_trgm_ops index accelerates it. `word_similarity`
    # ranks: it scores the query against the best-matching run of words in the
    # document rather than against the whole of it.
    substring_sql = _SELECT.format(
        score="word_similarity(%(query)s, t.text_content)",
        predicate="t.text_content ILIKE %(like)s",
        filters=filters,
    )

    started = time.monotonic()
    result = SearchResult(query=text, matcher=matcher)
    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            # Transaction-local, so a loose threshold for this search never
            # becomes a loose threshold for the next one. `set_config` rather
            # than `SET LOCAL`: SET takes a literal only, and passing the
            # threshold as a bound parameter is a syntax error at the server.
            cursor.execute(
                "SELECT set_config('pg_trgm.similarity_threshold', %s, true)",
                (str(similarity),),
            )
            if matcher in {"auto", "words"}:
                result.hits = _run(cursor, words_sql, parameters, text, "words")
            if matcher == "substring" or (matcher == "auto" and not result.hits):
                result.fell_back = matcher == "auto" and not result.hits
                result.hits = _run(cursor, substring_sql, parameters, text, "substring")
    result.took_ms = round((time.monotonic() - started) * 1000, 1)
    return result


def search_status(database_url: str) -> dict[str, Any]:
    """What the corpus holds, so an empty result can be told from an empty index."""
    import psycopg

    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT source, count(*), max(COALESCE(occurred_at, indexed_at)),
                       count(*) FILTER (WHERE embedding IS NOT NULL)
                  FROM search_documents
                 GROUP BY source ORDER BY count(*) DESC
                """
            )
            by_source = [
                {
                    "source": row[0],
                    "documents": row[1],
                    "latest": row[2].isoformat() if row[2] else None,
                    "embedded": row[3],
                }
                for row in cursor.fetchall()
            ]
    return {
        "by_source": by_source,
        "documents": sum(item["documents"] for item in by_source),
        "embedded": sum(item["embedded"] for item in by_source),
    }
