"""Finding the parents to sweep, from the ledger alone.

The query is the whole risk here: it decides which threads a one-time,
network-heavy recovery will fetch. So it is tested against a real throwaway
Postgres when one is offered, and the seed-shaping is tested without one.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from rlwrld_worklog.slack_sweep import summarize

REQUIRES_DATABASE = pytest.mark.skipif(
    not os.environ.get("WORKLOG_TEST_DATABASE_URL"),
    reason="set WORKLOG_TEST_DATABASE_URL to a throwaway database",
)


def test_summarize_counts_channels_and_parents() -> None:
    assert summarize({"C1": {"a", "b"}, "C2": {"c"}}) == {"channels": 2, "parents": 3}
    assert summarize({}) == {"channels": 0, "parents": 0}


@REQUIRES_DATABASE
def test_only_replies_whose_parent_is_absent_are_returned() -> None:
    import psycopg

    from rlwrld_worklog.ledger.load import apply_migrations
    from rlwrld_worklog.slack_sweep import orphan_thread_parents

    url = os.environ["WORKLOG_TEST_DATABASE_URL"]
    apply_migrations(
        database_url=url,
        migrations_dir=Path(__file__).resolve().parents[1] / "sql" / "migrations",
        dry_run=False,
    )
    with psycopg.connect(url) as connection:
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM ledger_records WHERE source = 'slack'")

            def insert(entity_id, *, is_reply, parent_ts=None, channel="C1"):
                cursor.execute(
                    """
                    INSERT INTO ledger_records (
                      ledger_id, capture_profile, source, entity_type,
                      tenant_workspace_id, tenant_status, scope, source_entity_id,
                      source_updated_at_status, deleted_status, raw_payload,
                      content_hash, relations, source_file, source_file_sha256,
                      record_pointer, legacy_layout_version, converter_version,
                      observation_role
                    ) VALUES (
                      %s, 'p', 'slack', 'message', 'T', 'observed',
                      %s, %s, 'observed', 'observed', '{}'::jsonb, %s,
                      %s, 'f', 'h', 'p', 'v', 'v', 'historical_observation'
                    )
                    """,
                    (
                        entity_id,
                        psycopg.types.json.Jsonb({"container": channel}),
                        entity_id,
                        entity_id,
                        psycopg.types.json.Jsonb(
                            {"is_thread_reply": is_reply, "parent_ts": parent_ts}
                        ),
                    ),
                )

            # An orphan: reply whose parent has no row.
            insert("100.1", is_reply=True, parent_ts="1.0")
            # Not an orphan: reply whose parent IS present.
            insert("2.0", is_reply=False)
            insert("200.1", is_reply=True, parent_ts="2.0")
            connection.commit()

    parents = orphan_thread_parents(url)
    assert parents == {"C1": {"1.0"}}
