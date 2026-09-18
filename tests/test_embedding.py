"""Filling and using the embedding column.

A fake embedder stands in for bge-m3: deterministic vectors of the right
width, so the batch, the width check and the ranking are all exercised without
downloading two gigabytes.
"""

from __future__ import annotations

import hashlib
import os

import pytest

from rlwrld_worklog.embedding import EMBED_DIM, EmbedResult, check_width

REQUIRES_DATABASE = pytest.mark.skipif(
    not os.environ.get("WORKLOG_TEST_DATABASE_URL"),
    reason="set WORKLOG_TEST_DATABASE_URL to a throwaway database",
)


class FakeEmbedder:
    """Stable pseudo-vectors: the same text always lands in the same place."""

    name = "fake/test-1024"

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def embed(self, texts):
        self.calls.append(list(texts))
        found = []
        for text in texts:
            seed = hashlib.sha256(text.encode("utf-8")).digest()
            raw = [seed[index % len(seed)] / 255.0 for index in range(EMBED_DIM)]
            norm = sum(value * value for value in raw) ** 0.5 or 1.0
            found.append([value / norm for value in raw])
        return found


class NarrowEmbedder(FakeEmbedder):
    name = "fake/too-narrow"

    def embed(self, texts):
        return [[0.1] * 384 for _ in texts]


def test_a_model_of_the_wrong_width_is_refused_not_truncated():
    """A vector cut to fit still inserts, still compares, and is nonsense."""
    with pytest.raises(ValueError) as error:
        check_width(NarrowEmbedder().embed(["x"]), model="fake/too-narrow")
    assert "384" in str(error.value) and str(EMBED_DIM) in str(error.value)


def test_the_right_width_passes():
    check_width(FakeEmbedder().embed(["안녕"]), model="fake/test-1024")


def test_a_dry_run_reports_the_backlog_and_writes_nothing():
    found = EmbedResult(dry_run=True, model="m", candidates=1200)
    assert found.as_dict()["candidates"] == 1200
    assert found.as_dict()["embedded"] == 0


@REQUIRES_DATABASE
def test_the_batch_fills_the_column_and_a_second_run_finds_less():
    import uuid

    import psycopg

    from rlwrld_worklog.embedding import embed_corpus
    from rlwrld_worklog.ledger.load import apply_migrations

    url = os.environ["WORKLOG_TEST_DATABASE_URL"]
    apply_migrations(
        database_url=url,
        migrations_dir=__import__("pathlib").Path(__file__).resolve().parents[1]
        / "sql"
        / "migrations",
        dry_run=False,
    )
    ids = [uuid.uuid5(uuid.NAMESPACE_URL, f"embed-test:{index}") for index in range(3)]
    with psycopg.connect(url) as connection:
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM search_documents WHERE doc_id = ANY(%s)", (ids,))
            for index, doc_id in enumerate(ids):
                cursor.execute(
                    "INSERT INTO search_documents "
                    "(doc_id, source, entity_type, text_content, text_sha256, extractor) "
                    "VALUES (%s, 'slack', 'message', %s, %s, 'test')",
                    (doc_id, f"배포는 배치가 해야지 {index}", f"sha-{index}"),
                )
        connection.commit()

    embedder = FakeEmbedder()
    try:
        dry = embed_corpus(url, embedder, apply=False)
        assert dry.candidates >= 3
        assert dry.embedded == 0
        assert not embedder.calls, "a dry run does not call the model"

        run = embed_corpus(url, embedder, apply=True)
        assert run.embedded >= 3
        assert run.batches >= 1

        again = embed_corpus(url, embedder, apply=False)
        assert again.candidates == dry.candidates - run.embedded

        with psycopg.connect(url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT count(*) FROM search_documents "
                    "WHERE doc_id = ANY(%s) AND embedding IS NOT NULL "
                    "AND embedding_model = %s AND embedded_at IS NOT NULL",
                    (ids, embedder.name),
                )
                assert cursor.fetchone()[0] == 3
    finally:
        with psycopg.connect(url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM search_documents WHERE doc_id = ANY(%s)", (ids,)
                )
            connection.commit()


