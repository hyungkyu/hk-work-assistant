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
def test_the_situation_is_what_gets_matched_and_his_answer_is_what_comes_back():
    """The direction the first real run got wrong.

    Asking "what does this situation look like" of his *answers* returned
    "가는 중." and "ㅋㅋㅋㅋ 알아서 해요" for a question about deployments run
    by hand. The query is a situation, so situations are what the index is
    compared against; his reply is the payload, not the key.

    The reply here is not a thread reply -- most of his messages are not -- so
    it is paired by being the next thing he said in that channel, and the
    pairing is labelled as such.
    """
    import uuid
    from datetime import datetime, timedelta, timezone

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
    rows = {
        "vec-situation": ("UOTHER", moment, "배포 스크립트 보내드릴게요, 직접 돌리시면 됩니다"),
        "vec-mine": ("UVECTOR", moment + timedelta(minutes=2), "이걸 내가 돌려야해? 자동으로 돌게 해야지"),
    }
    ids = {name: uuid.uuid5(uuid.NAMESPACE_URL, f"vec:{name}") for name in rows}

    with psycopg.connect(url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM search_documents WHERE doc_id = ANY(%s)", (list(ids.values()),)
            )
            cursor.execute("DELETE FROM ledger_records WHERE source_entity_id LIKE 'vec-%'")
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
            for name, (author, when, text) in rows.items():
                fields = {
                    "ledger_id": ids[name],
                    "schema_version": "v1",
                    "capture_profile": "live-slack-web-api/v1",
                    "source": "slack",
                    "entity_type": "message",
                    "tenant_workspace_id": "T",
                    "tenant_status": "observed",
                    "scope": Jsonb({"channel_id": "C9"}),
                    "source_entity_id": name,
                    "source_updated_at_status": "observed",
                    "deleted_status": "observed",
                    "raw_payload": Jsonb({"text": text}),
                    "content_hash": name,
                    "relations": Jsonb({"author_user_id": author}),
                    "source_file": "f",
                    "source_file_sha256": "h",
                    "record_pointer": "p",
                    "legacy_layout_version": "v",
                    "converter_version": "v",
                    "observation_role": "current_head",
                    "capture_completeness_status": "recorded",
                    "source_created_at": when,
                    "collected_at": when,
                }
                cursor.execute(
                    f"INSERT INTO ledger_records ({','.join(fields)}) "
                    f"VALUES ({','.join('%s' for _ in fields)})",
                    list(fields.values()),
                )
                cursor.execute(
                    "INSERT INTO search_documents (doc_id, ledger_id, source, entity_type, "
                    "text_content, text_sha256, extractor) "
                    "VALUES (%s, %s, 'slack', 'message', %s, %s, 'test')",
                    (ids[name], ids[name], text, f"sha-{name}"),
                )
        connection.commit()

    embedder = FakeEmbedder()
    try:
        embed_corpus(url, embedder, apply=True, sources=["slack"])
        # The fake embedder is deterministic, so the query that finds the
        # situation is the situation's own words.
        found = precedents(url, str(person), rows["vec-situation"][2], embedder=embedder)
        assert found.matcher == "embedding"
        assert found.found, "the situation was matched"
        best = found.found[0]
        assert "자동으로 돌게" in best.said, "and his answer is what came back"
        assert best.situation == rows["vec-situation"][2]
        assert best.link == "nearby", "no thread link; paired by place and time"
        assert best.certainty == "직후", "nobody else spoke in between"

        plain = precedents(url, str(person), "야간 배치")
        assert plain.matcher == "trigram"
    finally:
        with psycopg.connect(url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM search_documents WHERE doc_id = ANY(%s)",
                    (list(ids.values()),),
                )
                cursor.execute("DELETE FROM ledger_records WHERE source_entity_id LIKE 'vec-%'")
            connection.commit()


