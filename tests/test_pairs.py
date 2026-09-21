"""Candidate questions for one answer, proposed and correctable.

HK, 2026-09-21: "이 답변의 원 질문은 이거 일거 같다는 후보들이 있어서, 난 그걸
선택하는거지. 정확히는 네가 페어링을 한것을 가정하되, 나는 수정할 수 있게
하는거지." So the pairing is a proposal with the alternatives kept beside it,
and a correction is stored against what it corrected -- otherwise there is no
way to tell whether the proposing improves.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from rlwrld_worklog.blocks import BASIS_SCORE, quoted_refs

REQUIRES_DATABASE = pytest.mark.skipif(
    not os.environ.get("WORKLOG_TEST_DATABASE_URL"),
    reason="set WORKLOG_TEST_DATABASE_URL to a throwaway database",
)

START = datetime(2026, 9, 12, 1, 0, tzinfo=timezone.utc)


def test_a_quoted_permalink_names_the_message_exactly():
    """HK: 쓰레드를 인용/옮겨서 댓글을 다는 경우도 있어서.

    The ts arrives with its dot removed. Putting it back wrong yields a ts
    that matches nothing and is indistinguishable from "no quote".
    """
    found = quoted_refs(
        "이거 보세요 <https://rlwrld.slack.com/archives/C08HA0YKXD4/p1789638401411289>"
    )
    assert found == [("C08HA0YKXD4", "1789638401.411289")]


def test_two_links_to_the_same_message_are_one_candidate():
    text = (
        "https://x.slack.com/archives/C1/p1700000000111111 "
        "https://x.slack.com/archives/C1/p1700000000111111"
    )
    assert quoted_refs(text) == [("C1", "1700000000.111111")]


def test_text_with_no_link_offers_no_quote():
    assert quoted_refs("그냥 이야기") == []
    assert quoted_refs(None) == []


def test_slack_own_link_outranks_a_guess():
    """A thread link is Slack's record; a nearby message is this system
    guessing. The order is a starting point that corrections can move."""
    assert BASIS_SCORE["thread"] > BASIS_SCORE["quote"] > BASIS_SCORE["window"]


@REQUIRES_DATABASE
def test_the_routes_are_offered_together_and_the_top_one_is_only_a_proposal():
    import psycopg
    from psycopg.types.json import Jsonb

    from rlwrld_worklog.blocks import (
        agreement,
        choose_pair,
        pair_queue,
        propose_pairs,
    )
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
    quoted = "1757000000.111111"
    rows = [
        # The thread's opening message, two days before his reply.
        ("pair-parent", "UTHEM", None, -2880, "이 설계 리스크가 뭘까요"),
        # A message he quotes by permalink, from another channel.
        (quoted, "UTHEM", None, -60, "예산이 초과될 것 같습니다", "COTHER"),
        # Someone talking in the same channel a minute before he speaks.
        ("pair-near", "UTHEM", None, -1, "점심 뭐 드세요"),
        # His answer: a thread reply that also quotes something.
        (
            "pair-mine",
            "UPAIR",
            "pair-parent",
            0,
            "롤백 경로가 없는 게 리스크야 "
            "<https://x.slack.com/archives/COTHER/p1757000000111111>",
        ),
    ]

    with psycopg.connect(url) as connection:
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM answer_pairs WHERE channel IN ('CPAIR', 'COTHER')")
            cursor.execute("DELETE FROM ledger_records WHERE source_entity_id LIKE 'pair-%'")
            cursor.execute(
                "DELETE FROM ledger_records WHERE source_entity_id = %s", (quoted,)
            )
            cursor.execute("DELETE FROM org_identity WHERE value = 'UPAIR'")
            cursor.execute("DELETE FROM org_person WHERE name = '페어테스트'")
            cursor.execute(
                "INSERT INTO roster_observation (observed_at, source, row_count) "
                "VALUES (now(), 'roster_seed_2', 1) RETURNING observation_id"
            )
            observation = cursor.fetchone()[0]
            cursor.execute(
                "INSERT INTO org_person (person_id, name, first_seen, last_seen) "
                "VALUES (%s, '페어테스트', %s, %s)",
                (person, observation, observation),
            )
            cursor.execute(
                "INSERT INTO org_identity (person_id, kind, value, first_seen, last_seen, "
                "origin) VALUES (%s, 'slack', 'UPAIR', %s, %s, 'roster')",
                (person, observation, observation),
            )
            for row in rows:
                entity, author, parent, minute, text = row[:5]
                channel = row[5] if len(row) > 5 else "CPAIR"
                fields = {
                    "ledger_id": uuid.uuid5(uuid.NAMESPACE_URL, f"pair:{entity}"),
                    "schema_version": "v1",
                    "capture_profile": "live-slack-web-api/v1",
                    "source": "slack",
                    "entity_type": "message",
                    "tenant_workspace_id": "T",
                    "tenant_status": "observed",
                    "scope": Jsonb({"channel_id": channel}),
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
        built = propose_pairs(
            url,
            str(person),
            since=START - timedelta(days=7),
            names={"UPAIR": "HK", "UTHEM": "스톰"},
            apply=True,
        )
        assert built.answers == 1
        # All three routes found something for the same answer, which is the
        # case a single-answer design cannot represent.
        assert built.by_basis == {"thread": 1, "quote": 1, "window": 1}

        queue = pair_queue(url, str(person))
        assert queue["counts"] == {"decided": 0, "undecided": 1}
        answer = queue["answers"][0]
        assert "롤백 경로가 없는" in answer["answer_text"]
        candidates = answer["candidates"]
        assert [item["basis"] for item in candidates] == ["thread", "quote", "window"]
        assert candidates[0]["proposed"] is True, "the thread link is the proposal"
        assert all(item["chosen"] is False for item in candidates)
        assert "리스크가 뭘까요" in candidates[0]["text"]
        assert "예산이 초과" in candidates[1]["text"]
        assert "점심" in candidates[2]["text"], "offered, and clearly not it"

        # He corrects the proposal: the question was the quoted message.
        assert choose_pair(
            url, answer["answer_ledger_id"], candidates[1]["pair_id"], actor="hk"
        )
        decided = pair_queue(url, str(person), state="decided")["answers"][0]
        chosen = [item for item in decided["candidates"] if item["chosen"]]
        assert len(chosen) == 1 and chosen[0]["basis"] == "quote"

        measured = agreement(url, str(person))
        assert measured["decided_answers"] == 1
        assert measured["agreed"] == 0 and measured["corrected"] == 1
        assert measured["agreement_rate"] == 0.0
        assert measured["chosen_basis"] == {"quote": 1}

        # And "none of these" is one call, not an omission.
        assert choose_pair(url, answer["answer_ledger_id"], None, actor="hk")
        after = agreement(url, str(person))
        assert after["none_of_these"] == 1
        assert after["agreement_rate"] is None, (
            "no rate over zero choices -- that is an unanswered question, not 0%"
        )
    finally:
        with psycopg.connect(url) as connection:
            with connection.cursor() as cursor:
                cursor.execute("DELETE FROM answer_pairs WHERE person_id = %s", (str(person),))
                cursor.execute(
                    "DELETE FROM ledger_records WHERE source_entity_id LIKE 'pair-%'"
                )
                cursor.execute(
                    "DELETE FROM ledger_records WHERE source_entity_id = %s", (quoted,)
                )
            connection.commit()


@REQUIRES_DATABASE
def test_a_rebuild_does_not_undo_a_decision():
    """A correction is a person's work. Re-running the proposer must not ask
    the same question again."""
    from rlwrld_worklog.blocks import _PAIR_WRITE_SQL

    # Assignments only: the word appears in the comment explaining why it is
    # absent here, and a test that reads comments tests nothing.
    updates = [
        line.split("=")[0].strip()
        for line in _PAIR_WRITE_SQL.split("DO UPDATE SET")[1].splitlines()
        if "=" in line and not line.strip().startswith("--")
    ]
    assert "chosen" not in updates
    assert "decided_by" not in updates and "decided_at" not in updates
    assert "proposed" in updates, "the guess may be revised; the decision may not"
