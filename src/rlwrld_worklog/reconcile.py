"""Count the same day in every layer, and against the source itself.

Finding the gaps this week took three hand-written probes: calendar events that
were collected but never projected, Notion blocks that were projected but
attributed to nobody, digests that were counted but never written because the
run was a dry run. Each looked identical from the report -- "나오지 않아" --
and each lived in a different layer.

So the layers are counted side by side:

    source  →  ledger  →  timeline  →  digest

A number that falls between two columns names the layer that dropped it. The
source column is the one HK pointed at: 그냥 슬랙/노션/구캘 검색하면 나와. It
is filled by asking the source the same question a person would type into its
search box.

Nothing here guesses. A source that cannot be asked -- no credentials, no API
for the question -- reports None, which renders as "측정 안 함" and never as 0,
because "nobody looked" and "there was nothing" are different answers and
confusing them is how a gap stays hidden.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Protocol

KST = timezone(timedelta(hours=9))

# The sources a person's day is assembled from. Slurm is included so that a
# missing cluster shows up too, even though nothing can ask it a second time.
SOURCES = ("slack", "notion", "google_calendar", "github", "slurm")


def day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, datetime.min.time(), tzinfo=KST)
    return start, start + timedelta(days=1)


@dataclass
class Row:
    """One day, one source, counted in each layer."""

    day: str
    source: str
    ledger: int
    timeline: int
    digest: int
    # None means not measured. Never coerced to 0.
    external: int | None = None
    note: str | None = None

    @property
    def verdict(self) -> str:
        """Which layer lost it, named in the order the data flows."""
        if self.external is not None and self.external > self.ledger:
            return "수집 누락"
        if self.ledger > 0 and self.timeline == 0:
            return "투영 누락"
        if self.timeline > 0 and self.digest == 0:
            return "다이제스트 누락"
        if self.external is not None and self.ledger > self.external:
            # Not a defect on its own: one meeting re-collected daily is
            # several ledger rows and one meeting.
            return "원장이 더 많음"
        return "ok"

    def as_dict(self) -> dict[str, Any]:
        return {
            "day": self.day,
            "source": self.source,
            "external": self.external,
            "ledger": self.ledger,
            "timeline": self.timeline,
            "digest": self.digest,
            "verdict": self.verdict,
            "note": self.note,
        }


class SlackSearch(Protocol):
    def call(self, method: str, **params: Any) -> dict[str, Any]: ...


def slack_day_count(client: SlackSearch, slack_user_id: str, day: date) -> int | None:
    """How many messages Slack itself says this person sent that day.

    The same query a person would type: `from:<@U…> on:2026-09-15`. Slack
    reports the total in the paging block, so this costs one call per day.
    """
    body = client.call(
        "search.messages",
        query=f"from:<@{slack_user_id}> on:{day.isoformat()}",
        count=1,
    )
    messages = body.get("messages")
    if not isinstance(messages, dict):
        return None
    total = messages.get("total")
    return int(total) if isinstance(total, int) else None


class CalendarList(Protocol):
    def list_events(self, calendar_id: str, **params: Any) -> Any: ...


def calendar_day_count(
    client: CalendarList, calendar_ids: list[str], day: date
) -> int | None:
    """How many events the calendars themselves hold for that day.

    Counted per calendar and summed, because an event a person attends lives on
    the organiser's calendar too and both are collected.
    """
    start, end = day_bounds(day)
    total = 0
    seen: set[str] = set()
    for calendar_id in calendar_ids:
        for event in client.list_events(
            calendar_id,
            timeMin=start.isoformat(),
            timeMax=end.isoformat(),
            singleEvents=True,
        ):
            identifier = str((event or {}).get("id") or "")
            if identifier and identifier in seen:
                continue
            if identifier:
                seen.add(identifier)
            total += 1
    return total


_LEDGER_SQL = """
    SELECT count(*) FROM ledger_records
     WHERE source = %(source)s
       AND source_created_at >= %(start)s AND source_created_at < %(end)s
