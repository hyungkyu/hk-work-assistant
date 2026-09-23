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

            # `ledger_id` is a uuid column; this test predates that and passed
            # the Slack ts straight into it, so it has been failing on every
            # run with a real database since -- the only red in the suite, and
            # long enough that it started reading as background noise. The id
            # is derived from the ts so the rows stay recognisable.
            import uuid

            def ledger_id_of(entity_id: str) -> uuid.UUID:
                return uuid.uuid5(uuid.NAMESPACE_URL, f"slack-sweep-test:{entity_id}")

            def insert(
                entity_id, *, is_reply, parent_ts=None, channel="C1", entity_key=None
            ):
                cursor.execute(
                    """
                    INSERT INTO ledger_records (
                      ledger_id, schema_version, capture_profile, source, entity_type,
                      tenant_workspace_id, tenant_status, scope, source_entity_id,
                      source_updated_at_status, deleted_status, raw_payload,
                      content_hash, relations, source_file, source_file_sha256,
                      record_pointer, legacy_layout_version, converter_version,
                      observation_role, capture_completeness_status
                    ) VALUES (
                      %s, 'v1', 'p', 'slack', 'message', 'T', 'observed',
                      %s, %s, 'observed', 'observed', '{}'::jsonb, %s,
                      %s, 'f', 'h', 'p', 'v', 'v', 'historical_observation', 'recorded'
                    )
                    """,
                    (
                        ledger_id_of(entity_key or entity_id),
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
            # The same pair, written the way the converters actually write
            # ids: "{workspace}:{channel}:{ts}" for the row, bare ts for the
            # parent pointer. Before 2026-09-22 this query compared the two
            # spellings directly, so a present parent read as absent and the
            # nightly sweep re-fetched threads the ledger already held. Only a
            # fixture in the production shape can catch that.
            insert("T:C1:3.0", is_reply=False, entity_key="3.0")
            insert("T:C1:300.1", is_reply=True, parent_ts="3.0", entity_key="300.1")
            connection.commit()

    try:
        parents = orphan_thread_parents(url)
        assert parents == {"C1": {"1.0"}}, (
            "3.0 is present under a composite id and must not read as missing"
        )
    finally:
        # These rows have no source_created_at, which is fine for this query
        # and fatal for the projection suite that shares the database: leaving
        # them behind turned one red test into three. A test cleans up what it
        # inserted.
        with psycopg.connect(url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM ledger_records WHERE source_entity_id IN "
                    "('100.1', '2.0', '200.1', 'T:C1:3.0', 'T:C1:300.1')"
                )
            connection.commit()
