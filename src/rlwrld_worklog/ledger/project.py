"""Project already-loaded ledger records onto the activity timeline.

The loader projects as it loads, which covers everything it loads -- and
covers nothing it has already loaded. When GitHub and Slurm joined
`PROJECTED_ENTITY_TYPES` on 2026-09-11 their records were long since in
`ledger_records`, and `skip_unchanged` means the loader will never look at
those files again: every batch is skipped by sha256, so the projection would
have started from the next new record and left 13,266 loaded ones invisible
forever.

This module is the backfill, and it exists in a form that outlives this one
occasion: any future change to what gets projected, or to how, is followed by
running it. It reads `ledger_records` and writes the same three rows the
loader writes -- the timeline event, its observation, and the head that
selects the current observation -- from the same `_projection` function, so
there is one definition of what a record becomes and not two that can drift.

It never re-reads the ledger files and never touches `raw_payload`. Projection
is derived; repairing it is re-deriving it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .load import (
    EVENT_TYPE_BY_ENTITY,
    HEAD_UPSERT,
    OBSERVATION_UPSERT,
    PROJECTED_ENTITY_TYPES,
    TIMELINE_UPSERT,
    _origin_for,
    _payload_for_timeline,
    _projection,
    _timestamp,
)


def _moment(value: Any) -> datetime | None:
    """A timestamp from either a ledger file or a ledger row.

    The loader's `_timestamp` reads the ISO strings a JSONL record carries and
    returns None for anything else -- correct there, and wrong here: psycopg
    hands back `timestamptz` already parsed, so passing a row's value through
    it produced None for every record and the first run of this backfill
    reported every row as having no creation time. Both shapes arrive at this
    function, so both are handled in one place.
    """
    if isinstance(value, datetime):
        return value
    return _timestamp(value)


# The columns `_projection` and `_payload_for_timeline` read. Selected by name
# rather than with `*`, so a schema addition does not silently change what
# this reads, and every row is small enough that the whole backlog streams.
_COLUMNS = (
    "ledger_id",
    "schema_version",
    "capture_profile",
    "source",
    "entity_type",
    "source_entity_id",
    "source_created_at",
    "source_updated_at",
    "source_updated_at_status",
    "collected_at",
    "is_deleted",
    "scope",
    "relations",
    "raw_payload",
    "coverage",
    "observation_window",
    "capture_completeness",
    "denormalized_label_snapshot",
    "provenance",
)

_CANDIDATES = """
    SELECT {columns}
      FROM ledger_records r
     WHERE r.entity_type = ANY(%(entities)s)
       {filters}
     ORDER BY r.inserted_at
