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

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

KST = timezone(timedelta(hours=9))


def _kst_today() -> date:
    """Today in KST, the only calendar this system uses."""
    return (datetime.now(timezone.utc) + timedelta(hours=9)).date()


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


# A line that says only "slack · #general · 제목 없음" tells a reader nothing
# about what happened. The timeline keeps a pointer to the ledger rather than
# a copy of the payload (load.py `_payload_for_timeline`), so the words come
# from `ledger_records.raw_payload`, read through a join -- no reprojection and
# no second copy of every message.
EXCERPT_CHARS = 220

# Where the words live, by entity type. First key present wins; a pair means
# "headline, then body" joined by an em dash, which is what makes a PR read as
# "title -- what the description says" rather than as a bare title.
_BODY_KEYS: dict[str, tuple[str, ...]] = {
    "message": ("text",),
    "comment": ("body", "text"),
    "page": ("title",),
    "event": ("summary", "description"),
    "commit": ("message",),
    "pull_request": ("title", "body"),
    "issue": ("title", "body"),
    "review": ("state", "body"),
    "review_comment": ("body",),
    "issue_comment": ("body",),
    "job": ("job_name", "name"),
}


def _text_at(raw: dict[str, Any], key: str) -> str | None:
    """One text field, including the nested places a source hides it."""
    value = raw.get(key)
    if value is None and key == "message" and isinstance(raw.get("commit"), dict):
        # A GitHub commit's message sits under `commit`, not at the top level.
        value = raw["commit"].get("message")
    if isinstance(value, dict):
        # Notion titles arrive as rich text; the plain rendering is the part a
        # person reads.
        value = value.get("plain_text") or value.get("content")
    if isinstance(value, list):
        parts = [
            item.get("plain_text") or item.get("text", {}).get("content")
            for item in value
            if isinstance(item, dict)
        ]
        value = " ".join(part for part in parts if part)
    if not isinstance(value, str):
        return None
    collapsed = " ".join(value.split())
    return collapsed or None


def excerpt(
    entity_type: str | None, raw_payload: Any, *, names: dict[str, str] | None = None
) -> str | None:
    """A short, readable "what this was" for one timeline line.

    None rather than "" when the payload holds no words: an absent excerpt is
    a fact about the record, and an empty string reads as an empty message.
    """
    if not isinstance(raw_payload, dict):
        return None
    if entity_type == "block":
        # A block keeps its words in rich_text under a key named after its own
        # type (paragraph, heading_2, to_do ...), so there is no single field
        # to read -- the recursive walk is the only shape-independent way in.
        from .normalizers import _plain_text

        found = " ".join(_plain_text(raw_payload).split())
        if not found:
            return None
        found = _readable(found, names)
        return found if len(found) <= EXCERPT_CHARS else found[: EXCERPT_CHARS - 1] + "…"
    parts: list[str] = []
    for key in _BODY_KEYS.get(entity_type or "", ()):
        found = _text_at(raw_payload, key)
        if found and found not in parts:
            parts.append(found)
    if not parts:
        return None
    joined = _readable(" — ".join(parts), names)
    if len(joined) <= EXCERPT_CHARS:
        return joined
    return joined[: EXCERPT_CHARS - 1].rstrip() + "…"


_MENTION = re.compile(r"<@([A-Z0-9]+)(?:\|[^>]*)?>")
_CHANNEL_LINK = re.compile(r"<#[A-Z0-9]+\|([^>]*)>")
_URL_LINK = re.compile(r"<(https?://[^|>]+)(?:\|([^>]*))?>")