@REQUIRES_DATABASE
def test_a_precedent_is_found_when_the_words_differ():
    """The whole reason for embeddings.

    The trigram query cannot connect "배포를 자동으로 돌게 해야지" to a
    question about a nightly batch: no shared run of characters. Matching by
    meaning is the point, and with a fake embedder the check is that the
    vector path is the one that answered.
    """
    import uuid
    from datetime import datetime, timezone

    import psycopg
    from psycopg.types.json import Jsonb

    from rlwrld_worklog.embedding import embed_corpus
    from rlwrld_worklog.ledger.load import apply_migrations
    from rlwrld_worklog.voice import precedents

    url = os.environ["WORKLOG_TEST_DATABASE_URL"]
    apply_migrations(
        database_url=url,
        migrations_dir=__import__("pathlib").Path(__file__).resolve().parents[1]
        / "sql"
        / "migrations",
        dry_run=False,
    )
    person = uuid.uuid4()
    moment = datetime(2026, 9, 17, 1, 0, tzinfo=timezone.utc)
    reply_id = uuid.uuid5(uuid.NAMESPACE_URL, "vec:reply")

    with psycopg.connect(url) as connection:
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM ledger_records WHERE source_entity_id LIKE 'vec-%'")
            # By doc_id too: a previous run of this test, or of the search
            # index suite, may already hold this row.
            cursor.execute("DELETE FROM search_documents WHERE doc_id = %s", (reply_id,))
            cursor.execute("DELETE FROM search_documents WHERE ledger_id = %s", (reply_id,))
            cursor.execute("DELETE FROM org_identity WHERE value = 'UVECTOR'")
            cursor.execute("DELETE FROM org_person WHERE name = '벡터테스트'")
            cursor.execute(
                "INSERT INTO roster_observation (observed_at, source, row_count) "
                "VALUES (now(), 'roster_seed_2', 1) RETURNING observation_id"
            )
            observation = cursor.fetchone()[0]
            cursor.execute(
                "INSERT INTO org_person (person_id, name, first_seen, last_seen) "
                "VALUES (%s, '벡터테스트', %s, %s)",
                (person, observation, observation),
            )
            cursor.execute(
                "INSERT INTO org_identity (person_id, kind, value, first_seen, last_seen, "
                "origin) VALUES (%s, 'slack', 'UVECTOR', %s, %s, 'roster')",
                (person, observation, observation),
            )
            fields = {
                "ledger_id": reply_id,
                "schema_version": "v1",
                "capture_profile": "live-slack-web-api/v1",
                "source": "slack",
                "entity_type": "message",
                "tenant_workspace_id": "T",
                "tenant_status": "observed",
                "scope": Jsonb({"channel_id": "C9"}),
                "source_entity_id": "vec-reply",
                "source_updated_at_status": "observed",
                "deleted_status": "observed",
                "raw_payload": Jsonb({"text": "이걸 내가 돌려야해? 자동으로 돌게 해야지"}),
                "content_hash": "vec-reply",
                "relations": Jsonb({"author_user_id": "UVECTOR"}),
                "source_file": "f",
                "source_file_sha256": "h",
                "record_pointer": "p",
                "legacy_layout_version": "v",
                "converter_version": "v",
                "observation_role": "current_head",
                "capture_completeness_status": "recorded",
                "source_created_at": moment,
                "collected_at": moment,
            }
            cursor.execute(
                f"INSERT INTO ledger_records ({','.join(fields)}) "
                f"VALUES ({','.join('%s' for _ in fields)})",
                list(fields.values()),
            )
            cursor.execute(
                "INSERT INTO search_documents "
                "(doc_id, ledger_id, source, entity_type, text_content, text_sha256, "
                "extractor) VALUES (%s, %s, 'slack', 'message', %s, 'sha-vec', 'test')",
                (reply_id, reply_id, "이걸 내가 돌려야해? 자동으로 돌게 해야지"),
            )
        connection.commit()

    embedder = FakeEmbedder()
    try:
        embed_corpus(url, embedder, apply=True, sources=["slack"])
        found = precedents(url, str(person), "야간 배치", embedder=embedder)
        assert found.matcher == "embedding", "the vector path answered"
        assert found.found and "자동으로 돌게" in found.found[0].said

        # And with no embedder the same question falls back and says so.
        plain = precedents(url, str(person), "야간 배치")
        assert plain.matcher == "trigram"
    finally:
        with psycopg.connect(url) as connection:
            with connection.cursor() as cursor:
                cursor.execute("DELETE FROM ledger_records WHERE source_entity_id LIKE 'vec-%'")
                cursor.execute("DELETE FROM search_documents WHERE doc_id = %s", (reply_id,))
                cursor.execute(
                    "DELETE FROM search_documents WHERE ledger_id = %s", (reply_id,)
                )
            connection.commit()
