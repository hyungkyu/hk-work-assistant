"""Standard ledger -> service PostgreSQL.

Two things happen per batch, inside one transaction:

  1. the ledger rows themselves land in ledger_records / ledger_extracted_text,
     which is the system of record for historical observations;
  2. a service projection lands in timeline_events plus
     source_object_observations / source_object_heads with origin='legacy'.

The head upsert only advances when the incoming priority is greater than, or
equal-and-newer than, the stored one. A live observation (priority 100)
therefore always outranks a primary legacy capture (priority 20), while the
legacy thread supplement (priority 10) fills only objects absent from the
primary capture. Re-running a legacy load can never demote a current head
(principle 7).

Idempotency: ledger_id is deterministic, and every insert is ON CONFLICT DO
UPDATE keyed on it, so re-running a load converges rather than duplicating.

--dry-run executes the full transaction and then rolls back, so the reported
counts are what an apply would actually write.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from .common import file_sha256
from .convert import iter_extracted_text_files, iter_ledger_files, read_jsonl

LEGACY_ORIGIN_PRIORITY = 20
LEGACY_THREAD_STORE_PRIORITY = 10
LIVE_ORIGIN_PRIORITY = 100

# Entity types projected onto the activity timeline. Notion blocks stay in the
# ledger only: they are page content, not a timeline activity, and promoting
# them would double-count page edits. Slurm step rows (`.batch`, `.extern`)
# never reach the ledger at all, for the same reason -- they are parts of a
# job, not activities.
#
# GitHub and Slurm were absent from this set until 2026-09-11, and the effect
# was not a missing feature but a silent one: measured on the nightly load of
# 2026-09-10 KST, github had 1,710 ledger records and 0 timeline events, slurm
# 11,556 and 0. The data was collected, converted, loaded and then stopped one
# layer short of anything a person could read, so "who did what last week"
# answered for Slack and Notion and omitted everyone who wrote code or ran a
# training job.
PROJECTED_ENTITY_TYPES = {
    "message",
    "page",
    "comment",
    "event",
    # GitHub
    "commit",
    "pull_request",
    "review",
    "review_comment",
    "issue",
    "issue_comment",
    # Slurm
    "job",
}

EVENT_TYPE_BY_ENTITY = {
    "message": "message",
    "page": "notion_page",
    "comment": "notion_comment",
    "event": "calendar_event",
    # Prefixed by source, because `issue` and `comment` alone would collide
    # with names a future source will want, and an event_type is read by
    # people as well as by queries.
    "commit": "github_commit",
    "pull_request": "github_pull_request",
    "review": "github_review",
    "review_comment": "github_review_comment",
    "issue": "github_issue",
    "issue_comment": "github_issue_comment",
    "job": "slurm_job",
}

# GitHub activity entities whose author the collector already resolved to a
# login, recorded in `relations.author`.
_GITHUB_AUTHORED = {"pull_request", "review", "review_comment", "issue", "issue_comment"}

LEDGER_UPSERT = """
INSERT INTO ledger_records (
    ledger_id, schema_version, capture_profile, source, entity_type,
    tenant_workspace_id, tenant_status, scope, source_entity_id, source_entity_key,
    source_revision_id, source_created_at, source_updated_at, source_updated_at_status,
    collected_at, is_deleted, deleted_kind, deleted_status,
    raw_payload, content_hash, relations,
    source_file, source_file_sha256, source_file_kind, record_pointer,
    legacy_layout_version, converter_version, provenance,
    coverage, observation_role, observation_window_start, observation_window_end,
    observation_window, capture_completeness_status, capture_completeness,
    supplement_provenance, visibility_routing, denormalized_label_snapshot, batch_id
) VALUES (
    %(ledger_id)s, %(schema_version)s, %(capture_profile)s, %(source)s, %(entity_type)s,
    %(tenant_workspace_id)s, %(tenant_status)s, %(scope)s, %(source_entity_id)s, %(source_entity_key)s,
    %(source_revision_id)s, %(source_created_at)s, %(source_updated_at)s, %(source_updated_at_status)s,
    %(collected_at)s, %(is_deleted)s, %(deleted_kind)s, %(deleted_status)s,
    %(raw_payload)s, %(content_hash)s, %(relations)s,
    %(source_file)s, %(source_file_sha256)s, %(source_file_kind)s, %(record_pointer)s,
    %(legacy_layout_version)s, %(converter_version)s, %(provenance)s,
    %(coverage)s, %(observation_role)s, %(observation_window_start)s, %(observation_window_end)s,
    %(observation_window)s, %(capture_completeness_status)s, %(capture_completeness)s,
    %(supplement_provenance)s, %(visibility_routing)s, %(denormalized_label_snapshot)s, %(batch_id)s
)
ON CONFLICT (ledger_id) DO UPDATE SET
    capture_profile = EXCLUDED.capture_profile,
    scope = EXCLUDED.scope,
    raw_payload = EXCLUDED.raw_payload,
    relations = EXCLUDED.relations,
    provenance = EXCLUDED.provenance,
    coverage = EXCLUDED.coverage,
    capture_completeness = EXCLUDED.capture_completeness,
    supplement_provenance = EXCLUDED.supplement_provenance,
    visibility_routing = EXCLUDED.visibility_routing,
    denormalized_label_snapshot = EXCLUDED.denormalized_label_snapshot,
    batch_id = EXCLUDED.batch_id
