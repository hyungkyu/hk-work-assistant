from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from uuid import NAMESPACE_URL, uuid5


class Source(StrEnum):
    SLACK = "slack"
    GOOGLE_CALENDAR = "google_calendar"
    GITHUB = "github"


class Classification(StrEnum):
    REQUEST = "request"
    PROMISE = "promise"
    DECISION = "decision"
    QUESTION = "question"
    RESPONSE = "response"
    SCHEDULE_CHANGE = "schedule_change"
    UNCLASSIFIED = "unclassified"


class MentionKind(StrEnum):
    DIRECT = "direct"
    USER_GROUP = "user_group"
    CHANNEL = "channel"
    HERE = "here"
    EVERYONE = "everyone"


@dataclass(frozen=True, slots=True)
class Mention:
    target_id: str
    kind: MentionKind
    direction: str
    priority: int


@dataclass(slots=True)
class TimelineEvent:
    event_id: str
    source: Source
    event_type: str
    external_id: str
    actor_id: str | None
    occurred_at: datetime
    updated_at: datetime | None
    ingested_at: datetime
    container_id: str | None
    thread_id: str | None
    permalink: str | None
    classification: list[Classification] = field(default_factory=list)
    mentions: list[Mention] = field(default_factory=list)
    payload: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        *,
        source: Source,
        event_type: str,
        external_id: str,
        occurred_at: datetime,
        actor_id: str | None = None,
        updated_at: datetime | None = None,
        container_id: str | None = None,
        thread_id: str | None = None,
        permalink: str | None = None,
        classification: list[Classification] | None = None,
        mentions: list[Mention] | None = None,
        payload: dict[str, Any] | None = None,
        version_key: str = "current",
    ) -> "TimelineEvent":
        identity = f"{source}:{event_type}:{external_id}:{version_key}"
        return cls(
            event_id=str(uuid5(NAMESPACE_URL, identity)),
            source=source,
            event_type=event_type,
            external_id=external_id,
            actor_id=actor_id,
            occurred_at=occurred_at,
            updated_at=updated_at,
            ingested_at=datetime.now(timezone.utc),
            container_id=container_id,
            thread_id=thread_id,
            permalink=permalink,
            classification=classification or [Classification.UNCLASSIFIED],
            mentions=mentions or [],
            payload=payload or {},
        )

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        for key in ("occurred_at", "updated_at", "ingested_at"):
            if value[key] is not None:
                value[key] = value[key].isoformat()
        value["source"] = self.source.value
        value["classification"] = [item.value for item in self.classification]
        for index, mention in enumerate(self.mentions):
            value["mentions"][index]["kind"] = mention.kind.value
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "TimelineEvent":
        return cls(
            event_id=value["event_id"],
            source=Source(value["source"]),
            event_type=value["event_type"],
            external_id=value["external_id"],
            actor_id=value.get("actor_id"),
            occurred_at=datetime.fromisoformat(value["occurred_at"]),
            updated_at=datetime.fromisoformat(value["updated_at"]) if value.get("updated_at") else None,
            ingested_at=datetime.fromisoformat(value["ingested_at"]),
            container_id=value.get("container_id"),
            thread_id=value.get("thread_id"),
            permalink=value.get("permalink"),
            classification=[Classification(item) for item in value.get("classification", [])],
            mentions=[
                Mention(
                    target_id=item["target_id"],
                    kind=MentionKind(item["kind"]),
                    direction=item["direction"],
                    priority=item["priority"],
                )
                for item in value.get("mentions", [])
            ],
            payload=value.get("payload", {}),
        )
