from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Iterable, Iterator

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
    workspace_id = record.get("team_id") or record.get("workspace_id")
    external_id = f"{record['channel']}:{record['ts']}"
    if workspace_id:
        external_id = f"{workspace_id}:{external_id}"
    return TimelineEvent.create(
        source=Source.SLACK,
        event_type="message_deleted" if record.get("deleted") else "message",
        external_id=external_id,
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
    start_value = record.get("start") or record.get("originalStartTime") or {}
    if isinstance(start_value, dict):
        start = start_value.get("dateTime") or start_value.get("date")
    else:
        start = start_value
    updated = record.get("updated")
    status = record.get("status", "confirmed")
    start = start or updated or record.get("created") or datetime.now(timezone.utc).isoformat()
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
        event_type="calendar_event_cancelled" if status == "cancelled" else "calendar_event",
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


def _plain_text(value: Any) -> str:
    parts: list[str] = []

    def visit(item: Any) -> None:
        if isinstance(item, dict):
            plain_text = item.get("plain_text")
            if isinstance(plain_text, str):
                parts.append(plain_text)
            else:
                for nested in item.values():
                    visit(nested)
        elif isinstance(item, list):
            for nested in item:
                visit(nested)

    visit(value)
    return "\n".join(part for part in parts if part)


def _iter_notion_mentions(value: Any) -> Iterator[dict[str, Any]]:
    """Every rich-text entry that is a mention, wherever in the object it sits.

    Notion hangs `rich_text` off a key named after the block's own type
    (`paragraph`, `heading_2`, `to_do`, a database property, …) and a comment
    carries its own array, so there is no single path to read. Matching on the
    shape rather than the path means a block type nobody here has seen yet
    still yields the people it named.
    """
    if isinstance(value, dict):
        mention = value.get("mention")
        if value.get("type") == "mention" and isinstance(mention, dict):
            # A mention object holds only the thing mentioned; nothing nested
            # inside it is itself rich text.
            yield mention
            return
        for nested in value.values():
            yield from _iter_notion_mentions(nested)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_notion_mentions(item)


def notion_mention_user_ids(value: Any) -> list[str]:
    """Ids of the users mentioned anywhere in `value`, in first-seen order.

    Page, database, date and link_preview mentions are deliberately left out.
    A `Mention` carries a `direction` -- to_self, from_self, other -- which is a
    statement about people, and one document naming another has no direction to
    state; recording it here would answer "who mentioned whom" with something
    that is not a who. The document-to-document link is not lost by the choice:
    the mention object stays verbatim in the raw block JSON, which the ledger
    keeps as `raw_payload`.

    One entry per person, not one per occurrence. The question this answers is
    who a page pulled in, and a name repeated across forty blocks is still one
    person.
    """
    ids: list[str] = []
    for mention in _iter_notion_mentions(value):
        if mention.get("type") != "user":
            continue
        user = mention.get("user")
        user_id = user.get("id") if isinstance(user, dict) else None
        if isinstance(user_id, str) and user_id and user_id not in ids:
            ids.append(user_id)
    return ids


def extract_notion_mentions(
    value: Any, *, actor_id: str | None, self_user_id: str
) -> list[Mention]:
    """Notion mentions in the same shape Slack's arrive in.

    Direction needs a self to be relative to. Notion user ids are workspace
    uuids from a different namespace than Slack's, so a caller that has not
    said which one is ours gets `other` throughout rather than a guess.
    """
    mentions: list[Mention] = []
    for target_id in notion_mention_user_ids(value):
        if self_user_id and target_id == self_user_id:
            direction = "to_self"
        elif self_user_id and actor_id == self_user_id:
            direction = "from_self"
        else:
            direction = "other"
        mentions.append(Mention(target_id, MentionKind.DIRECT, direction, 100))
    return mentions


def normalize_notion(
    page: dict[str, Any],
    *,
    blocks: list[dict[str, Any]] | None = None,
    comments: list[dict[str, Any]] | None = None,
    legacy_text: str | None = None,
    self_user_id: str = "",
) -> TimelineEvent:
    created = page.get("created_time") or page.get("last_edited_time")
    if not created:
        raise ValueError("Notion page is missing created_time and last_edited_time")
    updated = page.get("last_edited_time") or created
    page_id = str(page["id"])
    content = legacy_text if legacy_text is not None else _plain_text(blocks or [])
    title = page.get("title") or _plain_text(page.get("properties", {}))[:500]
    actor_id = (page.get("last_edited_by") or {}).get("id")
    return TimelineEvent.create(
        source=Source.NOTION,
        event_type="notion_page",
        external_id=page_id,
        actor_id=actor_id,
        occurred_at=_iso_datetime(created),
        updated_at=_iso_datetime(updated),
        container_id=str((page.get("parent") or {}).get("page_id") or (page.get("parent") or {}).get("data_source_id") or ""),
        thread_id=page_id,
        permalink=page.get("url"),
        classification=classify_text(f"{title}\n{content}"),
        # The blocks and comments are already in hand; the mentions inside them
        # cost no request that has not already been paid for.
        mentions=extract_notion_mentions(
            [blocks or [], comments or []], actor_id=actor_id, self_user_id=self_user_id
        ),
        payload={
            "title": title,
            "properties": page.get("properties", {}),
            "content_text": content,
            "comments": comments or [],
            "archived": bool(page.get("archived") or page.get("in_trash")),
        },
        version_key=str(updated),
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
    if source is Source.NOTION:
        return [normalize_notion(record) for record in records]
    raise ValueError(f"Unsupported source: {source}")