"""

TEXT_UPSERT = """
INSERT INTO ledger_extracted_text (
    artifact_id, schema_version, ledger_id, source, kind, text_content,
    text_sha256, char_length, byte_length, extractor, source_ref, provenance, batch_id
) VALUES (
    %(artifact_id)s, %(schema_version)s, %(ledger_id)s, %(source)s, %(kind)s, %(text_content)s,
    %(text_sha256)s, %(char_length)s, %(byte_length)s, %(extractor)s, %(source_ref)s,
    %(provenance)s, %(batch_id)s
)
ON CONFLICT (artifact_id) DO UPDATE SET
    text_content = EXCLUDED.text_content,
    source_ref = EXCLUDED.source_ref,
    provenance = EXCLUDED.provenance,
    batch_id = EXCLUDED.batch_id
"""

TIMELINE_UPSERT = """
INSERT INTO timeline_events (
    event_id, source, event_type, external_id, actor_external_id,
    occurred_at, updated_at, ingested_at, container_id, thread_id,
    permalink, classifications, payload
) VALUES (
    %(event_id)s, %(source)s, %(event_type)s, %(external_id)s, %(actor_external_id)s,
    %(occurred_at)s, %(updated_at)s, %(ingested_at)s, %(container_id)s, %(thread_id)s,
    %(permalink)s, %(classifications)s, %(payload)s
)
ON CONFLICT (event_id) DO UPDATE SET
    actor_external_id = EXCLUDED.actor_external_id,
    occurred_at = EXCLUDED.occurred_at,
    updated_at = EXCLUDED.updated_at,
    container_id = EXCLUDED.container_id,
    thread_id = EXCLUDED.thread_id,
    permalink = EXCLUDED.permalink,
    payload = EXCLUDED.payload
"""

OBSERVATION_UPSERT = """
INSERT INTO source_object_observations (
    id, source, object_type, external_id, origin, origin_priority,
    observed_at, remote_updated_at, is_deleted, payload, timeline_event_id
) VALUES (
    %(id)s, %(source)s, %(object_type)s, %(external_id)s, %(origin)s, %(origin_priority)s,
    %(observed_at)s, %(remote_updated_at)s, %(is_deleted)s, %(payload)s, %(timeline_event_id)s
)
ON CONFLICT (id) DO UPDATE SET
    observed_at = EXCLUDED.observed_at,
    remote_updated_at = EXCLUDED.remote_updated_at,
    is_deleted = EXCLUDED.is_deleted,
    payload = EXCLUDED.payload,
    timeline_event_id = EXCLUDED.timeline_event_id
