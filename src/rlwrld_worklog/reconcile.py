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
from pathlib import Path
from typing import Any, Protocol

KST = timezone(timedelta(hours=9))

# The sources a person's day is assembled from. Slurm is included so that a
# missing cluster shows up too, even though nothing can ask it a second time.
SOURCES = ("slack", "notion", "google_calendar", "github", "slurm")


def running_code() -> str:
    """Which build produced this table.

    Three times in one day a run was read as "the fix did not work" when the
    fix had simply not been applied yet -- the patch queue ticks every three
    minutes and the numbers look identical until it does. A check whose output
    cannot be dated is a check that wastes a round trip, so it says which
    commit it is.
    """
    import subprocess

    here = Path(__file__).resolve().parent
    try:
        found = subprocess.run(
            ["git", "-C", str(here), "log", "--oneline", "-1"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if found.returncode == 0 and found.stdout.strip():
            return found.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return "버전 알 수 없음"


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
    def iter_day_events(self, calendar_id: str, *, time_min: str, time_max: str) -> Any: ...


def event_involves(event: Any, emails: set[str]) -> bool:
    """Is this meeting this person's, in the same sense the ledger means it?

    The first version of this counted every event visible on every calendar the
    account can see -- subscribed calendars, public holidays, other people's
    schedules -- and compared it with a ledger count filtered to events the
    person organises or attends. 72 against 18 read as 수집 누락 and was
    nothing of the kind. A comparison is only a comparison when both sides ask
    the same question.
    """
    event = event if isinstance(event, dict) else {}
    organiser = (event.get("organizer") or {}).get("email")
    creator = (event.get("creator") or {}).get("email")
    for value in (organiser, creator):
        if isinstance(value, str) and value.lower() in emails:
            return True
    for attendee in event.get("attendees") or []:
        if not isinstance(attendee, dict):
            continue
        value = attendee.get("email")
        if isinstance(value, str) and value.lower() in emails:
            # A declined invitation is not attendance, and the ledger side of
            # this comparison excludes it too.
            return attendee.get("responseStatus") != "declined"
    return False


def calendar_day_count(
    client: CalendarList,
    calendar_ids: list[str],
    day: date,
    emails: set[str] | None = None,
) -> int | None:
    """How many of that day's meetings are this person's.

    Deduplicated by event id, because a meeting a person attends sits on the
    organiser's calendar as well as their own and both are readable here.
    """
    start, end = day_bounds(day)
    seen: set[str] = set()
    total = 0
    for calendar_id in calendar_ids:
        for event in client.iter_day_events(
            calendar_id, time_min=start.isoformat(), time_max=end.isoformat()
        ):
            if emails and not event_involves(event, emails):
                continue
            identifier = str((event or {}).get("id") or "")
            if identifier:
                if identifier in seen:
                    continue
                seen.add(identifier)
            total += 1
    return total


# Distinct objects, not observations. The same Slack message is legitimately
# recorded twice -- once by the Web API capture and once by the search
# supplement -- and counting rows made a correct ledger look like it was
# inflating by 2-3x against the source.
_LEDGER_SQL = """
    SELECT count(DISTINCT coalesce(raw_payload->>'iCalUID', source_entity_id))
      FROM ledger_records
     WHERE source = %(source)s
       AND {window}
"""

# When the thing happened, per source. For everything but the calendar that is
# the creation time. A meeting is created when it is booked and happens later,
# so asking "how many meetings on Tuesday" against the creation time compares
# two different days and drifts in both directions -- which is exactly what the
# first clean-looking run did.
_CREATED_WINDOW = "source_created_at >= %(start)s AND source_created_at < %(end)s"
_WINDOW_BY_SOURCE = {
    "google_calendar": (
        "((raw_payload->'start'->>'dateTime') IS NOT NULL"
        " AND (raw_payload->'start'->>'dateTime')::timestamptz >= %(start)s"
        " AND (raw_payload->'start'->>'dateTime')::timestamptz < %(end)s)"
        " OR ((raw_payload->'start'->>'dateTime') IS NULL"
        "     AND (raw_payload->'start'->>'date') = %(day)s)"
    ),
}

_LEDGER_BY_PERSON = {
    # How each source says "this record is that person's", in the ledger's own
    # relations. Mirrors what `_projection` reads, so a mismatch between these
    # two is itself a finding.
    # Authored by them, or naming them. The digest counts both -- HK:
    # 내가 생성하지 않았지만, 내가 멘션되었거나 하는 것도 같이 보여줘 -- so a
    # ledger column that counted only authorship was comparing a narrower
    # question with the two columns beside it and reporting the difference as
    # a defect.
    "slack": (
        "(relations->>'author_user_id' = ANY(%(handles)s)"
        " OR EXISTS (SELECT 1 FROM regexp_matches("
        "     coalesce(raw_payload->>'text', ''), '<@([A-Z0-9]+)', 'g') AS found"
        "  WHERE found[1] = ANY(%(handles)s)))"
    ),
    "notion": (
        "(coalesce(relations->>'last_edited_by_user_id', relations->>'created_by_user_id') "
        "= ANY(%(handles)s)"
        " OR EXISTS (SELECT 1 FROM jsonb_array_elements_text("
        "     coalesce(relations->'mentioned_user_ids', '[]'::jsonb)) AS named"
        "  WHERE named = ANY(%(handles)s)))"
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
    params = {
        "source": source,
        "start": start,
        "end": end,
        "handles": handles,
        "day": day.isoformat(),
    }

    window = _WINDOW_BY_SOURCE.get(source, _CREATED_WINDOW)
    ledger_sql = _LEDGER_SQL.format(window=f"({window})")
    predicate = _LEDGER_BY_PERSON.get(source)
    cursor.execute(f"{ledger_sql} AND {predicate}" if predicate else ledger_sql, params)
    ledger = int(cursor.fetchone()[0])

    # Projected, by the same definition of "this person's" the other two
    # columns use. Matching on `actor_external_id` alone asked a third
    # question: a meeting the person attends but does not organise has
    # somebody else's actor, so 2026-09-11 read "투영 누락, 5 -> 0" for events
    # that were projected, attributed correctly and shown in the report. The
    # column exists to say whether projection ran, so it is matched through
    # the ledger record rather than through the actor.
    cursor.execute(
        f"""
        SELECT count(DISTINCT coalesce(ledger.raw_payload->>'iCalUID', event.external_id))
          FROM timeline_events event
          JOIN ledger_records ledger ON ledger.ledger_id = event.event_id
         WHERE event.source = %(source)s
           AND event.occurred_at >= %(start)s AND event.occurred_at < %(end)s
           {"AND " + predicate if predicate else ""}
        """,
        params,
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
            # The person's own addresses, so the source side of the calendar
            # comparison filters by exactly what the ledger side filters by.
            cursor.execute(
                "SELECT lower(value) FROM org_identity WHERE person_id = %s "
                "AND kind LIKE 'email%%'",
                (person_id,),
            )
            emails = {str(row[0]) for row in cursor.fetchall()}
            for day in days:
                for source in sources:
                    ledger, timeline, digest = count_layers(cursor, person_id, source, day)
                    external: int | None = None
                    note: str | None = None
                    if source == "slack" and slack_client and slack_user_id:
                        external = slack_day_count(slack_client, slack_user_id, day)
                    elif source == "google_calendar" and calendar_client and calendar_ids:
                        external = calendar_day_count(
                            calendar_client, calendar_ids, day, emails
                        )
                    if external is None and source in {"slack", "google_calendar"}:
                        note = "원본 미조회"
                        result.unmeasured.append(source)
                    elif source == "notion":
                        # Notion cannot be asked "what did this person edit on
                        # this day": blocks are not searchable and the editor
                        # filter needs a plan this connection does not have.
                        note = "원본 조회 불가 (노션 API 한계)"
                        result.unmeasured.append(source)
                    elif source in {"github", "slurm"}:
                        note = "원본 대조 미구현"
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