def _readable(text: str, names: dict[str, str] | None) -> str:
    """Slack's wire markup, as a person reads it.

    `<@U07EKRU6F7H>` is who the message was addressed to, and leaving it as an
    id makes that fact unreadable. An id with no roster entry keeps its id
    rather than becoming a plausible-looking name.
    """
    lookup = names or {}

    def mention(match: re.Match[str]) -> str:
        found = lookup.get(match.group(1))
        return f"@{found}" if found else match.group(0)

    text = _MENTION.sub(mention, text)
    text = _CHANNEL_LINK.sub(lambda m: f"#{m.group(1)}", text)
    return _URL_LINK.sub(lambda m: m.group(2) or m.group(1), text)


def audience(
    source: str,
    raw: Any,
    container_id: str | None,
    parent_relations: Any,
    names: dict[str, str] | None,
    places: dict[str, dict[str, Any]] | None,
) -> list[str]:
    """Who this was said to.

    HK, 2026-09-16: 내가 한말이 중요한게 아니라, 내가 누구에게 무슨 이야기를
    했느냐가 중요해. A message's audience is not in any one field: it is the
    people it names, the person on the other end of a DM, and -- for a reply --
    whoever started the thread.
    """
    if source != "slack":
        return []
    raw = raw if isinstance(raw, dict) else {}
    lookup = names or {}
    found: list[str] = []

    def add(value: str | None) -> None:
        if value and value not in found:
            found.append(value)

    place = (places or {}).get(str(container_id)) if container_id else None
    if place and place.get("private") and "·" in place["label"]:
        for part in place["label"].split("·", 1)[1].split(","):
            add(part.strip())

    parent = parent_relations if isinstance(parent_relations, dict) else {}
    author = parent.get("author_user_id")
    if author:
        add(lookup.get(str(author)) or str(author))

    text = raw.get("text")
    if isinstance(text, str):
        for handle in _MENTION.findall(text):
            add(lookup.get(handle) or f"@{handle}")
    return found


def _where(
    source: str,
    entity_type: str | None,
    labels: Any,
    raw: Any,
    container_id: str | None,
    parent_raw: Any = None,
    places: dict[str, str] | None = None,
) -> str | None:
    """The place, in the source's own words rather than as an id."""
    labels = labels if isinstance(labels, dict) else {}
    raw = raw if isinstance(raw, dict) else {}
    if source == "slack":
        # The conversation directory knows DMs by who is in them; the label
        # snapshot only ever holds a channel name, which a DM does not have.
        if places and container_id in places:
            return places[container_id]["label"]
        channel = labels.get("channel_name")
        return f"#{channel}" if channel else container_id
    if source == "notion":
        # A page is its own document; a block or comment hangs off one, and
        # the document is the place a person would go looking.
        found = (
            _plain_notion_title(parent_raw if isinstance(parent_raw, dict) else {})
            or _text_at(raw, "title")
            or _plain_notion_title(raw)
            or labels.get("name")
        )
        return found or container_id
    if source == "google_calendar":
        return labels.get("calendar_summary") or labels.get("calendar_name") or container_id
    return container_id


def _plain_notion_title(raw: dict[str, Any]) -> str | None:
    """A Notion page keeps its title inside `properties`, shape varying by database."""
    from .normalizers import _plain_text

    properties = raw.get("properties")
    if not isinstance(properties, dict):
        return None
    found = " ".join(_plain_text(properties).split())
    return found[:200] or None


def links(source: str, raw: Any) -> list[dict[str, str]]:
    """Named links the event itself carries, beyond its own permalink.

    A meeting's Gemini notes arrive as a calendar attachment, which is the one
    thing a person actually wants to click afterwards -- the event link only
    shows the invitation again.
    """
    raw = raw if isinstance(raw, dict) else {}
    found: list[dict[str, str]] = []
    if source == "google_calendar":
        for attachment in raw.get("attachments") or []:
            if not isinstance(attachment, dict):
                continue
            url = attachment.get("fileUrl")
            if not isinstance(url, str) or not url:
                continue
            title = attachment.get("title")
            found.append({"url": url, "title": str(title) if title else "첨부"})
        conference = raw.get("conferenceData")
        if isinstance(conference, dict):
            for entry in conference.get("entryPoints") or []:
                if isinstance(entry, dict) and entry.get("entryPointType") == "video":
                    uri = entry.get("uri")
                    if isinstance(uri, str) and uri:
                        found.append({"url": uri, "title": "회의 참여"})
                        break
    return found


