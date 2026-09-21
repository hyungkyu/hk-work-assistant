"""Cutting conversations into blocks, which is what a situation is.

The cutting rule is a pure function, so it is argued about here rather than in
a database. The round trip then checks the two texts stay apart -- what others
said is the key, what he said is the payload, and mixing them is the bug this
replaced.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

from rlwrld_worklog.blocks import block_id_for, cut, render

REQUIRES_DATABASE = pytest.mark.skipif(
    not os.environ.get("WORKLOG_TEST_DATABASE_URL"),
    reason="set WORKLOG_TEST_DATABASE_URL to a throwaway database",
)

START = datetime(2026, 9, 10, 1, 0, tzinfo=timezone.utc)


def _message(minute: int, *, author="UOTHER", text="말"):
    return {
        "ts": f"17{minute:08d}.0",
        "at": START + timedelta(minutes=minute),
        "author": author,
        "text": text,
        "permalink": None,
    }


def test_a_quiet_gap_starts_a_new_conversation():
    """This morning and this afternoon are two situations, not one."""
    runs = cut(
        [_message(0), _message(2), _message(200), _message(201)], gap_minutes=30
    )
    assert [len(run) for run in runs] == [2, 2]


def test_a_busy_channel_does_not_become_one_document():
    """The cap keeps a block inside what the model can read, and keeps a day
    of traffic from collapsing into a single key."""
    runs = cut([_message(index) for index in range(10)], max_messages=4)
    assert [len(run) for run in runs] == [4, 4, 2]


def test_a_slow_exchange_stays_one_conversation():
    runs = cut([_message(0), _message(20), _message(45)], gap_minutes=30)
    assert len(runs) == 1


def test_the_block_reads_as_people_talking():
    """Handles are not words. `U07…: 배포 됐나요` embeds an id as vocabulary."""
    found = render(
        [_message(0, author="U1", text="배포 됐나요"), _message(1, author="U2", text="아직")],
        {"U1": "제럴드", "U2": "스톰"},
    )
    assert found == "제럴드: 배포 됐나요\n스톰: 아직"
    # An unknown handle keeps its id rather than becoming a plausible name.
    assert "U9" in render([_message(0, author="U9")], {})


def test_the_same_conversation_rebuilds_to_the_same_id():
    first = block_id_for("C1", "1700.0", gap=30, cap=12)
    assert first == block_id_for("C1", "1700.0", gap=30, cap=12)
    # A different rule is a different object: blocks built under two windows
    # must not be compared with each other.
    assert first != block_id_for("C1", "1700.0", gap=60, cap=12)


@REQUIRES_DATABASE
def test_what_others_said_and_what_he_said_are_kept_apart():
    import uuid

    import psycopg
    from psycopg.types.json import Jsonb

    from rlwrld_worklog.blocks import build_blocks
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
    rows = [
        ("blk-1", "UTHEM", 0, "배포 스크립트 보내드릴게요"),
        ("blk-2", "UTHEM", 1, "직접 돌리시면 됩니다"),
        ("blk-3", "UBLOCK", 2, "이걸 내가 돌려야해? 배치가 해야지"),
        # Hours later: a different conversation, and he is not in it.
        ("blk-4", "UTHEM", 400, "점심 뭐 드세요"),
    ]

    with psycopg.connect(url) as connection:
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM conversation_blocks WHERE channel = 'CBLOCK'")
            cursor.execute("DELETE FROM ledger_records WHERE source_entity_id LIKE 'blk-%'")
            cursor.execute("DELETE FROM org_identity WHERE value = 'UBLOCK'")
            cursor.execute("DELETE FROM org_person WHERE name = '블록테스트'")
            cursor.execute(
                "INSERT INTO roster_observation (observed_at, source, row_count) "
                "VALUES (now(), 'roster_seed_2', 1) RETURNING observation_id"
            )
            observation = cursor.fetchone()[0]
            cursor.execute(
                "INSERT INTO org_person (person_id, name, first_seen, last_seen) "
                "VALUES (%s, '블록테스트', %s, %s)",
                (person, observation, observation),
            )
            cursor.execute(
                "INSERT INTO org_identity (person_id, kind, value, first_seen, last_seen, "
                "origin) VALUES (%s, 'slack', 'UBLOCK', %s, %s, 'roster')",
                (person, observation, observation),
            )
            for entity, author, minute, text in rows:
                fields = {
                    "ledger_id": uuid.uuid5(uuid.NAMESPACE_URL, f"blk:{entity}"),
                    "schema_version": "v1",
                    "capture_profile": "live-slack-web-api/v1",
                    "source": "slack",
                    "entity_type": "message",
                    "tenant_workspace_id": "T",
                    "tenant_status": "observed",
                    "scope": Jsonb({"channel_id": "CBLOCK"}),
                    "source_entity_id": entity,
                    "source_updated_at_status": "observed",
                    "deleted_status": "observed",
                    "raw_payload": Jsonb({"text": text, "permalink": f"https://x/{entity}"}),
                    "content_hash": entity,
                    "relations": Jsonb({"author_user_id": author}),
                    "source_file": "f",
                    "source_file_sha256": "h",
                    "record_pointer": "p",
                    "legacy_layout_version": "v",
                    "converter_version": "v",
                    "observation_role": "current_head",
                    "capture_completeness_status": "recorded",
                    "source_created_at": START + timedelta(minutes=minute),
                    "collected_at": START,
                }
                cursor.execute(
                    f"INSERT INTO ledger_records ({','.join(fields)}) "
                    f"VALUES ({','.join('%s' for _ in fields)})",
                    list(fields.values()),
                )
        connection.commit()

    try:
        dry = build_blocks(url, str(person), names={"UBLOCK": "HK", "UTHEM": "제럴드"})
        assert dry.blocks == 2, "two conversations in that channel"
        assert dry.blocks_with_him == 1, "he is in one of them"

        build_blocks(
            url, str(person), names={"UBLOCK": "HK", "UTHEM": "제럴드"}, apply=True
        )
        with psycopg.connect(url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT situation_text, his_text, message_count, speaker_count, "
                    "       permalink, embedding IS NULL "
                    "  FROM conversation_blocks WHERE channel = 'CBLOCK'"
                )
                found = cursor.fetchall()

        assert len(found) == 1
        situation, his, count, speakers, permalink, unembedded = found[0]
        assert "배포 스크립트" in situation and "직접 돌리시면" in situation
        assert "내가 돌려야해" not in situation, "his words are not part of the key"
        assert his == "HK: 이걸 내가 돌려야해? 배치가 해야지"
        assert count == 3 and speakers == 2
        assert permalink == "https://x/blk-3", "the link goes to what he said"
        assert unembedded, "a new block has no vector yet"

        # And the situation he never joined is not a block of his.
        assert "점심" not in situation
    finally:
        with psycopg.connect(url) as connection:
            with connection.cursor() as cursor:
                cursor.execute("DELETE FROM conversation_blocks WHERE channel = 'CBLOCK'")
                cursor.execute(
                    "DELETE FROM ledger_records WHERE source_entity_id LIKE 'blk-%'"
                )
            connection.commit()


@REQUIRES_DATABASE
def test_rebuilding_a_block_drops_its_stale_vector():
    """A rewritten block is a different document.

    Keeping the old vector would leave a key that still ranks and no longer
    describes its own text -- the quiet kind of wrong this project keeps
    finding.
    """
    from rlwrld_worklog.blocks import _WRITE_SQL

    assert "embedding = NULL" in _WRITE_SQL
    assert "embedding_model = NULL" in _WRITE_SQL


@REQUIRES_DATABASE
def test_a_thread_is_followed_to_its_first_message_however_old():
    """HK: 내가 쓴글이 댓글이면 원글을 찾고... 뭐 이런식으로 탐색해 보는건 어때?

    Right, and a time window cannot do it. A thread's opening message can be
    hours or days before his reply, so no run of nearby messages contains it.
    `thread_id` says exactly which message he answered -- the one path here
    that infers nothing at all.

    A thread whose opening message was never collected is counted, not
    silently kept: his answer is there and the question is missing.
    """
    import uuid

    import psycopg
    from psycopg.types.json import Jsonb

    from rlwrld_worklog.blocks import build_blocks
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
    # The parent is three days before the reply: far outside any window.
    rows = [
        ("thr-parent", "UTHEM", None, -4320, "이 설계로 가면 리스크가 뭘까요"),
        ("thr-mine", "UTHREAD", "thr-parent", 0, "롤백 경로가 없는 게 리스크야"),
        # A second thread whose opening message is not in the ledger at all.
        ("thr-orphan", "UTHREAD", "thr-missing", 5, "그건 다음 주에 보죠"),
    ]

    with psycopg.connect(url) as connection:
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM conversation_blocks WHERE channel = 'CTHREAD'")
            cursor.execute("DELETE FROM ledger_records WHERE source_entity_id LIKE 'thr-%'")
            cursor.execute("DELETE FROM org_identity WHERE value = 'UTHREAD'")
            cursor.execute("DELETE FROM org_person WHERE name = '스레드테스트'")
            cursor.execute(
                "INSERT INTO roster_observation (observed_at, source, row_count) "
                "VALUES (now(), 'roster_seed_2', 1) RETURNING observation_id"
            )
            observation = cursor.fetchone()[0]
            cursor.execute(
                "INSERT INTO org_person (person_id, name, first_seen, last_seen) "
                "VALUES (%s, '스레드테스트', %s, %s)",
                (person, observation, observation),
            )
            cursor.execute(
                "INSERT INTO org_identity (person_id, kind, value, first_seen, last_seen, "
                "origin) VALUES (%s, 'slack', 'UTHREAD', %s, %s, 'roster')",
                (person, observation, observation),
            )
            for entity, author, parent, minute, text in rows:
                fields = {
                    "ledger_id": uuid.uuid5(uuid.NAMESPACE_URL, f"thr:{entity}"),
                    "schema_version": "v1",
                    "capture_profile": "live-slack-web-api/v1",
                    "source": "slack",
                    "entity_type": "message",
                    "tenant_workspace_id": "T",
                    "tenant_status": "observed",
                    "scope": Jsonb({"channel_id": "CTHREAD"}),
                    "source_entity_id": entity,
                    "source_updated_at_status": "observed",
                    "deleted_status": "observed",
                    "raw_payload": Jsonb({"text": text, "permalink": f"https://x/{entity}"}),
                    "content_hash": entity,
                    "relations": Jsonb({"author_user_id": author, "thread_id": parent}),
                    "source_file": "f",
                    "source_file_sha256": "h",
                    "record_pointer": "p",
                    "legacy_layout_version": "v",
                    "converter_version": "v",
                    "observation_role": "current_head",
                    "capture_completeness_status": "recorded",
                    "source_created_at": START + timedelta(minutes=minute),
                    "collected_at": START,
                }
                cursor.execute(
                    f"INSERT INTO ledger_records ({','.join(fields)}) "
                    f"VALUES ({','.join('%s' for _ in fields)})",
                    list(fields.values()),
                )
        connection.commit()

    try:
        result = build_blocks(
            url, str(person), names={"UTHREAD": "HK", "UTHEM": "스톰"}, apply=True
        )
        assert result.thread_blocks == 1
        assert result.threads_without_parent == 1, "his answer, question missing"

        with psycopg.connect(url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT kind, situation_text, his_text FROM conversation_blocks "
                    " WHERE channel = 'CTHREAD' AND kind = 'thread'"
                )
                found = cursor.fetchall()

        assert len(found) == 1
        kind, situation, his = found[0]
        assert kind == "thread"
        assert situation == "스톰: 이 설계로 가면 리스크가 뭘까요", (
            "the opening message, three days back, reached by structure"
        )
        assert his == "HK: 롤백 경로가 없는 게 리스크야"
    finally:
        with psycopg.connect(url) as connection:
            with connection.cursor() as cursor:
                cursor.execute("DELETE FROM conversation_blocks WHERE channel = 'CTHREAD'")
                cursor.execute(
                    "DELETE FROM ledger_records WHERE source_entity_id LIKE 'thr-%'"
                )
            connection.commit()
