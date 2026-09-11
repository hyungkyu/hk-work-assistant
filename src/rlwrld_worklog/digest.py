"""Per-person daily digests, built by a batch from the timeline.

What this is not: a summary. There is no model in this path and no selection
of "important" activity. HK, 2026-09-11: 모든 액티비티를 시간순으로,
기계적으로 -- every activity, in time order, mechanically. A digest that
quietly drops the long tail cannot answer the one question it exists for,
which is what a day actually consisted of.

Each event line is assembled from fields the collectors already recorded: the
time, the source, the event type, the container it happened in, a title the
source itself provided, and the permalink. Where a source gave no title, the
line says what the event was and where, and nothing more -- an empty title is
reported as empty rather than filled in with a sentence nobody wrote.

The day is KST, because that is the day the people being described worked.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

KST = timezone(timedelta(hours=9))

GENERATOR = "digest/1"

# A sanity bound, not an editorial one. No real day reaches it; a person who
# does has something automated running under their account, and the row says
# it was truncated and by how much rather than pretending it was the whole
# day.
MAX_EVENTS = 5000

# Where each event type's human-readable title comes from. The timeline keeps
# a pointer to the ledger rather than a copy of the payload, so titles come
# from the label snapshot the collector recorded at capture time.
_TITLE_KEYS = ("title", "subject", "name", "job_name", "summary")


def _title(labels: dict[str, Any]) -> str | None:
    for key in _TITLE_KEYS:
        value = labels.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def kst_day_bounds(day: date) -> tuple[datetime, datetime]:
    """[00:00, 24:00) KST for one day, as instants."""
    start = datetime.combine(day, datetime.min.time(), tzinfo=KST)
    return start, start + timedelta(days=1)


@dataclass
class DigestResult:
    dry_run: bool
    day: str = ""
    people: int = 0
    people_with_activity: int = 0
    events: int = 0
    truncated_people: list[str] = field(default_factory=list)
    unattributed_events: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "day": self.day,
            "people": self.people,
            "people_with_activity": self.people_with_activity,
            "events": self.events,
            "truncated_people": self.truncated_people,
            # Activity whose actor maps to nobody. Not an error and not
            # hidden: it is the size of what the org chart cannot account
            # for, and it belongs next to the number it is missing from.
            "unattributed_events": self.unattributed_events,
            "errors": self.errors[:20],
            "generator": GENERATOR,
        }


_EVENTS_SQL = """
    SELECT identity.person_id,
           event.occurred_at,
           event.source,
           event.event_type,
           event.container_id,
           event.thread_id,
           event.permalink,
           event.actor_external_id,
           event.payload
      FROM timeline_events event
      JOIN org_identity identity
        ON identity.value = event.actor_external_id
       AND identity.kind = ANY(%(kinds)s)
     WHERE event.occurred_at >= %(start)s
       AND event.occurred_at < %(end)s
     ORDER BY identity.person_id, event.occurred_at, event.event_type
"""

# Which identity kinds an actor handle can match, by the `actor_kind` the
# projection recorded. A GitHub login must not match a Slack identity that
# happens to be the same string.
_KIND_BY_ACTOR_KIND = {
    "github_login": "github",
    "slurm_user": "slurm",
    "git_email": "email_official",
}

_ALL_KINDS = (
    "github",
    "slurm",
    "slack",
    "notion",
    "email_official",
    "email_personal",
    "email_school",
)


def build_day(
    database_url: str,
    day: date,
    *,
    dry_run: bool = False,
) -> DigestResult:
    """Build (or rebuild) every person's digest for one KST day."""
    import psycopg
    from psycopg.types.json import Jsonb

    result = DigestResult(dry_run=dry_run, day=day.isoformat())
    start, end = kst_day_bounds(day)

    with psycopg.connect(database_url) as connection:
        connection.autocommit = False
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM org_person")
            result.people = int(cursor.fetchone()[0])

            cursor.execute(
                """
                SELECT count(*) FROM timeline_events event
                 WHERE event.occurred_at >= %(start)s AND event.occurred_at < %(end)s
                   AND NOT EXISTS (
                       SELECT 1 FROM org_identity identity
                        WHERE identity.value = event.actor_external_id
                   )
                """,
                {"start": start, "end": end},
            )
            result.unattributed_events = int(cursor.fetchone()[0])

            cursor.execute(_EVENTS_SQL, {"start": start, "end": end, "kinds": list(_ALL_KINDS)})
            rows = cursor.fetchall()

            current: str | None = None
            events: list[dict] = []
            for row in rows:
                person_id = row[0]
                if current is not None and person_id != current:
                    _write(cursor, current, day, events, result, Jsonb)
                    events = []
                current = person_id
                events.append(_event(row))
            if current is not None:
                _write(cursor, current, day, events, result, Jsonb)

            if dry_run:
                connection.rollback()
            else:
                connection.commit()
    return result