"""

HEAD_UPSERT = """
INSERT INTO source_object_heads (
    source, object_type, external_id, observation_id, origin, origin_priority, remote_updated_at
) VALUES (%s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (source, object_type, external_id) DO UPDATE SET
    observation_id = EXCLUDED.observation_id,
    origin = EXCLUDED.origin,
    origin_priority = EXCLUDED.origin_priority,
    remote_updated_at = EXCLUDED.remote_updated_at,
    selected_at = now()
WHERE
    EXCLUDED.origin_priority > source_object_heads.origin_priority
    OR (
        EXCLUDED.origin_priority = source_object_heads.origin_priority
        AND COALESCE(EXCLUDED.remote_updated_at, '-infinity'::timestamptz)
            >= COALESCE(source_object_heads.remote_updated_at, '-infinity'::timestamptz)
    )
"""


@dataclass
class LoadResult:
    source: str
    dry_run: bool
    run_id: str | None = None
    batches: int = 0
    batches_skipped_unchanged: int = 0
    ledger_records: int = 0
    extracted_text: int = 0
    timeline_events: int = 0
    observations: int = 0
    heads_advanced: int = 0
    heads_not_advanced: int = 0
    skipped_not_projected: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "mode": "dry_run" if self.dry_run else "apply",
            "run_id": self.run_id,
            "batches": self.batches,
            "batches_skipped_unchanged": self.batches_skipped_unchanged,
            "ledger_records": self.ledger_records,
            "extracted_text": self.extracted_text,
            "timeline_events": self.timeline_events,
            "observations": self.observations,
            "heads_advanced": self.heads_advanced,
            "heads_not_advanced": self.heads_not_advanced,
            "skipped_not_projected": self.skipped_not_projected,
            "errors": self.errors[:20],
        }


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _date(value: Any):
    if not isinstance(value, str) or len(value) != 10:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def _origin_for(record: dict[str, Any]) -> tuple[str, int]:
    """Rank an observation without discarding any of them.

    A live official-API capture is the current head at priority 100, a primary
    legacy capture is 20, and the legacy Slack thread supplement is 10. The
    capture profile decides, per record, so one loader handles both a live run
    and a legacy batch and a legacy re-run can never demote a live head
    (principle 7).
    """
    profile = str(record.get("capture_profile") or "")
    if profile.startswith("live-"):
        return "live", LIVE_ORIGIN_PRIORITY
    if profile == "legacy-slack-thread-store/v1":
        return "legacy", LEGACY_THREAD_STORE_PRIORITY
    return "legacy", LEGACY_ORIGIN_PRIORITY


def _observation_date(records: list[dict[str, Any]]):
    """A live ledger file is named by run id, so the day comes from the rows."""
    for record in records:
        day = _date((record.get("observation_window") or {}).get("start"))
        if day is not None:
            return day
    return None


def _commit_actor(relations: dict[str, Any], raw: dict[str, Any]) -> tuple[str | None, str]:
    """A commit's author, and which identity space the answer is in.

    A commit read over REST carries the GitHub account that authored it; the
    same commit read from a local bare mirror carries only the git author
    email, because that is all a git object holds. Both identify a person and
    neither can be converted into the other here, so the projection records
    which one it got rather than pretending they are the same namespace.
    Identity mapping (people/identities) is what reconciles them, and it can
    only do that if this row says which kind of handle it holds.
    """
    author = raw.get("author")
    if isinstance(author, dict) and author.get("login"):
        return str(author["login"]), "github_login"
    email = relations.get("author_email") or (
        ((raw.get("commit") or {}).get("author") or {}).get("email")
        if isinstance(raw.get("commit"), dict)
        else None
    )
    if email:
        return str(email), "git_email"
    return None, "unknown"


def _projection(record: dict[str, Any]) -> dict[str, Any]:
    """Derive the service timeline row from a ledger record."""
    entity = record["entity_type"]
    relations = record.get("relations") or {}
    scope = record.get("scope") or {}
    labels = record.get("denormalized_label_snapshot") or {}
    raw = record.get("raw_payload") or {}
    actor_kind = "unknown"
    if entity == "commit":
        actor, actor_kind = _commit_actor(relations, raw)
        container = scope.get("repository")
        # A commit's thread is its repository: a commit belongs to a history,
        # not to a conversation, and grouping by repository is what a reader
        # asking "what happened in this repo" wants.
        thread = scope.get("repository")
        permalink = raw.get("html_url")
    elif entity in _GITHUB_AUTHORED:
        actor = relations.get("author")
        actor_kind = "github_login" if actor else "unknown"
        container = scope.get("repository")
        # Reviews and comments hang off a pull request or issue; grouping them
        # under that URL is what makes a review thread readable as one thing.
        thread = (
            relations.get("pull_request_url")
            or relations.get("issue_url")
            or record["source_entity_id"]
        )
        permalink = raw.get("html_url")
    elif entity == "job":
        actor = relations.get("user")
        actor_kind = "slurm_user" if actor else "unknown"
        container = scope.get("cluster")
        thread = record["source_entity_id"]
        permalink = None
    elif entity == "message":
        actor = relations.get("author_user_id")
        container = scope.get("channel_id")
        thread = relations.get("thread_id")
        permalink = raw.get("permalink")
    elif entity == "page":
        actor = relations.get("last_edited_by_user_id") or relations.get("created_by_user_id")
        container = scope.get("notion_source_id")
        thread = record["source_entity_id"]
        permalink = raw.get("url")
    elif entity == "comment":
        actor = relations.get("created_by_user_id")
        container = relations.get("page_id")
        thread = relations.get("discussion_id")
        permalink = None
    else:
        actor = None
        container = scope.get("calendar_id")
        thread = record["source_entity_id"]
        permalink = raw.get("htmlLink")
    return {
        "actor": actor,
        "actor_kind": actor_kind,
        "container": container,
        "thread": thread,
        "permalink": permalink,
        "labels": labels,
    }


def _payload_for_timeline(record: dict[str, Any], projection: dict[str, Any]) -> dict[str, Any]:
    """Timeline payload keeps a pointer back to the ledger rather than a copy.

    The full raw payload already lives in ledger_records; duplicating it here
    would roughly double storage for no added capability.
    """
    return {
        "ledger_id": record["ledger_id"],
        "capture_profile": record["capture_profile"],
        "entity_type": record["entity_type"],
        "observation_window": record.get("observation_window"),
        "capture_completeness_status": (record.get("capture_completeness") or {}).get("status"),
        "source_updated_at_status": record.get("source_updated_at_status"),
        "deleted_status": (record.get("deleted_state") or {}).get("status"),
        "labels": projection["labels"],
        # Which identity space `actor_external_id` is in. Never guessed: a row
        # whose actor could not be read says `unknown` rather than naming a
        # space it does not belong to.
        "actor_kind": projection["actor_kind"],
        "provenance": {
            "source_file": (record.get("provenance") or {}).get("source_file"),
            "source_file_sha256": (record.get("provenance") or {}).get("source_file_sha256"),
            "record_pointer": (record.get("provenance") or {}).get("record_pointer"),
        },
    }


def load_source(
    *,
    database_url: str,
    ledger_root: Path,
    source: str,
    dry_run: bool = True,
    batch_size: int = 500,
    skip_unchanged: bool = True,
) -> LoadResult:
    import psycopg
    from psycopg.types.json import Jsonb

    result = LoadResult(source=source, dry_run=dry_run)
    ledger_files = iter_ledger_files(ledger_root, source)
    text_files = iter_extracted_text_files(ledger_root, source)
    if not ledger_files and not text_files:
        result.errors.append(f"no ledger files under {ledger_root}/ledger/{source}")
        return result

    with psycopg.connect(database_url) as connection:
        connection.autocommit = False
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO ledger_load_runs (source, mode, status, ledger_root)
                VALUES (%s, %s, 'running', %s) RETURNING id
                """,
                (source, "dry_run" if dry_run else "apply", str(ledger_root)),
            )
            run_id = cursor.fetchone()[0]
            result.run_id = str(run_id)

            for path in ledger_files:
                digest = file_sha256(str(path))
                if skip_unchanged:
                    cursor.execute(
                        "SELECT id FROM ledger_batches WHERE source = %s AND file_path = %s AND file_sha256 = %s",
                        (source, str(path), digest),
                    )
                    if cursor.fetchone():
                        result.batches_skipped_unchanged += 1
                        continue
                records = list(read_jsonl(path))
                observation_date = _date(path.stem) or _observation_date(records)
                cursor.execute(
                    """
                    INSERT INTO ledger_batches (
                        source, observation_date, file_path, file_sha256,
                        record_count, converter_version, load_run_id
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (source, file_path, file_sha256)
                    DO UPDATE SET record_count = EXCLUDED.record_count, load_run_id = EXCLUDED.load_run_id
                    RETURNING id
                    """,
                    (
                        source,
                        observation_date,
                        str(path),
                        digest,
                        len(records),
                        records[0]["provenance"]["converter_version"] if records else "unknown",
                        run_id,
                    ),
                )
                batch_id = cursor.fetchone()[0]
                result.batches += 1
                _load_records(cursor, records, batch_id, result, Jsonb)

            batch_id_for_text = None
            for path in text_files:
                artifacts = list(read_jsonl(path))
                for artifact in artifacts:
                    cursor.execute(
                        TEXT_UPSERT,
                        {
                            "artifact_id": artifact["artifact_id"],
                            "schema_version": artifact["schema_version"],
                            "ledger_id": artifact["ledger_id"],
                            "source": artifact["source"],
                            "kind": artifact["kind"],
                            "text_content": artifact["text"],
                            "text_sha256": artifact["text_sha256"],
                            "char_length": artifact["char_length"],
                            "byte_length": artifact["byte_length"],
                            "extractor": artifact["extractor"],
                            "source_ref": Jsonb(artifact.get("source_ref") or {}),
                            "provenance": Jsonb(artifact.get("provenance") or {}),
                            "batch_id": batch_id_for_text,
                        },
                    )
                    result.extracted_text += 1

            cursor.execute(
                """
                UPDATE ledger_load_runs
                SET finished_at = now(), status = %s, counters = %s
                WHERE id = %s
                """,
                (
                    "rolled_back" if dry_run else "succeeded",
                    Jsonb(result.as_dict()),
                    run_id,
                ),
            )
        if dry_run:
            connection.rollback()
            result.run_id = None
        else:
            connection.commit()
    return result


