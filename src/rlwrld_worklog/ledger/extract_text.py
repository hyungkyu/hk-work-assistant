"""Text extraction from ledger payloads, into the search corpus.

The first search index (migration 0005) was built on `ledger_extracted_text`
on the belief that it held the text of everything collected. It does not: its
own table comment says it preserves text the live API cannot return again --
938 legacy Notion documents, growing by zero rows per night. The text of what
the collectors actually collect lives in `ledger_records.raw_payload`, in five
different API shapes, and nothing read it.

This module is the missing step: one extractor per source that knows its API
shape, and an indexer that walks `ledger_records` and writes what the
extractors find into `search_documents` (migration 0006). It is idempotent --
one document per collected entity, keyed by (source, source_entity_id), and a
record already indexed is only re-read when a newer observation of it lands --
so it runs as a batch after every load and as a backfill over everything
already loaded.

Extraction is derivation, not preservation. A wrong extraction is repaired by
re-running with a newer extractor version; the raw payload it derives from is
never touched. That is also why this table is separate from
`ledger_extracted_text`, whose contract is "preserved verbatim; never
rewritten" -- derived text that gets rewritten on repair must not live under
that contract.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

# Bumped whenever an extractor changes what it returns, so a re-index can be
# scoped to "everything an older extractor wrote" instead of everything.
EXTRACTOR_VERSION = "ledger-extract/1"

_MAX_TEXT_CHARS = 100_000


def _clip(text: str) -> str:
    # A pathological payload (a page holding a pasted book) must not become a
    # pathological index row. The clip point is far above any observed page.
    return text[:_MAX_TEXT_CHARS]


def _join(parts: Iterable[Any]) -> str | None:
    pieces = [str(part).strip() for part in parts if part and str(part).strip()]
    if not pieces:
        return None
    return _clip("\n".join(dict.fromkeys(pieces)))


def _plain_text(value: Any, found: list[str]) -> None:
    """Collect every Notion `plain_text` in a payload, in document order.

    Notion nests rich text arbitrarily deep (paragraph, table cell, property,
    caption), and every leaf spells its text the same way. Walking for the key
    is the one extractor that survives Notion adding block types.
    """
    if isinstance(value, dict):
        text = value.get("plain_text")
        if isinstance(text, str) and text.strip():
            found.append(text)
        for key, item in value.items():
            if key != "plain_text":
                _plain_text(item, found)
    elif isinstance(value, list):
        for item in value:
            _plain_text(item, found)


def _extract_notion(entity_type: str, payload: dict[str, Any]) -> str | None:
    found: list[str] = []
    _plain_text(payload, found)
    return _join(found)


def _extract_slack(entity_type: str, payload: dict[str, Any]) -> str | None:
    if entity_type == "message":
        parts: list[Any] = [payload.get("text")]
        for attachment in payload.get("attachments") or []:
            if isinstance(attachment, dict):
                parts.extend((attachment.get("title"), attachment.get("text"),
                              attachment.get("fallback")))
        for file_object in payload.get("files") or []:
            if isinstance(file_object, dict):
                parts.extend((file_object.get("title"), file_object.get("name")))
        return _join(parts)
    if entity_type == "conversation":
        topic = payload.get("topic") or {}
        purpose = payload.get("purpose") or {}
        return _join((payload.get("name"),
                      topic.get("value") if isinstance(topic, dict) else None,
                      purpose.get("value") if isinstance(purpose, dict) else None))
    if entity_type == "user":
        profile = payload.get("profile") or {}
        return _join((payload.get("name"),
                      profile.get("real_name") if isinstance(profile, dict) else None,
                      profile.get("display_name") if isinstance(profile, dict) else None,
                      profile.get("title") if isinstance(profile, dict) else None))
    if entity_type == "usergroup":
        return _join((payload.get("name"), payload.get("handle"),
                      payload.get("description")))
    return None


def _extract_calendar(entity_type: str, payload: dict[str, Any]) -> str | None:
    if entity_type == "event":
        return _join((payload.get("summary"), payload.get("description"),
                      payload.get("location")))
    if entity_type == "calendar":
        return _join((payload.get("summary"), payload.get("description")))
    return None


def _extract_github(entity_type: str, payload: dict[str, Any]) -> str | None:
    parts: list[Any] = [payload.get("title"), payload.get("body")]
    commit = payload.get("commit")
    if isinstance(commit, dict):
        parts.append(commit.get("message"))
    parts.append(payload.get("message"))
    # Review states and comment paths help a search for "approved" or a file.
    parts.append(payload.get("state"))
    parts.append(payload.get("path"))
    return _join(parts)


def _extract_slurm(entity_type: str, payload: dict[str, Any]) -> str | None:
    name = payload.get("job_name") or payload.get("name") or payload.get("JobName")
    return _join((name, payload.get("comment"), payload.get("partition"),
                  payload.get("account"), payload.get("user") or payload.get("User")))


EXTRACTORS: dict[str, Callable[[str, dict[str, Any]], str | None]] = {
    "slack": _extract_slack,
    "notion": _extract_notion,
    "google_calendar": _extract_calendar,
    "google-calendar": _extract_calendar,
    "github": _extract_github,
    "slurm": _extract_slurm,
}


def extract_text(source: str, entity_type: str, payload: Any) -> str | None:
    """The searchable text of one ledger payload, or None when it has none.

    None is an answer, not a failure: a Slack join event, a Notion divider
    block, and most Slurm step rows genuinely hold nothing a person would
    search for.
    """
    if not isinstance(payload, dict):
        return None
    extractor = EXTRACTORS.get(source)
    if extractor is None:
        return None
    return extractor(entity_type, payload)


# ------------------------------------------------------------------ indexer

DOCUMENT_UPSERT = """
INSERT INTO search_documents (
    doc_id, ledger_id, source, entity_type, external_id,
    occurred_at, text_content, text_sha256, extractor
) VALUES (
    %(doc_id)s, %(ledger_id)s, %(source)s, %(entity_type)s, %(external_id)s,
    %(occurred_at)s, %(text_content)s, %(text_sha256)s, %(extractor)s
)
ON CONFLICT (doc_id) DO UPDATE SET
    ledger_id = EXCLUDED.ledger_id,
    text_content = EXCLUDED.text_content,
    text_sha256 = EXCLUDED.text_sha256,
    extractor = EXCLUDED.extractor,
    occurred_at = EXCLUDED.occurred_at,
    indexed_at = now(),
    -- Text that changed is text whose embedding no longer describes it.
    embedding = CASE WHEN search_documents.text_sha256 = EXCLUDED.text_sha256
                     THEN search_documents.embedding ELSE NULL END,
    embedded_at = CASE WHEN search_documents.text_sha256 = EXCLUDED.text_sha256
                       THEN search_documents.embedded_at ELSE NULL END