"""

_LEDGER_BY_PERSON = {
    # How each source says "this record is that person's", in the ledger's own
    # relations. Mirrors what `_projection` reads, so a mismatch between these
    # two is itself a finding.
    "slack": "relations->>'author_user_id' = ANY(%(handles)s)",
    "notion": (
        "coalesce(relations->>'last_edited_by_user_id', relations->>'created_by_user_id') "
        "= ANY(%(handles)s)"
    ),
    "google_calendar": (
        "(lower(relations->>'organizer_email') = ANY(%(handles)s) OR EXISTS ("
        " SELECT 1 FROM jsonb_array_elements("
        "   coalesce(relations->'attendee_responses', '[]'::jsonb)) attendee"
        "  WHERE lower(attendee->>'email') = ANY(%(handles)s)))"
    ),
    "github": "relations->>'author' = ANY(%(handles)s)",
    "slurm": "relations->>'user' = ANY(%(handles)s)",
}


def person_handles(cursor, person_id: str) -> list[str]:
    """Every identity this person is known by, lowercased for email matching."""
    cursor.execute(
        "SELECT value FROM org_identity WHERE person_id = %s", (person_id,)
    )
    found = {str(row[0]) for row in cursor.fetchall()}
    return sorted(found | {value.lower() for value in found})


def count_layers(cursor, person_id: str, source: str, day: date) -> tuple[int, int, int]:
    """Ledger, timeline and digest counts for one person, source and day."""
    start, end = day_bounds(day)
    handles = person_handles(cursor, person_id)
    params = {"source": source, "start": start, "end": end, "handles": handles}

    predicate = _LEDGER_BY_PERSON.get(source)
    cursor.execute(f"{_LEDGER_SQL} AND {predicate}" if predicate else _LEDGER_SQL, params)
    ledger = int(cursor.fetchone()[0])

    cursor.execute(
        """
        SELECT count(*) FROM timeline_events event
          JOIN org_identity identity ON identity.value = event.actor_external_id
         WHERE event.source = %(source)s AND identity.person_id = %(person)s
           AND event.occurred_at >= %(start)s AND event.occurred_at < %(end)s
        """,
        {"source": source, "person": person_id, "start": start, "end": end},
    )
    timeline = int(cursor.fetchone()[0])

    cursor.execute(
        """
        SELECT coalesce((counts->'by_source'->>%(source)s)::int, 0)
          FROM person_day_digest WHERE person_id = %(person)s AND day = %(day)s
        """,
        {"source": source, "person": person_id, "day": day},
    )
    row = cursor.fetchone()
    digest = int(row[0]) if row else 0
    return ledger, timeline, digest


@dataclass
class ReconcileResult:
    rows: list[Row] = field(default_factory=list)
    unmeasured: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "rows": [row.as_dict() for row in self.rows],
            "gaps": [row.as_dict() for row in self.rows if row.verdict != "ok"],
            # Named, not silently absent: a source nobody could ask is a hole
            # in this check, and the check should say so.
            "unmeasured": sorted(set(self.unmeasured)),
        }


def reconcile(
    database_url: str,
    person_id: str,
    days: list[date],
    *,
    slack_client: SlackSearch | None = None,
    slack_user_id: str | None = None,
    calendar_client: CalendarList | None = None,
    calendar_ids: list[str] | None = None,
    sources: tuple[str, ...] = SOURCES,
) -> ReconcileResult:
    import psycopg

    result = ReconcileResult()
    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            for day in days:
                for source in sources:
                    ledger, timeline, digest = count_layers(cursor, person_id, source, day)
                    external: int | None = None
                    note: str | None = None
                    if source == "slack" and slack_client and slack_user_id:
                        external = slack_day_count(slack_client, slack_user_id, day)
                    elif source == "google_calendar" and calendar_client and calendar_ids:
                        external = calendar_day_count(calendar_client, calendar_ids, day)
                    if external is None and source in {"slack", "google_calendar"}:
                        note = "원본 미조회"
                        result.unmeasured.append(source)
                    elif source in {"notion", "github", "slurm"}:
                        # Notion's API cannot be asked "what did this person
                        # edit on this day" -- blocks are not searchable and the
                        # editor filter needs a plan this connection does not
                        # have. Saying so beats a column of zeros.
                        note = "원본 조회 불가"
                        result.unmeasured.append(source)
                    result.rows.append(
                        Row(
                            day=day.isoformat(),
                            source=source,
                            ledger=ledger,
                            timeline=timeline,
                            digest=digest,
                            external=external,
                            note=note,
                        )
                    )
    return result