def _load_records(cursor, records: Iterable[dict[str, Any]], batch_id, result: LoadResult, Jsonb) -> None:
    ingested_at = datetime.now(timezone.utc)
    for record in records:
        provenance = record.get("provenance") or {}
        window = record.get("observation_window") or {}
        deleted = record.get("deleted_state") or {}
        cursor.execute(
            LEDGER_UPSERT,
            {
                "ledger_id": record["ledger_id"],
                "schema_version": record["schema_version"],
                "capture_profile": record["capture_profile"],
                "source": record["source"],
                "entity_type": record["entity_type"],
                "tenant_workspace_id": (record.get("tenant") or {}).get("workspace_id", "unknown"),
                "tenant_status": (record.get("tenant") or {}).get("status", "unknown"),
                "scope": Jsonb(record.get("scope") or {}),
                "source_entity_id": record["source_entity_id"],
                "source_entity_key": Jsonb(record.get("source_entity_key") or {}),
                "source_revision_id": record.get("source_revision_id"),
                "source_created_at": _timestamp(record.get("source_created_at")),
                "source_updated_at": _timestamp(record.get("source_updated_at")),
                "source_updated_at_status": record["source_updated_at_status"],
                "collected_at": _timestamp(record.get("collected_at")),
                "is_deleted": deleted.get("is_deleted"),
                "deleted_kind": deleted.get("kind"),
                "deleted_status": deleted.get("status", "unknown"),
                "raw_payload": Jsonb(record.get("raw_payload") or {}),
                "content_hash": record["content_hash"],
                "relations": Jsonb(record.get("relations") or {}),
                "source_file": provenance.get("source_file", ""),
                "source_file_sha256": provenance.get("source_file_sha256", ""),
                "source_file_kind": provenance.get("source_file_kind"),
                "record_pointer": provenance.get("record_pointer", ""),
                "legacy_layout_version": provenance.get("legacy_layout_version", "unknown"),
                "converter_version": provenance.get("converter_version", "unknown"),
                "provenance": Jsonb(provenance),
                "coverage": Jsonb(record.get("coverage") or {}),
                "observation_role": (record.get("coverage") or {}).get(
                    "observation_role", "historical_observation"
                ),
                "observation_window_start": _date(window.get("start")),
                "observation_window_end": _date(window.get("end")),
                "observation_window": Jsonb(window),
                "capture_completeness_status": (record.get("capture_completeness") or {}).get(
                    "status", "unknown"
                ),
                "capture_completeness": Jsonb(record.get("capture_completeness") or {}),
                "supplement_provenance": Jsonb(record.get("supplement_provenance") or {}),
                "visibility_routing": Jsonb(record.get("visibility_routing") or {}),
                "denormalized_label_snapshot": Jsonb(record.get("denormalized_label_snapshot") or {}),
                "batch_id": batch_id,
            },
        )
        result.ledger_records += 1

        if record["entity_type"] not in PROJECTED_ENTITY_TYPES:
            result.skipped_not_projected += 1
            continue

        projection = _projection(record)
        occurred_at = _timestamp(record.get("source_created_at"))
        if occurred_at is None:
            result.skipped_not_projected += 1
            result.errors.append(f"{record['ledger_id']}: no source_created_at, not projected")
            continue
        event_type = EVENT_TYPE_BY_ENTITY[record["entity_type"]]
        object_type = record["entity_type"]
        cursor.execute(
            TIMELINE_UPSERT,
            {
                "event_id": record["ledger_id"],
                "source": record["source"],
                "event_type": event_type,
                "external_id": record["source_entity_id"],
                "actor_external_id": projection["actor"],
                "occurred_at": occurred_at,
                "updated_at": _timestamp(record.get("source_updated_at")),
                "ingested_at": ingested_at,
                "container_id": projection["container"],
                "thread_id": projection["thread"],
                "permalink": projection["permalink"],
                # Classification is a service-derived concern; the legacy load
                # asserts nothing, so every row starts unclassified.
                "classifications": ["unclassified"],
                "payload": Jsonb(_payload_for_timeline(record, projection)),
            },
        )
        result.timeline_events += 1

        origin, origin_priority = _origin_for(record)
        cursor.execute(
            OBSERVATION_UPSERT,
            {
                "id": record["ledger_id"],
                "source": record["source"],
                "object_type": object_type,
                "external_id": record["source_entity_id"],
                "origin": origin,
                "origin_priority": origin_priority,
                "observed_at": _timestamp(record.get("collected_at")) or occurred_at,
                "remote_updated_at": _timestamp(record.get("source_updated_at")),
                "is_deleted": bool(deleted.get("is_deleted")),
                "payload": Jsonb({"ledger_id": record["ledger_id"]}),
                "timeline_event_id": record["ledger_id"],
            },
        )
        result.observations += 1

        cursor.execute(
            HEAD_UPSERT,
            (
                record["source"],
                object_type,
                record["source_entity_id"],
                record["ledger_id"],
                origin,
                origin_priority,
                _timestamp(record.get("source_updated_at")),
            ),
        )
        if cursor.rowcount:
            result.heads_advanced += 1
        else:
            result.heads_not_advanced += 1


