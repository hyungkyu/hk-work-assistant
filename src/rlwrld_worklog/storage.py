from __future__ import annotations

from collections.abc import Iterable

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


def write_events(database_url: str, events: Iterable[TimelineEvent]) -> int:
    import psycopg
    from psycopg.types.json import Jsonb

    count = 0
    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            for event in events:
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
                count += 1
    return count