"""

# One document per collected entity, not per observation of it. The same Slack
# message can sit in the ledger twice -- a historical backfill observation and
# a current head -- and a search that returns it once per observation is worse
# than one that returns it once. The document id is therefore derived from
# (source, source_entity_id), spelled identically here and in SQL: an md5 of
# that key, cast to uuid, which needs no extension on either side.
#
# Candidates are every record whose entity has no document yet, or that was
# loaded after the entity was last indexed (an edit collected tonight must
# replace last week's text). Ordering puts current_head last so that when one
# batch carries both observations of an entity, the head's text is the one
# that stays.
_CANDIDATES = """
    SELECT r.ledger_id, r.source, r.entity_type, r.source_entity_id,
           COALESCE(r.source_updated_at, r.source_created_at) AS occurred_at,
           r.raw_payload
      FROM ledger_records r
      LEFT JOIN search_documents d
        ON d.doc_id = md5(r.source || ':' || r.source_entity_id)::uuid
     WHERE (d.doc_id IS NULL OR r.inserted_at > d.indexed_at)
       {source_filter}
     ORDER BY (r.observation_role = 'current_head'),
              COALESCE(r.source_updated_at, r.source_created_at) NULLS FIRST
"""


def document_id(source: str, source_entity_id: str) -> str:
    """The uuid the SQL side computes as md5(source || ':' || entity)::uuid."""
    import uuid

    digest = hashlib.md5(f"{source}:{source_entity_id}".encode("utf-8")).hexdigest()
    return str(uuid.UUID(digest))


@dataclass
class IndexResult:
    dry_run: bool
    scanned: int = 0
    indexed: int = 0
    without_text: int = 0
    by_source: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "scanned": self.scanned,
            "indexed": self.indexed,
            "without_text": self.without_text,
            "by_source": dict(sorted(self.by_source.items())),
            "errors": self.errors,
            "extractor": EXTRACTOR_VERSION,
        }


def index_ledger_text(
    database_url: str,
    *,
    sources: tuple[str, ...] = (),
    dry_run: bool = False,
    batch_size: int = 2000,
) -> IndexResult:
    """Extract and index every collected entity not yet (or newly) indexed."""
    import psycopg

    result = IndexResult(dry_run=dry_run)
    source_filter = "AND r.source = ANY(%(sources)s)" if sources else ""
    query = _CANDIDATES.format(source_filter=source_filter)
    parameters: dict[str, Any] = {"sources": list(sources)} if sources else {}

    with psycopg.connect(database_url) as connection:
        connection.autocommit = False
        # A server-side cursor: the candidate set can be the whole backlog
        # (170k+ rows of jsonb) and must not be materialised in this process.
        with connection.cursor(name="ledger_text_candidates") as reader:
            reader.itersize = batch_size
            reader.execute(query, parameters)
            with connection.cursor() as writer:
                for row in reader:
                    ledger_id, source, entity_type, external_id, occurred_at, payload = row
                    result.scanned += 1
                    if isinstance(payload, str):
                        try:
                            payload = json.loads(payload)
                        except ValueError:
                            payload = None
                    text = extract_text(source, entity_type, payload)
                    if not text:
                        result.without_text += 1
                        continue
                    if not dry_run:
                        writer.execute(
                            DOCUMENT_UPSERT,
                            {
                                "doc_id": document_id(source, str(external_id)),
                                "ledger_id": ledger_id,
                                "source": source,
                                "entity_type": entity_type,
                                "external_id": external_id,
                                "occurred_at": occurred_at,
                                "text_content": text,
                                "text_sha256": hashlib.sha256(
                                    text.encode("utf-8")
                                ).hexdigest(),
                                "extractor": EXTRACTOR_VERSION,
                            },
                        )
                    result.indexed += 1
                    result.by_source[source] = result.by_source.get(source, 0) + 1
        if dry_run:
            connection.rollback()
        else:
            connection.commit()
    return result