_PLACES_SQL = """
    SELECT DISTINCT ON (source_entity_id)
           source_entity_id,
           raw_payload,
           relations
      FROM ledger_records
     WHERE source = 'slack' AND entity_type = 'conversation'
     ORDER BY source_entity_id, collected_at DESC
"""


def slack_place(raw: Any, relations: Any, names: dict[str, str]) -> dict[str, Any] | None:
    """What to call a Slack conversation.

    A channel has a name. A DM does not, and "D0C09PW4T60" tells a reader
    nothing about who they were talking to -- which, for a person whose day is
    half DMs, is most of the day. So a DM is named by the person on the other
    end, and a group DM by its members.
    """
    raw = raw if isinstance(raw, dict) else {}
    relations = relations if isinstance(relations, dict) else {}
    # A DM and a private channel are not the same thing as #general, and a
    # report that shows them identically invites someone to quote one in the
    # open. The flag rides with the name so the page can mark it.
    if raw.get("is_im"):
        other = raw.get("user")
        found = names.get(str(other)) if other else None
        return {"label": f"DM · {found}" if found else "DM", "private": True}
    if raw.get("is_mpim"):
        members = [
            names[str(member)]
            for member in (relations.get("member_user_ids") or [])
            if str(member) in names
        ]
        label = f"그룹DM · {', '.join(members[:4])}" if members else "그룹DM"
        return {"label": label, "private": True}
    name = raw.get("name")
    if not name:
        return None
    return {"label": f"#{name}", "private": bool(raw.get("is_private"))}


def _slack_prefix(rows) -> str | None:
    """The workspace's archive URL prefix, learned from the day's own rows."""
    for row in rows:
        source, permalink = row[2], row[6]
        if source == "slack" and isinstance(permalink, str) and "/archives/" in permalink:
            return permalink.split("/archives/", 1)[0]
    return None


def _slack_permalink(raw: Any, container_id: str | None, prefix: str | None) -> str | None:
    """A Slack link built from ids, for records the API did not hand one to.

    Messages fetched through search carry no permalink, so half a person's day
    had nothing to click. The archive URL is a pure function of the channel id
    and the timestamp; the workspace prefix is taken from a permalink the same
    day already provided rather than hardcoded, so this never invents a
    workspace that does not exist.
    """
    raw = raw if isinstance(raw, dict) else {}
    stamp = raw.get("ts")
    if not prefix or not container_id or not isinstance(stamp, str) or "." not in stamp:
        return None
    return f"{prefix}/archives/{container_id}/p{stamp.replace('.', '')}"


def _detail(source: str, entity_type: str | None, raw: Any, thread_id: str | None) -> str | None:
    """The one fact this source needs that the shared columns cannot hold."""
    raw = raw if isinstance(raw, dict) else {}
    if source == "google_calendar":
        span = _time_span(raw)
        attendees = raw.get("attendees")
        people = f"{len(attendees)}명" if isinstance(attendees, list) and attendees else None
        return " · ".join(part for part in (span, people) if part) or None
    if source == "slack" and thread_id:
        return "스레드 답글"
    if entity_type == "review":
        state = raw.get("state")
        return str(state).lower() if state else None
    return None


def _time_span(raw: dict[str, Any]) -> str | None:
    """A meeting's clock time in KST, from Google's start/end pair."""
    def one(key: str) -> str | None:
        value = raw.get(key)
        if not isinstance(value, dict):
            return None
        stamp = value.get("dateTime")
        if not isinstance(stamp, str):
            # An all-day event has `date` and no clock time. Saying so beats
            # inventing 00:00.
            return "종일" if value.get("date") else None
        try:
            return datetime.fromisoformat(stamp).astimezone(KST).strftime("%H:%M")
        except ValueError:
            return None

    start, end = one("start"), one("end")
    if start == "종일":
        return "종일"
    if start and end:
        return f"{start}–{end}"
    return start or end