def _event(row) -> dict[str, Any]:
    (
        _person_id,
        occurred_at,
        source,
        event_type,
        container_id,
        thread_id,
        permalink,
        actor_external_id,
        payload,
    ) = row
    labels = (payload or {}).get("labels") or {}
    return {
        "at": occurred_at.astimezone(KST).isoformat(),
        "time": occurred_at.astimezone(KST).strftime("%H:%M"),
        "source": source,
        "event_type": event_type,
        "container": container_id,
        "thread": thread_id,
        "permalink": permalink,
        "actor": actor_external_id,
        # None, not "". An absent title is a fact about the source, and an
        # empty string reads as a title that happens to be blank.
        "title": _title(labels if isinstance(labels, dict) else {}),
    }


def _counts(events: list[dict]) -> dict[str, Any]:
    by_source: dict[str, int] = {}
    by_event_type: dict[str, int] = {}
    for event in events:
        by_source[event["source"]] = by_source.get(event["source"], 0) + 1
        by_event_type[event["event_type"]] = by_event_type.get(event["event_type"], 0) + 1
    return {
        "by_source": dict(sorted(by_source.items())),
        "by_event_type": dict(sorted(by_event_type.items())),
    }


def _write(cursor, person_id: str, day: date, events: list[dict], result: DigestResult, Jsonb):
    truncated = None
    if len(events) > MAX_EVENTS:
        truncated = len(events)
        events = events[:MAX_EVENTS]
        result.truncated_people.append(person_id)
    cursor.execute(
        """
        INSERT INTO person_day_digest
            (person_id, day, generated_at, generator, events_total, counts, events, truncated_at)
        VALUES (%s, %s, now(), %s, %s, %s, %s, %s)
        ON CONFLICT (person_id, day) DO UPDATE SET
            generated_at = now(),
            generator = EXCLUDED.generator,
            events_total = EXCLUDED.events_total,
            counts = EXCLUDED.counts,
            events = EXCLUDED.events,
            truncated_at = EXCLUDED.truncated_at
        """,
        (
            person_id,
            day,
            GENERATOR,
            truncated or len(events),
            Jsonb(_counts(events)),
            Jsonb(events),
            truncated,
        ),
    )
    result.people_with_activity += 1
    result.events += len(events)


def build_range(
    database_url: str, start: date, end: date, *, dry_run: bool = False
) -> list[dict[str, Any]]:
    """Every day in [start, end], oldest first. The backfill path."""
    out = []
    day = start
    while day <= end:
        out.append(build_day(database_url, day, dry_run=dry_run).as_dict())
        day += timedelta(days=1)
    return out


def read_digest(database_url: str, person_id: str, day: date) -> dict[str, Any] | None:
    """One person's day, as the batch stored it."""
    import psycopg

    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT d.generated_at, d.generator, d.events_total, d.counts,
                       d.events, d.truncated_at, p.name
                  FROM person_day_digest d
                  JOIN org_person p ON p.person_id = d.person_id
                 WHERE d.person_id = %s AND d.day = %s
                """,
                (person_id, day),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            cursor.execute(
                """
                SELECT s.nickname, s.title, s.employment_type, s.affiliation,
                       s.access_level, s.status, s.department_raw
                  FROM org_person_state s
                 WHERE s.person_id = %s
                 ORDER BY s.observation_id DESC LIMIT 1
                """,
                (person_id,),
            )
            state = cursor.fetchone()
            cursor.execute(
                "SELECT kind, value FROM org_identity WHERE person_id = %s ORDER BY kind, value",
                (person_id,),
            )
            identities = [{"kind": row_[0], "value": row_[1]} for row_ in cursor.fetchall()]

    generated_at, generator, events_total, counts, events, truncated_at, name = row
    return {
        "person_id": person_id,
        "name": name,
        "day": day.isoformat(),
        "generated_at": generated_at.isoformat(),
        "generator": generator,
        "events_total": events_total,
        "counts": counts,
        "events": events,
        "truncated_at": truncated_at,
        "state": (
            {
                "nickname": state[0],
                "title": state[1],
                "employment_type": state[2],
                "affiliation": state[3],
                "access_level": state[4],
                "status": state[5],
                "department_raw": state[6],
            }
            if state
            else None
        ),
        "identities": identities,
    }


def digest_status(database_url: str) -> dict[str, Any]:
    """Which days have digests, and how far back they go."""
    import psycopg

    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT min(day), max(day), count(DISTINCT day), count(*), sum(events_total)
                  FROM person_day_digest
                """
            )
            row = cursor.fetchone()
    return {
        "first_day": row[0].isoformat() if row and row[0] else None,
        "last_day": row[1].isoformat() if row and row[1] else None,
        "days": row[2] or 0,
        "rows": row[3] or 0,
        "events": int(row[4] or 0),
    }
