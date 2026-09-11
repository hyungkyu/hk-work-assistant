"""The projection backfill, against a real database.

The case that made this module necessary: records already in the ledger,
whose batches the loader will never re-read because their sha256 is
unchanged. Without a backfill a change to what gets projected applies only
to data that has not arrived yet.
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rlwrld_worklog.ledger.project import project_timeline, timeline_status  # noqa: E402

REQUIRES_DATABASE = pytest.mark.skipif(
    not os.environ.get("WORKLOG_TEST_DATABASE_URL"),
    reason="set WORKLOG_TEST_DATABASE_URL to a throwaway database to run the projection round trip",
)

TENANT = "projection-round-trip-test"

RECORD_SQL = """
INSERT INTO ledger_records (
    ledger_id, schema_version, capture_profile, source, entity_type,
    tenant_workspace_id, tenant_status, source_entity_id, scope, relations,
    source_created_at, source_updated_at, source_updated_at_status,
    collected_at, deleted_status, raw_payload, content_hash,
    source_file, source_file_sha256, record_pointer, legacy_layout_version,
    converter_version, observation_role, capture_completeness_status,
    denormalized_label_snapshot
) VALUES (
    %(ledger_id)s, '1.0', %(profile)s, %(source)s, %(entity_type)s,
    %(tenant)s, 'active', %(entity)s, %(scope)s, %(relations)s,
    %(created)s, %(created)s, 'observed',
    %(created)s, 'unknown', %(payload)s, %(hash)s,
    'test.jsonl', 'sha', 'p:0', 'test', 'test', 'current_head', 'unknown',
    '{}'::jsonb
)
"""


@pytest.fixture()
def database():
    import psycopg

    from rlwrld_worklog.ledger.load import apply_migrations

    url = os.environ["WORKLOG_TEST_DATABASE_URL"]
    repository = Path(__file__).resolve().parents[1]
    with psycopg.connect(url) as connection:
        with connection.cursor() as cursor:
            cursor.execute((repository / "sql" / "schema.sql").read_text(encoding="utf-8"))
        connection.commit()
    apply_migrations(
        database_url=url, migrations_dir=repository / "sql" / "migrations", dry_run=False
    )
    _clean(url)
    yield url
    _clean(url)


def _clean(url: str) -> None:
    import psycopg

    with psycopg.connect(url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT ledger_id, source, entity_type, source_entity_id"
                "  FROM ledger_records WHERE tenant_workspace_id = %s",
                (TENANT,),
            )
            rows = cursor.fetchall()
            for ledger_id, source, entity_type, entity in rows:
                cursor.execute(
                    "DELETE FROM source_object_heads"
                    " WHERE source = %s AND object_type = %s AND external_id = %s",
                    (source, entity_type, entity),
                )
                cursor.execute(
                    "DELETE FROM source_object_observations WHERE id = %s", (ledger_id,)
                )
                cursor.execute("DELETE FROM search_documents WHERE ledger_id = %s", (ledger_id,))
                cursor.execute("DELETE FROM timeline_events WHERE event_id = %s", (ledger_id,))
            cursor.execute("DELETE FROM ledger_records WHERE tenant_workspace_id = %s", (TENANT,))
        connection.commit()


def _insert(url, *, source, entity_type, entity, payload=None, relations=None, scope=None,
            created="2026-09-10T04:00:00+00:00", profile="live-test/v1") -> str:
    import psycopg
    from psycopg.types.json import Jsonb

    ledger_id = str(uuid.uuid4())
    with psycopg.connect(url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                RECORD_SQL,
                {
                    "ledger_id": ledger_id,
                    "profile": profile,
                    "source": source,
                    "entity_type": entity_type,
                    "tenant": TENANT,
                    "entity": entity,
                    "scope": Jsonb(scope or {}),
                    "relations": Jsonb(relations or {}),
                    "created": created,
                    "payload": Jsonb(payload or {}),
                    "hash": ledger_id,
                },
            )
        connection.commit()
    return ledger_id


def _events(url):
    import psycopg

    with psycopg.connect(url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT e.event_type, e.actor_external_id, e.container_id, e.payload->>'actor_kind'"
                "  FROM timeline_events e JOIN ledger_records r ON r.ledger_id = e.event_id"
                " WHERE r.tenant_workspace_id = %s ORDER BY e.event_type",
                (TENANT,),
            )
            return cursor.fetchall()


@REQUIRES_DATABASE
def test_loaded_github_and_slurm_records_reach_the_timeline(database) -> None:
    """The 2026-09-11 backfill, in miniature."""
    _insert(database, source="github", entity_type="commit", entity="repo:abc",
            scope={"repository": "rlwrld-vla"},
            payload={"sha": "abc", "author": {"login": "mskim"},
                     "html_url": "https://github.com/x/commit/abc"})
    _insert(database, source="github", entity_type="pull_request", entity="repo:pull_request:7",
            scope={"repository": "rlwrld-vla"}, relations={"author": "storm"})
    _insert(database, source="slurm", entity_type="job", entity="ncloud:12345",
            scope={"cluster": "gpu-a100"}, relations={"user": "gerald"})

    result = project_timeline(database, dry_run=False)

    assert result.projected >= 3
    assert result.without_occurred_at == 0
    assert _events(database) == [
        ("github_commit", "mskim", "rlwrld-vla", "github_login"),
        ("github_pull_request", "storm", "rlwrld-vla", "github_login"),
        ("slurm_job", "gerald", "gpu-a100", "slurm_user"),
    ]


@REQUIRES_DATABASE
def test_a_projected_record_is_not_projected_twice(database) -> None:
    _insert(database, source="slurm", entity_type="job", entity="ncloud:1",
            relations={"user": "a"}, scope={"cluster": "c"})
    first = project_timeline(database, sources=("slurm",), dry_run=False)
    assert first.projected >= 1
    again = project_timeline(database, sources=("slurm",), dry_run=False)
    assert again.scanned == 0 and again.projected == 0


@REQUIRES_DATABASE
def test_reproject_re_derives_rows_that_already_have_an_event(database) -> None:
    """For when the projection changed rather than the data."""
    _insert(database, source="slurm", entity_type="job", entity="ncloud:2",
            relations={"user": "b"}, scope={"cluster": "c"})
    project_timeline(database, sources=("slurm",), dry_run=False)
    again = project_timeline(database, sources=("slurm",), reproject=True, dry_run=False)
    assert again.projected >= 1


@REQUIRES_DATABASE
def test_the_head_is_written_so_the_current_view_can_see_the_row(database) -> None:
    """A backfill that wrote only the event would be invisible on every screen.

    `current_timeline_events` joins through source_object_heads, so an event
    with no head exists in the table and nowhere a person looks.
    """
    import psycopg

    ledger_id = _insert(database, source="github", entity_type="issue",
                        entity="repo:issue:9", relations={"author": "hk"},
                        scope={"repository": "r"})
    project_timeline(database, sources=("github",), dry_run=False)
    with psycopg.connect(database) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM current_timeline_events WHERE event_id = %s", (ledger_id,)
            )
            assert cursor.fetchone()[0] == 1


@REQUIRES_DATABASE
def test_a_record_without_a_creation_time_is_reported_not_dropped(database) -> None:
    """An event with no time cannot sit on a timeline; silence would hide it."""
    _insert(database, source="slurm", entity_type="job", entity="ncloud:3",
            relations={"user": "c"}, scope={"cluster": "c"}, created=None)
    result = project_timeline(database, sources=("slurm",), dry_run=False)
    assert result.without_occurred_at == 1
    assert result.projected == 0
    assert any("no source_created_at" in error for error in result.errors)


@REQUIRES_DATABASE
def test_a_dry_run_counts_without_writing(database) -> None:
    _insert(database, source="slurm", entity_type="job", entity="ncloud:4",
            relations={"user": "d"}, scope={"cluster": "c"})
    result = project_timeline(database, sources=("slurm",), dry_run=True)
    assert result.projected >= 1
    assert _events(database) == []


@REQUIRES_DATABASE
def test_an_actor_the_projection_could_not_read_is_counted_per_source(database) -> None:
    """Zero is the only reassuring value, so it has to be reported at all."""
    _insert(database, source="slurm", entity_type="job", entity="ncloud:5",
            scope={"cluster": "c"})
    result = project_timeline(database, sources=("slurm",), dry_run=False)
    assert result.without_actor.get("slurm") == 1


@REQUIRES_DATABASE
def test_status_names_the_unprojected_records(database) -> None:
    _insert(database, source="github", entity_type="commit", entity="repo:z",
            scope={"repository": "r"}, relations={"author_email": "x@y.invalid"})
    status = timeline_status(database)
    github = [row for row in status["by_source"] if row["source"] == "github"]
    assert github and github[0]["unprojected"] >= 1
    assert status["unprojected"] >= 1

    project_timeline(database, sources=("github",), dry_run=False)
    after = timeline_status(database)
    github_after = [row for row in after["by_source"] if row["source"] == "github"]
    assert github_after[0]["unprojected"] == 0


@REQUIRES_DATABASE
def test_an_unknown_entity_type_is_refused_by_name(database) -> None:
    result = project_timeline(database, entity_types=("nonsense",), dry_run=True)
    assert result.projected == 0
    assert any("is a projected entity type" in error for error in result.errors)