@REQUIRES_DATABASE
def test_scoping_to_one_person_covers_their_replies_and_what_they_answered():
    """581,315 documents on the real database, against a few thousand here.

    The precedent search reads his messages and the ones they answered, and
    nothing else. Embedding the whole archive first is ten hours of CPU for
    text this path never opens.
    """
    import uuid
    from datetime import datetime, timezone

    import psycopg
    from psycopg.types.json import Jsonb

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
    person = uuid.uuid4()
    moment = datetime(2026, 9, 17, 2, 0, tzinfo=timezone.utc)
    rows = {
        "scope-parent": ("UOTHER", None, "부모 메시지"),
        "scope-mine": ("USCOPE", "scope-parent", "내 답글"),
        "scope-theirs": ("UOTHER", None, "남의 메시지"),
    }
    ids = {name: uuid.uuid5(uuid.NAMESPACE_URL, f"scope:{name}") for name in rows}

    with psycopg.connect(url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM search_documents WHERE doc_id = ANY(%s)", (list(ids.values()),)
            )
            cursor.execute("DELETE FROM ledger_records WHERE source_entity_id LIKE 'scope-%'")
            cursor.execute("DELETE FROM org_identity WHERE value = 'USCOPE'")
            cursor.execute("DELETE FROM org_person WHERE name = '범위테스트'")
            cursor.execute(
                "INSERT INTO roster_observation (observed_at, source, row_count) "
                "VALUES (now(), 'roster_seed_2', 1) RETURNING observation_id"
            )
            observation = cursor.fetchone()[0]
            cursor.execute(
                "INSERT INTO org_person (person_id, name, first_seen, last_seen) "
                "VALUES (%s, '범위테스트', %s, %s)",
                (person, observation, observation),
            )
            cursor.execute(
                "INSERT INTO org_identity (person_id, kind, value, first_seen, last_seen, "
                "origin) VALUES (%s, 'slack', 'USCOPE', %s, %s, 'roster')",
                (person, observation, observation),
            )
            for name, (author, parent, text) in rows.items():
                fields = {
                    "ledger_id": ids[name],
                    "schema_version": "v1",
                    "capture_profile": "live-slack-web-api/v1",
                    "source": "slack",
                    "entity_type": "message",
                    "tenant_workspace_id": "T",
                    "tenant_status": "observed",
                    "scope": Jsonb({"channel_id": "C8"}),
                    "source_entity_id": name,
                    "source_updated_at_status": "observed",
                    "deleted_status": "observed",
                    "raw_payload": Jsonb({"text": text}),
                    "content_hash": name,
                    "relations": Jsonb({"author_user_id": author, "thread_id": parent}),
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
                    "INSERT INTO search_documents (doc_id, ledger_id, source, entity_type, "
                    "text_content, text_sha256, extractor) "
                    "VALUES (%s, %s, 'slack', 'message', %s, %s, 'test')",
                    (ids[name], ids[name], text, f"sha-{name}"),
                )
        connection.commit()

    embedder = FakeEmbedder()
    try:
        embed_corpus(url, embedder, apply=True, person_id=str(person))
        embedded = set()
        with psycopg.connect(url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT doc_id FROM search_documents WHERE doc_id = ANY(%s) "
                    "AND embedding IS NOT NULL",
                    (list(ids.values()),),
                )
                embedded = {row[0] for row in cursor.fetchall()}

        assert ids["scope-mine"] in embedded, "his own reply"
        assert ids["scope-parent"] in embedded, "and the message it answered"
        assert ids["scope-theirs"] not in embedded, "not the rest of the workspace"



    finally:
        with psycopg.connect(url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM search_documents WHERE doc_id = ANY(%s)",
                    (list(ids.values()),),
                )
                cursor.execute(
                    "DELETE FROM ledger_records WHERE source_entity_id LIKE 'scope-%'"
                )
            connection.commit()


