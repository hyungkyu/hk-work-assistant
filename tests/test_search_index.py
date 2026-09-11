"""The indexer against a real database: ledger_records in, search_documents out.

Runs only against `WORKLOG_TEST_DATABASE_URL`, like every other database round
trip here, and removes everything it inserted -- the loader round-trip tests
verify that the database holds exactly what the ledger files hold, and rows
left behind make that check fail about a problem that does not exist.
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rlwrld_worklog.ledger.extract_text import (  # noqa: E402
    EXTRACTOR_VERSION,
    document_id,
    index_ledger_text,
)

REQUIRES_DATABASE = pytest.mark.skipif(
    not os.environ.get("WORKLOG_TEST_DATABASE_URL"),
    reason="set WORKLOG_TEST_DATABASE_URL to a throwaway database to run the index round trip",
)

TENANT = "index-round-trip-test"

RECORD_SQL = """
INSERT INTO ledger_records (
    ledger_id, schema_version, capture_profile, source, entity_type,
    tenant_workspace_id, tenant_status, source_entity_id,
    source_updated_at, source_updated_at_status, deleted_status,
    raw_payload, content_hash, source_file, source_file_sha256,
    record_pointer, legacy_layout_version, converter_version,
    observation_role, capture_completeness_status, inserted_at
) VALUES (
    %(ledger_id)s, '1.0', 'test/v1', %(source)s, %(entity_type)s,
    %(tenant)s, 'active', %(entity)s,
    %(updated)s, 'observed', 'unknown',
    %(payload)s, %(hash)s, 'test.jsonl', 'sha', 'p:0', 'test', 'test',
    %(role)s, 'unknown', %(inserted_at)s
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
    yield url
    with psycopg.connect(url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM search_documents WHERE doc_id IN ("
                "SELECT md5(source || ':' || source_entity_id)::uuid"
                "  FROM ledger_records WHERE tenant_workspace_id = %s)",
                (TENANT,),
            )
            cursor.execute(
                "DELETE FROM ledger_records WHERE tenant_workspace_id = %s", (TENANT,)
            )
        connection.commit()


def _insert(url: str, *, source: str, entity_type: str, entity: str, payload: dict,
            role: str = "current_head", updated: str = "2026-09-10T12:00:00+00:00",
            inserted_at: str = "now()") -> str:
    import psycopg
    from psycopg.types.json import Jsonb

    ledger_id = str(uuid.uuid4())
    sql = RECORD_SQL if inserted_at != "now()" else RECORD_SQL.replace(
        "%(inserted_at)s", "now()"
    )
    with psycopg.connect(url) as connection:
        with connection.cursor() as cursor:
            parameters = {
                "ledger_id": ledger_id,
                "source": source,
                "entity_type": entity_type,
                "tenant": TENANT,
                "entity": entity,
                "updated": updated,
                "payload": Jsonb(payload),
                "hash": ledger_id,
                "role": role,
            }
            if inserted_at != "now()":
                parameters["inserted_at"] = inserted_at
            cursor.execute(sql, parameters)
        connection.commit()
    return ledger_id


def _documents(url: str) -> dict[str, dict]:
    import psycopg

    with psycopg.connect(url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT d.external_id, d.source, d.text_content, d.extractor"
                "  FROM search_documents d JOIN ledger_records r ON r.ledger_id = d.ledger_id"
                " WHERE r.tenant_workspace_id = %s",
                (TENANT,),
            )
            return {
                row[0]: {"source": row[1], "text": row[2], "extractor": row[3]}
                for row in cursor.fetchall()
            }


@REQUIRES_DATABASE
def test_every_source_with_text_lands_in_the_corpus(database) -> None:
    """The defect this module exists for: only notion legacy text was searchable."""
    _insert(database, source="slack", entity_type="message", entity="C1/1.0",
            payload={"text": "슬랙에서 수집을 이야기했다"})
    _insert(database, source="github", entity_type="commit", entity="sha1",
            payload={"commit": {"message": "Fix the Notion walk"}})
    _insert(database, source="slurm", entity_type="job", entity="job-1",
            payload={"job_name": "vla-train", "user": "mskim"})
    _insert(database, source="slack", entity_type="message", entity="C1/2.0",
            payload={"subtype": "channel_join"})  # nothing to search

    result = index_ledger_text(database, dry_run=False)

    documents = _documents(database)
    assert set(documents) == {"C1/1.0", "sha1", "job-1"}
    assert documents["sha1"]["text"] == "Fix the Notion walk"
    assert all(doc["extractor"] == EXTRACTOR_VERSION for doc in documents.values())
    assert result.without_text >= 1


@REQUIRES_DATABASE
def test_one_entity_observed_twice_is_one_document_with_the_head_text(database) -> None:
    """A backfill observation and a current head are the same message once."""
    _insert(database, source="slack", entity_type="message", entity="C2/9.9",
            payload={"text": "옛 관측"}, role="historical_observation",
            updated="2026-08-01T00:00:00+00:00")
    _insert(database, source="slack", entity_type="message", entity="C2/9.9",
            payload={"text": "현재 헤드"}, role="current_head")

    index_ledger_text(database, dry_run=False)

    documents = _documents(database)
    assert documents["C2/9.9"]["text"] == "현재 헤드"
    assert document_id("slack", "C2/9.9") is not None


@REQUIRES_DATABASE
def test_a_second_run_scans_nothing_already_indexed(database) -> None:
    _insert(database, source="slack", entity_type="message", entity="C3/1.1",
            payload={"text": "한 번만"})
    first = index_ledger_text(database, sources=("slack",), dry_run=False)
    assert first.indexed >= 1
    again = index_ledger_text(database, sources=("slack",), dry_run=False)
    assert again.scanned == again.without_text  # only the text-less leftovers


@REQUIRES_DATABASE
def test_a_newer_observation_reindexes_its_entity(database) -> None:
    """An edit collected tonight must replace last week's text."""
    _insert(database, source="slack", entity_type="message", entity="C4/1.1",
            payload={"text": "수정 전"})
    index_ledger_text(database, sources=("slack",), dry_run=False)
    _insert(database, source="slack", entity_type="message", entity="C4/1.1",
            payload={"text": "수정 후"}, updated="2026-09-10T13:00:00+00:00")
    index_ledger_text(database, sources=("slack",), dry_run=False)
    assert _documents(database)["C4/1.1"]["text"] == "수정 후"


@REQUIRES_DATABASE
def test_a_dry_run_writes_nothing(database) -> None:
    _insert(database, source="slack", entity_type="message", entity="C5/1.1",
            payload={"text": "쓰지 않는다"})
    result = index_ledger_text(database, sources=("slack",), dry_run=True)
    assert result.indexed >= 1
    assert "C5/1.1" not in _documents(database)
