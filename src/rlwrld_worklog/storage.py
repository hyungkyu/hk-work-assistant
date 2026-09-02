from __future__ import annotations

from collections.abc import Iterable
from datetime import timezone

from .models import Classification, TimelineEvent


EVENT_UPSERT = """
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
    ingested_at = EXCLUDED.ingested_at,
    container_id = EXCLUDED.container_id,
    thread_id = EXCLUDED.thread_id,
    permalink = EXCLUDED.permalink,
    classifications = EXCLUDED.classifications,
    payload = EXCLUDED.payload
"""


def write_events(database_url: str, events: Iterable[TimelineEvent], *, origin: str = "live") -> int:
    import psycopg
    from psycopg.types.json import Jsonb

    if origin not in {"legacy", "live"}:
        raise ValueError("origin must be legacy or live")
    origin_priority = 100 if origin == "live" else 10
    count = 0
    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            for event in events:
                object_type = {
                    "message_deleted": "message",
                    "calendar_event_cancelled": "calendar_event",
                }.get(event.event_type, event.event_type)
                cursor.execute(
                    EVENT_UPSERT,
                    {
                        "event_id": event.event_id,
                        "source": event.source.value,
                        "event_type": event.event_type,
                        "external_id": event.external_id,
                        "actor_external_id": event.actor_id,
                        "occurred_at": event.occurred_at,
                        "updated_at": event.updated_at,
                        "ingested_at": event.ingested_at,
                        "container_id": event.container_id,
                        "thread_id": event.thread_id,
                        "permalink": event.permalink,
                        "classifications": [item.value for item in event.classification],
                        "payload": Jsonb(event.payload),
                    },
                )
                cursor.execute("DELETE FROM mentions WHERE event_id = %s", (event.event_id,))
                for mention in event.mentions:
                    cursor.execute(
                        """
                        INSERT INTO mentions (
                            event_id, target_external_id, kind, direction, priority
                        ) VALUES (%s, %s, %s, %s, %s)
                        """,
                        (
                            event.event_id,
                            mention.target_id,
                            mention.kind.value,
                            mention.direction,
                            mention.priority,
                        ),
                    )
                cursor.execute("DELETE FROM action_candidates WHERE event_id = %s", (event.event_id,))
                for classification in event.classification:
                    if classification is Classification.UNCLASSIFIED:
                        continue
                    cursor.execute(
                        """
                        INSERT INTO action_candidates (
                            event_id, kind, rule_id, confidence
                        ) VALUES (%s, %s, %s, %s)
                        """,
                        (event.event_id, classification.value, "deterministic-v1", 1.0),
                    )
                cursor.execute(
                    """
                    INSERT INTO source_object_observations (
                        id, source, object_type, external_id, origin, origin_priority,
                        observed_at, remote_updated_at, is_deleted, payload, timeline_event_id
                    ) VALUES (
                        %(id)s, %(source)s, %(object_type)s, %(external_id)s, %(origin)s,
                        %(origin_priority)s, %(observed_at)s, %(remote_updated_at)s,
                        %(is_deleted)s, %(payload)s, %(timeline_event_id)s
                    )
                    ON CONFLICT (id) DO UPDATE SET
                        origin = CASE
                            WHEN EXCLUDED.origin_priority >= source_object_observations.origin_priority
                            THEN EXCLUDED.origin ELSE source_object_observations.origin END,
                        origin_priority = GREATEST(
                            EXCLUDED.origin_priority, source_object_observations.origin_priority
                        ),
                        observed_at = EXCLUDED.observed_at,
                        remote_updated_at = COALESCE(
                            EXCLUDED.remote_updated_at, source_object_observations.remote_updated_at
                        ),
                        is_deleted = EXCLUDED.is_deleted,
                        payload = EXCLUDED.payload,
                        timeline_event_id = EXCLUDED.timeline_event_id
                    """,
                    {
                        "id": event.event_id,
                        "source": event.source.value,
                        "object_type": object_type,
                        "external_id": event.external_id,
                        "origin": origin,
                        "origin_priority": origin_priority,
                        "observed_at": event.ingested_at.astimezone(timezone.utc),
                        "remote_updated_at": event.updated_at,
                        "is_deleted": bool(event.payload.get("deleted") or event.payload.get("archived"))
                        or event.event_type.endswith("cancelled"),
                        "payload": Jsonb(event.payload),
                        "timeline_event_id": event.event_id,
                    },
                )
                cursor.execute(
                    """
                    INSERT INTO source_object_heads (
                        source, object_type, external_id, observation_id, origin,
                        origin_priority, remote_updated_at
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
                    """,
                    (
                        event.source.value,
                        object_type,
                        event.external_id,
                        event.event_id,
                        origin,
                        origin_priority,
                        event.updated_at,
                    ),
                )
                count += 1
    return count
