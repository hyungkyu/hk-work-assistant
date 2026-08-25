from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Iterable

from .models import Classification, Mention, MentionKind, Source, TimelineEvent


USER_MENTION_RE = re.compile(r"<@([A-Z0-9]+)>")
GROUP_MENTION_RE = re.compile(r"<!subteam\^([A-Z0-9]+)(?:\|[^>]+)?>")
SPECIAL_MENTION_RE = re.compile(r"<!(channel|here|everyone)>")

REQUEST_HINTS = ("해주세요", "부탁", "확인해", "검토해", "please", "could you", "can you")
PROMISE_HINTS = ("하겠습니다", "할게요", "해볼게", "i will", "i'll")
DECISION_HINTS = ("결정", "확정", "하기로", "agreed", "decided")


def _iso_datetime(value: str) -> datetime:
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _slack_datetime(value: str) -> datetime:
    return datetime.fromtimestamp(float(value), tz=timezone.utc)


def classify_text(text: str, *, is_response: bool = False) -> list[Classification]:
    lowered = text.casefold()
    found: list[Classification] = []
    if "?" in text:
        found.append(Classification.QUESTION)
    if any(hint in lowered for hint in REQUEST_HINTS):
        found.append(Classification.REQUEST)
    if any(hint in lowered for hint in PROMISE_HINTS):
        found.append(Classification.PROMISE)
    if any(hint in lowered for hint in DECISION_HINTS):
        found.append(Classification.DECISION)
    if is_response:
        found.append(Classification.RESPONSE)
    return list(dict.fromkeys(found)) or [Classification.UNCLASSIFIED]


def extract_slack_mentions(text: str, *, actor_id: str | None, self_user_id: str) -> list[Mention]:
    mentions: list[Mention] = []
    for target_id in USER_MENTION_RE.findall(text):
        if target_id == self_user_id:
            direction = "to_self"
        elif actor_id == self_user_id:
            direction = "from_self"
        else:
            direction = "other"
        mentions.append(Mention(target_id, MentionKind.DIRECT, direction, 100))
    for target_id in GROUP_MENTION_RE.findall(text):
        mentions.append(Mention(target_id, MentionKind.USER_GROUP, "group", 60))
    for kind_value in SPECIAL_MENTION_RE.findall(text):
        kind = MentionKind(kind_value)
        mentions.append(Mention(kind_value, kind, "broadcast", 20))
    return mentions


def normalize_slack(record: dict[str, Any], *, self_user_id: str) -> TimelineEvent:
    text = record.get("text", "")
    thread_id = record.get("thread_ts") or record.get("ts")
    is_response = record.get("thread_ts") is not None
    version_key = record.get("edited", {}).get("ts") or record.get("version", "current")
    file_metadata = [
        {
            "id": item.get("id"),
            "name": item.get("name"),
            "mimetype": item.get("mimetype"),
            "size": item.get("size"),
            "permalink": item.get("permalink"),
        }
        for item in record.get("files", [])
    ]
    actor_id = record.get("user") or record.get("bot_id")
    return TimelineEvent.create(
        source=Source.SLACK,
        event_type="message_deleted" if record.get("deleted") else "message",
        external_id=f"{record['channel']}:{record['ts']}",
        actor_id=actor_id,
        occurred_at=_slack_datetime(record["ts"]),
        updated_at=_slack_datetime(record["edited"]["ts"]) if record.get("edited") else None,
        container_id=record["channel"],
        thread_id=thread_id,
        permalink=record.get("permalink"),
        classification=classify_text(text, is_response=is_response),
        mentions=extract_slack_mentions(text, actor_id=actor_id, self_user_id=self_user_id),
        payload={
            "text": text,
            "deleted": bool(record.get("deleted")),
            "reactions": record.get("reactions", []),
            "files": file_metadata,
        },
        version_key=str(version_key),
    )


def normalize_calendar(record: dict[str, Any]) -> TimelineEvent:
    private = record.get("visibility") == "private"
    start = record.get("start", {}).get("dateTime") or record.get("start", {}).get("date")
    updated = record.get("updated")
    status = record.get("status", "confirmed")
    summary = "Busy" if private else record.get("summary", "(untitled)")
    payload = {
        "summary": summary,
        "start": record.get("start"),
        "end": record.get("end"),
        "status": status,
        "visibility": record.get("visibility", "default"),
    }
    if not private:
        payload.update(
            {
                "description": record.get("description"),
                "attendees": record.get("attendees", []),
                "conferenceData": record.get("conferenceData"),
            }
        )
    classification = (
        [Classification.SCHEDULE_CHANGE]
        if status == "cancelled" or updated != record.get("created")
        else [Classification.UNCLASSIFIED]
    )
    return TimelineEvent.create(
        source=Source.GOOGLE_CALENDAR,
        event_type="calendar_event",
        external_id=f"{record['calendar_id']}:{record['id']}",
        actor_id=record.get("creator", {}).get("email"),
        occurred_at=_iso_datetime(start),
        updated_at=_iso_datetime(updated) if updated else None,
        container_id=record["calendar_id"],
        thread_id=record.get("recurringEventId") or record["id"],
        permalink=record.get("htmlLink"),
        classification=classification,
        payload=payload,
        version_key=updated or status,
    )


def normalize_github(record: dict[str, Any]) -> TimelineEvent:
    action = record.get("action", "unknown")
    subject = record.get("subject", {})
    text = subject.get("body") or subject.get("title") or ""
    return TimelineEvent.create(
        source=Source.GITHUB,
        event_type=record["event_type"],
        external_id=str(record["id"]),
        actor_id=record.get("actor", {}).get("login"),
        occurred_at=_iso_datetime(record["created_at"]),
        updated_at=_iso_datetime(record["updated_at"]) if record.get("updated_at") else None,
        container_id=record.get("repository", {}).get("full_name"),
        thread_id=str(subject.get("number") or subject.get("id") or record["id"]),
        permalink=subject.get("html_url"),
        classification=classify_text(text, is_response="comment" in record["event_type"]),
        payload={
            "action": action,
            "subject": subject,
            "actor_type": record.get("actor", {}).get("type", "User"),
            "automated": record.get("actor", {}).get("type") == "Bot",
        },
        version_key=record.get("updated_at") or action,
    )


def normalize_records(
    source: Source, records: Iterable[dict[str, Any]], *, self_user_id: str = ""
) -> list[TimelineEvent]:
    if source is Source.SLACK:
        return [normalize_slack(record, self_user_id=self_user_id) for record in records]
    if source is Source.GOOGLE_CALENDAR:
        return [normalize_calendar(record) for record in records]
    if source is Source.GITHUB:
        return [normalize_github(record) for record in records]
    raise ValueError(f"Unsupported source: {source}")