def apply_migrations(
    *, database_url: str, migrations_dir: Path, dry_run: bool = True
) -> dict[str, Any]:
    """Apply pending sql/migrations files in filename order.

    Dry-run is the default: it reports the plan and rolls back.
    """
    import hashlib

    import psycopg

    files = sorted(migrations_dir.glob("*.sql"))
    if not files:
        # `Path.glob` on a directory that is not there returns nothing, so a
        # wrong --migrations-dir reported `pending: []` and `plan: []` -- which
        # reads as "fully migrated" and means "I found no migrations at all".
        # That happened on 2026-09-08: the app image does not carry sql/, and a
        # container run pointed at a relative path found an empty world and
        # said everything was applied.
        raise FileNotFoundError(
            f"no .sql files under {migrations_dir}; refusing to report a database "
            "as migrated on the strength of an empty directory"
        )
    plan: list[dict[str, Any]] = []
    with psycopg.connect(database_url) as connection:
        connection.autocommit = False
        with connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version text PRIMARY KEY,
                    applied_at timestamptz NOT NULL DEFAULT now(),
                    checksum text NOT NULL,
                    applied_by text NOT NULL DEFAULT current_user
                )
                """
            )
            cursor.execute("SELECT version, checksum FROM schema_migrations")
            applied = dict(cursor.fetchall())
            for path in files:
                body = path.read_text(encoding="utf-8")
                checksum = hashlib.sha256(body.encode("utf-8")).hexdigest()
                version = path.stem
                if version in applied:
                    state = "already_applied" if applied[version] == checksum else "CHECKSUM_MISMATCH"
                    plan.append({"version": version, "state": state})
                    continue
                cursor.execute(body)
                cursor.execute(
                    "INSERT INTO schema_migrations (version, checksum) VALUES (%s, %s)",
                    (version, checksum),
                )
                plan.append({"version": version, "state": "applied"})
        if dry_run:
            connection.rollback()
        else:
            connection.commit()
    return {
        "mode": "dry_run" if dry_run else "apply",
        "migrations_dir": str(migrations_dir),
        "plan": plan,
        "pending": [item["version"] for item in plan if item["state"] == "applied"],
        "checksum_mismatch": [item["version"] for item in plan if item["state"] == "CHECKSUM_MISMATCH"],
    }