@REQUIRES_DATABASE
def test_the_index_covers_what_the_search_will_ask_about():
    """Inverting the search without reindexing found nothing, silently.

    The search matches situations -- what other people said. If only his own
    words carry vectors, every question falls back to character matching and
    reports zero, which reads as "no precedent" and means "nothing to compare
    against". A message someone else sent shortly before he spoke in that
    channel is a situation, and has to be in the index.
    """
    import uuid
    from datetime import datetime, timedelta, timezone

    import psycopg
    from psycopg.types.json import Jsonb

    from rlwrld_worklog.embedding import embed_corpus

    url = os.environ["WORKLOG_TEST_DATABASE_URL"]
    person = uuid.uuid4()
    moment = datetime(2026, 9, 19, 1, 0, tzinfo=timezone.utc)
    rows = {
        # Someone else, minutes before he spoke in the same channel: a
        # situation, with no thread link anywhere.
        "near-theirs": ("UOTHER", "C7", moment, None),
        "near-mine": ("UNEAR", "C7", moment + timedelta(minutes=3), None),
        # Someone else, in a channel he never spoke in.
        "far-theirs": ("UOTHER", "C6", moment, None),
    }
    ids = {name: uuid.uuid5(uuid.NAMESPACE_URL, f"near:{name}") for name in rows}

    with psycopg.connect(url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM search_documents WHERE doc_id = ANY(%s)", (list(ids.values()),)
            )
            # By id as well as by name: a previous run's rows are the same
            # ids, and a leftover ledger row makes the next suite's row count
            # wrong rather than failing here.
            cursor.execute(
                "DELETE FROM ledger_records WHERE ledger_id = ANY(%s)",
                (list(ids.values()),),
            )
            cursor.execute("DELETE FROM ledger_records WHERE source_entity_id LIKE 'near-%'")
            cursor.execute("DELETE FROM org_identity WHERE value = 'UNEAR'")
            cursor.execute("DELETE FROM org_person WHERE name = '근접테스트'")
            cursor.execute(
                "INSERT INTO roster_observation (observed_at, source, row_count) "
                "VALUES (now(), 'roster_seed_2', 1) RETURNING observation_id"
            )
            observation = cursor.fetchone()[0]
            cursor.execute(
                "INSERT INTO org_person (person_id, name, first_seen, last_seen) "
                "VALUES (%s, '근접테스트', %s, %s)",
                (person, observation, observation),
            )
            cursor.execute(
                "INSERT INTO org_identity (person_id, kind, value, first_seen, last_seen, "
                "origin) VALUES (%s, 'slack', 'UNEAR', %s, %s, 'roster')",
                (person, observation, observation),
            )
            for name, (author, channel, when, parent) in rows.items():
                fields = {
                    "ledger_id": ids[name],
                    "schema_version": "v1",
                    "capture_profile": "live-slack-web-api/v1",
                    "source": "slack",
                    "entity_type": "message",
                    "tenant_workspace_id": "T",
                    "tenant_status": "observed",
                    "scope": Jsonb({"channel_id": channel}),
                    "source_entity_id": name,
                    "source_updated_at_status": "observed",
                    "deleted_status": "observed",
                    "raw_payload": Jsonb({"text": f"{name} 본문"}),
                    "content_hash": name,
                    "relations": Jsonb({"author_user_id": author, "thread_id": parent}),
                    "source_file": "f",
                    "source_file_sha256": "h",
                    "record_pointer": "p",
                    "legacy_layout_version": "v",
                    "converter_version": "v",
                    "observation_role": "current_head",
                    "capture_completeness_status": "recorded",
                    "source_created_at": when,
                    "collected_at": when,
                }
                cursor.execute(
                    f"INSERT INTO ledger_records ({','.join(fields)}) "
                    f"VALUES ({','.join('%s' for _ in fields)})",
                    list(fields.values()),
                )
                cursor.execute(
                    "INSERT INTO search_documents (doc_id, ledger_id, source, entity_type, "
                    "text_content, text_sha256, extractor) "
                    "VALUES (%s, %s, 'slack', 'message', %s, %s, 'test')",
                    (ids[name], ids[name], f"{name} 본문", f"sha-{name}"),
                )
        connection.commit()

    try:
        embed_corpus(url, FakeEmbedder(), apply=True, person_id=str(person))
        with psycopg.connect(url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT doc_id FROM search_documents WHERE doc_id = ANY(%s) "
                    "AND embedding IS NOT NULL",
                    (list(ids.values()),),
                )
                embedded = {row[0] for row in cursor.fetchall()}

        assert ids["near-mine"] in embedded
        assert ids["near-theirs"] in embedded, "the situation he answered"
        assert ids["far-theirs"] not in embedded, "a conversation he never joined"
    finally:
        with psycopg.connect(url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM search_documents WHERE doc_id = ANY(%s)",
                    (list(ids.values()),),
                )
                cursor.execute(
                    "DELETE FROM ledger_records WHERE ledger_id = ANY(%s)",
                    (list(ids.values()),),
                )
                cursor.execute("DELETE FROM ledger_records WHERE source_entity_id LIKE 'near-%'")
            connection.commit()
