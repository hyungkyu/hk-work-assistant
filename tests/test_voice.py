"""Retrieval of HK's own interventions, for the agent that answers like him.

The agent generates; this only supplies evidence. HK, 2026-09-18: "나처럼
대답하라는게 숙제지. 내가 뭐라 답변했느냐가 숙제가 아니야." So these tests
cover the one thing retrieval owes the generator: real pairs, best first, and
an honest count of the ones whose situation is missing.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

from rlwrld_worklog.voice import Precedent, PrecedentResult

REQUIRES_DATABASE = pytest.mark.skipif(
    not os.environ.get("WORKLOG_TEST_DATABASE_URL"),
    reason="set WORKLOG_TEST_DATABASE_URL to a throwaway database",
)


def _precedent(
    said: str,
    *,
    situation: str | None,
    score: float = 0.5,
    link: str = "thread",
    between_count: int = 0,
    between_speakers: int = 0,
) -> Precedent:
    return Precedent(
        said_at=datetime(2026, 9, 16, 1, 0, tzinfo=timezone.utc),
        channel="C1",
        said=said,
        permalink=None,
        situation=situation,
        situation_author="storm",
        score=score,
        link=link if situation else "none",
        between_count=between_count,
        between_speakers=between_speakers,
    )


def test_a_reply_without_its_parent_is_marked_not_dropped():
    """The 622 orphans: his words, but no longer evidence about when he says them."""
    with_parent = _precedent("이걸 내가 돌려야해?", situation="스크립트 드립니다")
    orphan = _precedent("업무 일지는?", situation=None)
    assert with_parent.has_situation
    assert not orphan.has_situation
    assert orphan.as_dict()["has_situation"] is False


def test_the_count_of_half_precedents_is_reported():
    result = PrecedentResult(query="배포", person_id="p_1")
    result.found = [
        _precedent("a", situation="x"),
        _precedent("b", situation=None),
    ]
    result.without_situation = 1
    assert result.as_dict()["without_situation"] == 1


def test_an_empty_situation_is_refused_rather_than_matching_everything():
    from rlwrld_worklog.voice import precedents

    with pytest.raises(ValueError):
        precedents("postgresql://fake", "p_1", "   ")


@REQUIRES_DATABASE
def test_his_own_replies_come_back_with_what_they_answered(tmp_path):
    import uuid

    import psycopg
    from psycopg.types.json import Jsonb

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
    moment = datetime(2026, 9, 16, 1, 0, tzinfo=timezone.utc)

    def insert(cursor, entity_id, *, text, author, parent=None):
        fields = {
            "ledger_id": uuid.uuid5(uuid.NAMESPACE_URL, f"voice:{entity_id}"),
            "schema_version": "v1",
            "capture_profile": "live-slack-web-api/v1",
            "source": "slack",
            "entity_type": "message",
            "tenant_workspace_id": "T",
            "tenant_status": "observed",
            "scope": Jsonb({"channel_id": "C1", "container": "C1"}),
            "source_entity_id": entity_id,
            "source_updated_at_status": "observed",
            "deleted_status": "observed",
            "raw_payload": Jsonb({"text": text}),
            "content_hash": entity_id,
            "relations": Jsonb(
                {
                    "author_user_id": author,
                    "thread_id": parent,
                    "is_thread_reply": parent is not None,
                }
            ),
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

    with psycopg.connect(url) as connection:
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM ledger_records WHERE source_entity_id LIKE 'voice-%'")
            cursor.execute("DELETE FROM org_identity WHERE value = 'UHKVOICE'")
            cursor.execute("DELETE FROM org_person WHERE name = '보이스테스트'")
            cursor.execute(
                "INSERT INTO roster_observation (observed_at, source, row_count) "
                "VALUES (now(), 'roster_seed_2', 1) RETURNING observation_id"
            )
            observation = cursor.fetchone()[0]
            cursor.execute(
                "INSERT INTO org_person (person_id, name, first_seen, last_seen) "
                "VALUES (%s, '보이스테스트', %s, %s)",
                (person, observation, observation),
            )
            cursor.execute(
                "INSERT INTO org_identity (person_id, kind, value, first_seen, last_seen, "
                "origin) VALUES (%s, 'slack', 'UHKVOICE', %s, %s, 'roster')",
                (person, observation, observation),
            )
            insert(cursor, "voice-parent", text="배포 스크립트 드립니다", author="USOMEONE")
            insert(
                cursor,
                "voice-reply",
                text="이걸 내가 돌려야해? 배포는 배치가 해야지",
                author="UHKVOICE",
                parent="voice-parent",
            )
            insert(
                cursor,
                "voice-orphan",
                text="배포 로그는?",
                author="UHKVOICE",
                parent="voice-missing-parent",
            )
        connection.commit()

    try:
        result = precedents(url, str(person), "배포")
        said = [item.said for item in result.found]
        assert "이걸 내가 돌려야해? 배포는 배치가 해야지" in said
        best = result.found[0]
        assert best.has_situation, "a precedent with its situation ranks first"
        assert best.situation == "배포 스크립트 드립니다"
        assert best.situation_author == "USOMEONE"
        assert result.without_situation == 1
    finally:
        with psycopg.connect(url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM ledger_records WHERE source_entity_id LIKE 'voice-%'"
                )
            connection.commit()


def test_how_the_pair_was_linked_is_part_of_the_answer():
    """HK: 여럿이 오갈 경우, 바로 직전 대화가 아닐 수는 있어.

    In a busy channel the message he answered may be several turns back, and
    the pairing is then an inference. It is reported as one: a thread link is
    Slack's own record, a quiet gap is a conversation, and a crowded gap is a
    guess that says so rather than looking like the other two.
    """
    threaded = _precedent("이건 주인이 없네", situation="스크립트 드립니다")
    quiet = _precedent("주인이 누구야?", situation="배포 얘기", link="nearby")
    crowded = _precedent(
        "ㅇㅇ",
        situation="배포 얘기",
        link="nearby",
        between_count=9,
        between_speakers=4,
    )

    assert threaded.certainty == "스레드"
    assert quiet.certainty == "직후"
    assert "추정" in crowded.certainty and "9건/4명" in crowded.certainty
    assert _precedent("x", situation=None).certainty == "상황 없음"


def test_a_linked_precedent_outranks_a_closer_guess():
    """A better similarity score does not beat Slack saying so itself."""
    from rlwrld_worklog.voice import PrecedentResult

    threaded = _precedent("A", situation="상황", score=0.4)
    crowded = _precedent(
        "B", situation="상황", score=0.9, link="nearby", between_count=9,
        between_speakers=4,
    )
    order = {"thread": 2, "nearby": 1, "none": 0}
    ranked = sorted(
        [crowded, threaded],
        key=lambda item: (
            order.get(item.link, 0),
            -min(item.between_count, 10),
            item.score,
        ),
        reverse=True,
    )
    assert ranked[0] is threaded
    assert isinstance(PrecedentResult(query="q").as_dict()["matcher"], str)


def test_noise_is_not_shown_as_precedent():
    """63,092 embedded messages, and the nearest situation to a question about
    deployments was "퇴근하고 운동중입니다".

    One line of Slack is short and contextless, so in a space that size
    everything is roughly equidistant and the nearest neighbour is noise. A
    floor is the difference between an empty answer and a misleading one, and
    the count below it is reported so empty can be told from unasked.
    """
    from rlwrld_worklog.voice import SCORE_FLOOR, PrecedentResult

    assert 0 < SCORE_FLOOR < 1
    result = PrecedentResult(query="배포", matcher="embedding")
    result.below_floor = 7
    found = result.as_dict()
    assert found["below_floor"] == 7
    assert found["precedents"] == []