def collapse(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fold repeats of the same thing into one line carrying a count.

    A Notion page saved twenty times is twenty timeline rows and one piece of
    news. Identity is (source, where, excerpt): the same words in the same
    place. Times are kept as a range rather than dropped, because when it
    started and when it stopped are both part of what happened.
    """
    folded: list[dict[str, Any]] = []
    index: dict[tuple, int] = {}
    for event in events:
        fold_by = event.get("fold_by")
        if fold_by == "document":
            key = (event.get("source"), event.get("where"), "")
        elif fold_by == "thread":
            key = (event.get("source"), event.get("thread"), "")
        else:
            key = (event.get("source"), event.get("where"), event.get("excerpt"))
        foldable = fold_by is not None or event.get("excerpt") is not None
        if not foldable or key not in index:
            index[key] = len(folded)
            folded.append({**event, "repeat": 1})
            continue
        seen = folded[index[key]]
        seen["repeat"] += 1
        seen["last_time"] = event.get("time")
    return folded


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


# Who an event belongs to. For every source but one that is the actor: the
# person who sent, committed, wrote or submitted it. A meeting is different --
# what a person did yesterday was *attend* it, and a timeline row has one actor
# column, so the organiser alone would hide every meeting from everyone who sat
# in it. The second arm is that fan-out, done here rather than in the schema:
# one event legitimately belongs to many people's days. A declined invitation is
# not attendance and is left out; UNION folds the organiser's two matches into
# one row.
_EVENTS_SQL = """
    WITH matched AS (
        SELECT identity.person_id, event.event_id
          FROM timeline_events event
          JOIN org_identity identity
            ON identity.value = event.actor_external_id
           AND identity.kind = ANY(%(kinds)s)
         WHERE event.occurred_at >= %(start)s
           AND event.occurred_at < %(end)s
        UNION
        SELECT identity.person_id, event.event_id
          FROM timeline_events event
          JOIN ledger_records ledger
            ON ledger.ledger_id = event.event_id
          CROSS JOIN LATERAL jsonb_array_elements(
              coalesce(ledger.relations->'attendee_responses', '[]'::jsonb)
          ) AS attendee
          JOIN org_identity identity
            ON lower(identity.value) = lower(attendee->>'email')
           AND identity.kind = ANY(%(kinds)s)
         WHERE event.source = 'google_calendar'
           AND coalesce(attendee->>'responseStatus', '') <> 'declined'
           AND event.occurred_at >= %(start)s
           AND event.occurred_at < %(end)s
    )
    -- A meeting's identity is its iCalUID, not its row: the same meeting sits
    -- on the organiser's calendar and on every attendee's, and the ledger keys
    -- each copy by `calendar_id:event_id`, so one meeting arrived as a dozen.
    -- iCalUID is the same value across all the copies. Null for every other
    -- source, which falls back to the external id.
    SELECT DISTINCT ON (
               matched.person_id,
               event.source,
               coalesce(ledger.raw_payload->>'iCalUID', event.external_id)
           )
           matched.person_id,
           event.occurred_at,
           event.source,
           event.event_type,
           event.container_id,
           event.thread_id,
           event.permalink,
           event.actor_external_id,
           event.payload,
           ledger.entity_type,
           ledger.raw_payload,
           -- A Notion block knows its page id but not its page title, and a
           -- line that reads "3b16cbdf..." names nothing. The parent page's
           -- own ledger row carries the title, and ledger_records_entity_idx
           -- covers this lookup.
           parent.raw_payload AS parent_raw,
           parent.relations AS parent_relations
      FROM matched
      JOIN timeline_events event
        ON event.event_id = matched.event_id
      LEFT JOIN ledger_records ledger
        ON ledger.ledger_id = event.event_id
      LEFT JOIN LATERAL (
          SELECT origin.raw_payload, origin.relations
            FROM ledger_records origin
           WHERE (origin.source = 'notion'
                  AND origin.entity_type = 'page'
                  AND origin.source_entity_id = ledger.relations->>'page_id')
              -- A reply's audience starts with whoever it is replying to, and
              -- that name is only on the parent message.
              OR (origin.source = 'slack'
                  AND origin.entity_type = 'message'
                  AND origin.source_entity_id = ledger.relations->>'thread_id')
           LIMIT 1
      ) AS parent ON ledger.source IN ('notion', 'slack')
     -- One row per real message, not per observation of it. The same Slack
     -- message arrives twice -- once from the Web API and once from the search
     -- supplement -- with different content hashes and therefore different
     -- ledger ids, so the timeline holds two rows for one thing a person said.
     -- The ledger is right to keep both: it records observations. A day's
     -- reading is not, so the official capture wins and the supplement is the
     -- fallback, which is the same priority order the head store already uses.
     ORDER BY matched.person_id, event.source,
              coalesce(ledger.raw_payload->>'iCalUID', event.external_id),
              CASE WHEN ledger.capture_profile LIKE '%%search%%' THEN 1 ELSE 0 END,
              event.occurred_at
"""

# Which identity kinds an actor handle can match, by the `actor_kind` the
# projection recorded. A GitHub login must not match a Slack identity that
# happens to be the same string.
_KIND_BY_ACTOR_KIND = {
    "github_login": "github",
    "slurm_user": "slurm",
    "git_email": "email_official",
    "notion_user": "notion",
    # A calendar organiser is an email address, and the roster keeps work
    # addresses under email_official -- the same space a commit's git email
    # lands in.
    "calendar_email": "email_official",
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

            # Slack writes mentions as <@U07EKRU6F7H>, which is unreadable. One
            # lookup for the whole day turns them into names, so a line can say
            # who it was addressed to.
            cursor.execute(
                "SELECT identity.value, person.name FROM org_identity identity "
                "JOIN org_person person ON person.person_id = identity.person_id "
                "WHERE identity.kind = 'slack'"
            )
            names = {str(value): str(name) for value, name in cursor.fetchall()}

            # Slack's own display names, for the people on the other end of a
            # DM who are not on the roster at all.
            cursor.execute(
                "SELECT DISTINCT ON (source_entity_id) source_entity_id, "
                "denormalized_label_snapshot FROM ledger_records "
                "WHERE source = 'slack' AND entity_type = 'user' "
                "ORDER BY source_entity_id, collected_at DESC"
            )
            for value, snapshot in cursor.fetchall():
                label = snapshot if isinstance(snapshot, dict) else {}
                shown = label.get("real_name") or label.get("name")
                names.setdefault(str(value), str(shown)) if shown else None

            cursor.execute(_PLACES_SQL)
            places = {
                str(channel): place
                for channel, raw, relations in cursor.fetchall()
                if (place := slack_place(raw, relations, names))
            }

            cursor.execute(_EVENTS_SQL, {"start": start, "end": end, "kinds": list(_ALL_KINDS)})
            # DISTINCT ON forces its own ordering, so the person-major,
            # time-ascending order the writer depends on is restored here.
            rows = sorted(cursor.fetchall(), key=lambda row: (str(row[0]), row[1]))

            # Messages fetched through Slack search carry no permalink, so the
            # workspace prefix is borrowed from one that does rather than
            # hardcoded. No prefix means no invented links.
            slack_prefix = _slack_prefix(rows)

            current: str | None = None
            events: list[dict] = []
            for row in rows:
                person_id = row[0]
                if current is not None and person_id != current:
                    _write(cursor, current, day, collapse(events), result, Jsonb)
                    events = []
                current = person_id
                events.append(
                    _event(row, names=names, slack_prefix=slack_prefix, places=places)
                )
            if current is not None:
                _write(cursor, current, day, collapse(events), result, Jsonb)

            if dry_run:
                connection.rollback()
            else:
                connection.commit()
    return result


def _event(
    row,
    *,
    names: dict[str, str] | None = None,
    slack_prefix: str | None = None,
    places: dict[str, str] | None = None,
) -> dict[str, Any]:
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
        entity_type,
        raw_payload,
        parent_raw,
        parent_relations,
    ) = row
    labels = (payload or {}).get("labels") or {}
    return {
        "at": occurred_at.astimezone(KST).isoformat(),
        "time": occurred_at.astimezone(KST).strftime("%H:%M"),
        "source": source,
        "event_type": event_type,
        "container": container_id,
        "thread": thread_id,
        "permalink": permalink
        or (_slack_permalink(raw_payload, container_id, slack_prefix) if source == "slack" else None),
        # Links the event carries of its own -- a meeting's Gemini notes, its
        # video room. Separate from `permalink`, which is the event itself.
        "links": links(source, raw_payload),
        # Computed here because the timeline row keeps no copy of the payload,
        # and the summary must not re-read the ledger to add up a day.
        "gpu_hours": round(_gpu_hours(raw_payload), 3) if source == "slurm" else None,
        # Who it was said to, and whether the place it was said in is closed.
        "to": audience(source, raw_payload, container_id, parent_relations, names, places),
        "private": bool(
            (places or {}).get(str(container_id), {}).get("private")
        ) if source == "slack" else False,
        "actor": actor_external_id,
        # None, not "". An absent title is a fact about the source, and an
        # empty string reads as a title that happens to be blank.
        "title": _title(labels if isinstance(labels, dict) else {}),
        "entity_type": entity_type,
        "excerpt": excerpt(entity_type, raw_payload, names=names),
        # Where a person would go looking for this, in that source's own terms:
        # a Slack channel, a Notion document, a calendar, a repository. The raw
        # container id ("C07ABCDEF") stays in `container` for machines.
        "where": _where(
            source, entity_type, labels, raw_payload, container_id, parent_raw, places
        ),
        # The one extra fact that source needs and the others do not -- a
        # meeting's time span, a reply's threadedness, a review's verdict.
        "detail": _detail(source, entity_type, raw_payload, thread_id),
        # What counts as "the same thing happening again". Normally the same
        # words in the same place; for a Notion block it is the document
        # itself, because editing a document paragraph by paragraph is one
        # piece of news, not thirty.
        # A recurring meeting is re-collected every day, and each capture is its
        # own ledger row, so folding a meeting by its words leaves one meeting
        # showing up several times. A meeting is the same meeting when it has
        # the same event id.
        "fold_by": (
            "document"
            if entity_type == "block"
            else "thread"
            if source == "google_calendar"
            else None
        ),
    }


_ELAPSED = re.compile(r"^(?:(\d+)-)?(\d+):(\d+):(\d+)(?:\.\d+)?$")


def _gpu_hours(raw: Any) -> float:
    """GPU-hours for one Slurm job, from what sacct recorded.

    AllocTRES names the GPUs (`gres/gpu=4`) and Elapsed the wall time; neither
    alone is the cost. A job missing either contributes nothing rather than a
    guessed number.
    """
    raw = raw if isinstance(raw, dict) else {}
    tres = raw.get("AllocTRES")
    elapsed = raw.get("Elapsed")
    if not isinstance(tres, str) or not isinstance(elapsed, str):
        return 0.0
    gpus = 0
    for part in tres.split(","):
        key, _, value = part.partition("=")
        if key.strip() in {"gres/gpu", "gpu"}:
            try:
                gpus = int(value)
            except ValueError:
                return 0.0
            break
    match = _ELAPSED.match(elapsed.strip())
    if not gpus or not match:
        return 0.0
    days, hours, minutes, seconds = (int(value or 0) for value in match.groups())
    return gpus * (days * 24 + hours + minutes / 60 + seconds / 3600)


def _hours(events: list[dict]) -> float:
    """Meeting hours, counting each meeting once however often it was captured."""
    spans: dict[str, float] = {}
    for event in events:
        detail = event.get("detail") or ""
        head = detail.split(" · ")[0]
        if "–" not in head:
            continue
        start, _, end = head.partition("–")
        try:
            begin = int(start[:2]) * 60 + int(start[3:5])
            finish = int(end[:2]) * 60 + int(end[3:5])
        except ValueError:
            continue
        if finish > begin:
            spans[str(event.get("thread"))] = (finish - begin) / 60
    return sum(spans.values())


def summarize(events: list[dict]) -> dict[str, Any]:
    """The day in one line, and then in a handful of groups.

    Pure counting -- no model, no selection of what mattered. The full timeline
    is still the record; this is the part a person reads first, because a day
    with 7,900 Slurm jobs in it cannot be read as a list.
    """
    by_source: dict[str, list[dict]] = {}
    for event in events:
        by_source.setdefault(str(event.get("source")), []).append(event)

    headline: list[str] = []
    groups: list[dict[str, Any]] = []

    meetings = by_source.get("google_calendar") or []
    if meetings:
        distinct = {str(event.get("thread")) for event in meetings}
        hours = _hours(meetings)
        label = f"회의 {len(distinct)}건"
        if hours:
            label += f" ({hours:.1f}h)"
        headline.append(label)

    messages = by_source.get("slack") or []
    if messages:
        places = {str(event.get("where")) for event in messages if event.get("where")}
        headline.append(f"슬랙 {len(messages)}건 · {len(places)}곳")

    notion = by_source.get("notion") or []
    if notion:
        documents = {str(event.get("where")) for event in notion if event.get("where")}
        headline.append(f"노션 문서 {len(documents)}개")

    github = by_source.get("github") or []
    if github:
        kinds: dict[str, int] = {}
        for event in github:
            kinds[str(event.get("event_type"))] = kinds.get(str(event.get("event_type")), 0) + 1
        shown = {
            "github_pull_request": "PR",
            "github_commit": "커밋",
            "github_review": "리뷰",
        }
        parts = [f"{name} {kinds[key]}" for key, name in shown.items() if kinds.get(key)]
        headline.append("깃헙 " + " · ".join(parts) if parts else f"깃헙 {len(github)}건")

    jobs = by_source.get("slurm") or []
    if jobs:
        gpu_hours = sum(float(event.get("gpu_hours") or 0) for event in jobs)
        label = f"잡 {len(jobs)}건"
        if gpu_hours:
            label += f" · {gpu_hours:,.0f} GPU-h"
        headline.append(label)

    # Nothing is folded away here yet. HK, 2026-09-16: 지금 에그리게이션보다
    # 나열로 보는건, 데이터 누락을 확인하기 위함이야 -- while the data is still
    # being verified, a view that hides rows hides exactly what he is looking
    # for. The headline is a checksum over the full list, not a replacement for
    # it: "회의 0건" on a day full of meetings is the fastest way to see a gap.
    return {"headline": headline, "groups": groups}


def _counts(events: list[dict]) -> dict[str, Any]:
    by_source: dict[str, int] = {}
    by_event_type: dict[str, int] = {}
    for event in events:
        by_source[event["source"]] = by_source.get(event["source"], 0) + 1
        by_event_type[event["event_type"]] = by_event_type.get(event["event_type"], 0) + 1
    return {
        "summary": summarize(events),
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


def missing_days(database_url: str, *, days: int, today: date | None = None) -> list[date]:
    """KST days in the last `days` that no digest covers, oldest first.

    A day is covered when any person has a digest row for it. That is a
    coarse test and the right one: the builder writes every person with
    activity in a single pass, so a day with one row is a day that ran.
    """
    import psycopg

    end = (today or _kst_today()) - timedelta(days=1)
    start = end - timedelta(days=max(0, days - 1))
    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT DISTINCT day FROM person_day_digest WHERE day BETWEEN %s AND %s",
                (start, end),
            )
            covered = {row[0] for row in cursor.fetchall()}
    out = []
    day = start
    while day <= end:
        if day not in covered:
            out.append(day)
        day += timedelta(days=1)
    return out


def catch_up(
    database_url: str, *, days: int = 7, dry_run: bool = False, today: date | None = None
) -> dict[str, Any]:
    """Build any of the last `days` KST days that has no digest at all.

    This is what makes a backfill something the batch does rather than
    something a person is handed a command for. It is bounded on purpose: a
    window, not the whole history, so a batch that has been off for a month
    catches up over several nights instead of trying to rebuild a year in one
    run and timing out where nobody sees it.

    Idempotent. A day already built is not rebuilt -- rebuilding one is
    `--date`, which is the deliberate act.
    """
    gaps = missing_days(database_url, days=days, today=today)
    built = [build_day(database_url, day, dry_run=dry_run).as_dict() for day in gaps]
    return {
        "window_days": days,
        "missing": [day.isoformat() for day in gaps],
        "built": built,
        "dry_run": dry_run,
    }


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


def resolve_people(database_url: str, names: list[str]) -> dict[str, Any]:
    """Match each requested name to a person, by name, nickname, or identity.

    Case-insensitive substring, across the roster name, the latest nickname,
    and every identity value (email, github, slack ...) -- so "gerald", "샘",
    "장주철" land by name/nickname, and "hyungkyu" lands because it sits inside
    hyungkyu.ryu@rlwrld.ai. The Korean roster keeps names in Hangul, so a
    romanised handle would never match a name column alone; the identity join
    is what makes the English handle work. Ambiguity is reported rather than
    guessed: a term that hits two people is returned as a conflict, because a
    report that silently picked one of two matches is worse than one that asks.
    """
    import psycopg

    resolved: dict[str, str] = {}
    unresolved: list[str] = []
    ambiguous: dict[str, list[str]] = {}
    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            for name in names:
                needle = f"%{name.strip().lower()}%"
                cursor.execute(
                    """
                    SELECT DISTINCT p.person_id, p.name
                      FROM org_person p
                      LEFT JOIN org_person_state s ON s.person_id = p.person_id
                      LEFT JOIN org_identity i ON i.person_id = p.person_id
                     WHERE lower(p.name) LIKE %(n)s
                        OR lower(coalesce(s.nickname, '')) LIKE %(n)s
                        OR lower(coalesce(i.value, '')) LIKE %(n)s
                    """,
                    {"n": needle},
                )
                hits = cursor.fetchall()
                if not hits:
                    unresolved.append(name)
                elif len(hits) > 1:
                    ambiguous[name] = [f"{row[1]} ({row[0]})" for row in hits]
                else:
                    resolved[name] = hits[0][0]
    return {"resolved": resolved, "unresolved": unresolved, "ambiguous": ambiguous}


def build_report_sections(
    database_url: str, person_ids: list[str], days: list[date]
) -> list[dict[str, Any]]:
    """Every (person, day) as a digest dict, or a not-built marker.

    Person-major, day-ascending: a reader scans one person down their days,
    then the next person. A day with no digest row is marked built=False so
    the report can say "not built" rather than showing nothing.
    """
    sections: list[dict[str, Any]] = []
    for person_id in person_ids:
        for day in days:
            found = read_digest(database_url, person_id, day)
            if found is None:
                sections.append(
                    {"person_id": person_id, "day": day.isoformat(), "built": False}
                )
            else:
                sections.append({"built": True, **found})
    return sections