"""


@dataclass
class ProjectResult:
    dry_run: bool
    scanned: int = 0
    projected: int = 0
    already_present: int = 0
    without_occurred_at: int = 0
    heads_advanced: int = 0
    heads_not_advanced: int = 0
    by_event_type: dict[str, int] = field(default_factory=dict)
    without_actor: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "scanned": self.scanned,
            "projected": self.projected,
            "already_present": self.already_present,
            "without_occurred_at": self.without_occurred_at,
            "heads_advanced": self.heads_advanced,
            "heads_not_advanced": self.heads_not_advanced,
            "by_event_type": dict(sorted(self.by_event_type.items())),
            # Reported per source, because an actor the projection could not
            # read is a person missing from every report built on this row,
            # and a count of zero is the only reassuring form of this number.
            "without_actor": dict(sorted(self.without_actor.items())),
            "errors": self.errors[:20],
        }


def project_timeline(
    database_url: str,
    *,
    sources: tuple[str, ...] = (),
    entity_types: tuple[str, ...] = (),
    reproject: bool = False,
    dry_run: bool = False,
    batch_size: int = 2000,
) -> ProjectResult:
    """Project loaded ledger records that have no timeline event yet.

    `reproject` re-derives rows that already have one, for when the
    projection itself changed rather than the data. It is off by default: the
    normal case after a code change is the records nothing has projected, and
    rewriting 800k rows to fix 13k is not a repair, it is a rebuild.
    """
    import psycopg
    from psycopg.types.json import Jsonb

    result = ProjectResult(dry_run=dry_run)
    entities = sorted(set(entity_types) & PROJECTED_ENTITY_TYPES) if entity_types else sorted(
        PROJECTED_ENTITY_TYPES
    )
    if entity_types and not entities:
        result.errors.append(
            f"none of {sorted(set(entity_types))} is a projected entity type; "
            f"expected some of {sorted(PROJECTED_ENTITY_TYPES)}"
        )
        return result

    filters = []
    parameters: dict[str, Any] = {"entities": entities}
    if sources:
        filters.append("AND r.source = ANY(%(sources)s)")
        parameters["sources"] = list(sources)
    if not reproject:
        filters.append(
            "AND NOT EXISTS (SELECT 1 FROM timeline_events e WHERE e.event_id = r.ledger_id)"
        )
    query = _CANDIDATES.format(
        columns=", ".join(f"r.{name}" for name in _COLUMNS),
        filters="\n       ".join(filters),
    )
    ingested_at = datetime.now(timezone.utc)

    with psycopg.connect(database_url) as connection:
        connection.autocommit = False
        # Server-side: the candidate set is the entire projected backlog and
        # each row carries a jsonb payload. Materialising it here would hold
        # the whole ledger in memory to write rows one at a time.
        with connection.cursor(name="timeline_candidates") as reader:
            reader.itersize = batch_size
            reader.execute(query, parameters)
            with connection.cursor() as writer:
                for row in reader:
                    record = dict(zip(_COLUMNS, row))
                    record["ledger_id"] = str(record["ledger_id"])
                    result.scanned += 1
                    _project_one(
                        writer, record, result, ingested_at=ingested_at, Jsonb=Jsonb,
                        dry_run=dry_run,
                    )
        if dry_run:
            connection.rollback()
        else:
            connection.commit()
    return result


def _project_one(cursor, record, result: ProjectResult, *, ingested_at, Jsonb, dry_run) -> None:
    occurred_at = _moment(record.get("source_created_at"))
    if occurred_at is None:
        # The loader reports this the same way and for the same reason: an
        # event with no time cannot sit on a timeline, and dropping it
        # silently would make the timeline quietly incomplete.
        result.without_occurred_at += 1
        result.errors.append(f"{record['ledger_id']}: no source_created_at, not projected")
        return

    projection = _projection(record)
    event_type = EVENT_TYPE_BY_ENTITY[record["entity_type"]]
    if projection["actor"] is None:
        source = str(record["source"])
        result.without_actor[source] = result.without_actor.get(source, 0) + 1

    if not dry_run:
        cursor.execute(
            TIMELINE_UPSERT,
            {
                "event_id": record["ledger_id"],
                "source": record["source"],
                "event_type": event_type,
                "external_id": record["source_entity_id"],
                "actor_external_id": projection["actor"],
                "occurred_at": occurred_at,
                "updated_at": _moment(record.get("source_updated_at")),
                "ingested_at": ingested_at,
                "container_id": projection["container"],
                "thread_id": projection["thread"],
                "permalink": projection["permalink"],
                "classifications": ["unclassified"],
                "payload": Jsonb(_payload_for_timeline(record, projection)),
            },
        )
        origin, origin_priority = _origin_for(record)
        cursor.execute(
            OBSERVATION_UPSERT,
            {
                "id": record["ledger_id"],
                "source": record["source"],
                "object_type": record["entity_type"],
                "external_id": record["source_entity_id"],
                "origin": origin,
                "origin_priority": origin_priority,
                "observed_at": _moment(record.get("collected_at")) or occurred_at,
                "remote_updated_at": _moment(record.get("source_updated_at")),
                "is_deleted": bool(record.get("is_deleted")),
                "payload": Jsonb({"ledger_id": record["ledger_id"]}),
                "timeline_event_id": record["ledger_id"],
            },
        )
        # The head is what `current_timeline_events` selects, so a backfill
        # that wrote only the event would produce rows the view cannot see --
        # present in the table, absent from every screen.
        cursor.execute(
            HEAD_UPSERT,
            (
                record["source"],
                record["entity_type"],
                record["source_entity_id"],
                record["ledger_id"],
                origin,
                origin_priority,
                _moment(record.get("source_updated_at")),
            ),
        )
        if cursor.rowcount:
            result.heads_advanced += 1
        else:
            result.heads_not_advanced += 1

    result.projected += 1
    result.by_event_type[event_type] = result.by_event_type.get(event_type, 0) + 1


def timeline_status(database_url: str) -> dict[str, Any]:
    """Ledger records against timeline events, per source.

    The number this exists to make unmissable is `unprojected`: on
    2026-09-10 it was 1,710 for github and 11,556 for slurm while every
    collection reported `ok`.
    """
    import psycopg

    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT r.source,
                       count(*) FILTER (WHERE r.entity_type = ANY(%(entities)s)) AS projectable,
                       count(e.event_id) AS projected,
                       count(*) FILTER (
                           WHERE r.entity_type = ANY(%(entities)s) AND e.event_id IS NULL
                       ) AS unprojected,
                       count(*) AS ledger_records
                  FROM ledger_records r
                  LEFT JOIN timeline_events e ON e.event_id = r.ledger_id
                 GROUP BY r.source
                 ORDER BY r.source
                """,
                {"entities": sorted(PROJECTED_ENTITY_TYPES)},
            )
            by_source = [
                {
                    "source": row[0],
                    "projectable": row[1],
                    "projected": row[2],
                    "unprojected": row[3],
                    "ledger_records": row[4],
                }
                for row in cursor.fetchall()
            ]
            cursor.execute(
                """
                SELECT source, event_type, count(*),
                       count(*) FILTER (WHERE actor_external_id IS NULL)
                  FROM timeline_events
                 GROUP BY source, event_type ORDER BY source, event_type
                """
            )
            by_event_type = [
                {"source": row[0], "event_type": row[1], "events": row[2], "without_actor": row[3]}
                for row in cursor.fetchall()
            ]
    return {
        "by_source": by_source,
        "by_event_type": by_event_type,
        "unprojected": sum(item["unprojected"] for item in by_source),
    }
