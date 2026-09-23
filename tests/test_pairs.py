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


def test_a_missing_table_says_which_command_fixes_it(monkeypatch):
    """Three commands answered with tracebacks on 2026-09-21.

    The tables come from a migration, and the migration is a separate step I
    left out of the instructions. A traceback says what broke; it does not say
    what to do, and that difference costs a round trip.
    """
    import pytest as _pytest

    from rlwrld_worklog import blocks

    class UndefinedTable(Exception):
        pass

    def boom():
        with blocks._needs_migration():
            raise UndefinedTable('relation "answer_pairs" does not exist')

    with _pytest.raises(SystemExit) as failure:
        boom()
    assert "ledger-migrate --apply" in str(failure.value)

    # Anything else is not swallowed or relabelled.
    def other():
        with blocks._needs_migration():
            raise ValueError("something else")

    with _pytest.raises(ValueError):
        other()


@REQUIRES_DATABASE
def test_a_rebuild_leaves_exactly_one_proposal_per_answer():
    """Answers in his queue with nothing marked 제안 (2026-09-22).

    Candidates were only ever inserted or updated, never removed. So a
    candidate the current rules no longer offer kept the rank it had on an
    earlier run -- and when that stale row was the one carrying `proposed`,
    the answer was left with a proposal that is not among its candidates, or
    with none at all. Reviewing an answer whose proposal is missing is
    reviewing from scratch, which is the work this was supposed to remove.

    Mutation this catches: drop the clear-before-write and the second run
    leaves two proposals and a candidate that no longer exists.
    """
    import uuid

    import psycopg
    from psycopg.types.json import Jsonb

    from rlwrld_worklog.blocks import pair_queue, propose_pairs
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

    def write(entity, author, minute, text):
        fields = {
            "ledger_id": uuid.uuid5(uuid.NAMESPACE_URL, f"stale:{entity}"),
            "schema_version": "v1",
            "capture_profile": "p",
            "source": "slack",
            "entity_type": "message",
            "tenant_workspace_id": "T",
            "tenant_status": "observed",
            "scope": Jsonb({"channel_id": "CSTALE"}),
            "source_entity_id": f"T:CSTALE:{entity}",
            "source_updated_at_status": "observed",
            "deleted_status": "observed",
            "raw_payload": Jsonb({"text": text}),
            "content_hash": entity,
            "relations": Jsonb({"author_user_id": author}),
            "source_file": "staletest",
            "source_file_sha256": "h",
            "record_pointer": "p",
            "legacy_layout_version": "v",
            "converter_version": "v",
            "observation_role": "current_head",
            "capture_completeness_status": "recorded",
            "source_created_at": START + timedelta(minutes=minute),
            "collected_at": START,
        }
        return (
            f"INSERT INTO ledger_records ({','.join(fields)}) "
            f"VALUES ({','.join('%s' for _ in fields)})",
            list(fields.values()),
        )

    with psycopg.connect(url) as connection:
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM answer_pairs WHERE channel = 'CSTALE'")
            cursor.execute("DELETE FROM ledger_records WHERE source_file = 'staletest'")
            cursor.execute("DELETE FROM org_identity WHERE value = 'USTALE'")
            cursor.execute("DELETE FROM org_person WHERE name = '잔재테스트'")
            cursor.execute(
                "INSERT INTO roster_observation (observed_at, source, row_count) "
                "VALUES (now(), 'roster_seed_2', 1) RETURNING observation_id"
            )
            observation = cursor.fetchone()[0]
            cursor.execute(
                "INSERT INTO org_person (person_id, name, first_seen, last_seen) "
                "VALUES (%s, '잔재테스트', %s, %s)",
                (person, observation, observation),
            )
            cursor.execute(
                "INSERT INTO org_identity (person_id, kind, value, first_seen, "
                "last_seen, origin) VALUES (%s, 'slack', 'USTALE', %s, %s, 'roster')",
                (person, observation, observation),
            )
            cursor.execute(*write("far", "UTHEM", 0, "예전 질문입니다"))
            cursor.execute(*write("mine", "USTALE", 5, "그건 다음 주에 보죠"))
        connection.commit()

    names = {"USTALE": "HK", "UTHEM": "상대"}
    try:
        propose_pairs(url, str(person), names=names, apply=True)

        before = pair_queue(url, str(person), limit=5)["answers"][0]
        assert any("예전 질문" in item["text"] for item in before["candidates"])

        # The candidate goes away -- a message deleted at the source, a
        # collection rule that stops including it, a narrower window. An
        # upsert alone cannot express "this is no longer a candidate", so
        # without a clear the row simply stays, carrying whatever rank and
        # proposal it last had.
        with psycopg.connect(url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM ledger_records "
                    " WHERE source_file = 'staletest' AND source_entity_id LIKE '%%far'"
                )
                cursor.execute(*write("near", "UTHEM", 4, "이거 어떻게 할까요"))
            connection.commit()

        propose_pairs(url, str(person), names=names, apply=True)

        found = pair_queue(url, str(person), limit=5)
        answers = found["answers"]
        assert len(answers) == 1
        candidates = answers[0]["candidates"]
        assert not any("예전 질문" in item["text"] for item in candidates), (
            "a candidate the rules no longer offer is gone, not left behind "
            f"with a stale rank: {candidates}"
        )
        proposed = [item for item in candidates if item["proposed"]]
        assert len(proposed) == 1, (
            "exactly one candidate is the proposal, on every rebuild: "
            f"{candidates}"
        )
        assert proposed[0]["rank"] == 0, "the proposal is the top-ranked one"
        assert "이거 어떻게 할까요" in proposed[0]["text"], "the nearer message wins"
    finally:
        with psycopg.connect(url) as connection:
            with connection.cursor() as cursor:
                cursor.execute("DELETE FROM answer_pairs WHERE channel = 'CSTALE'")
                cursor.execute(
                    "DELETE FROM ledger_records WHERE source_file = 'staletest'"
                )
            connection.commit()


@REQUIRES_DATABASE
def test_a_decision_survives_a_rebuild():
    """The one row a rebuild must never clear.

    Clearing an answer's candidates is what keeps the proposal honest, and it
    is also the fastest way to throw away the only thing here that cost a
    person's attention. So the delete is filtered, and this says so.
    """
    import uuid

    import psycopg
    from psycopg.types.json import Jsonb

    from rlwrld_worklog.blocks import choose_pair, pair_queue, propose_pairs
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

    def write(entity, author, minute, text):
        fields = {
            "ledger_id": uuid.uuid5(uuid.NAMESPACE_URL, f"keep:{entity}"),
            "schema_version": "v1",
            "capture_profile": "p",
            "source": "slack",
            "entity_type": "message",
            "tenant_workspace_id": "T",
            "tenant_status": "observed",
            "scope": Jsonb({"channel_id": "CKEEP"}),
            "source_entity_id": f"T:CKEEP:{entity}",
            "source_updated_at_status": "observed",
            "deleted_status": "observed",
            "raw_payload": Jsonb({"text": text}),
            "content_hash": f"keep-{entity}",
            "relations": Jsonb({"author_user_id": author}),
            "source_file": "keeptest",
            "source_file_sha256": "h",
            "record_pointer": "p",
            "legacy_layout_version": "v",
            "converter_version": "v",
            "observation_role": "current_head",
            "capture_completeness_status": "recorded",
            "source_created_at": START + timedelta(minutes=minute),
            "collected_at": START,
        }
        return (
            f"INSERT INTO ledger_records ({','.join(fields)}) "
            f"VALUES ({','.join('%s' for _ in fields)})",
            list(fields.values()),
        )

    with psycopg.connect(url) as connection:
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM answer_pairs WHERE channel = 'CKEEP'")
            cursor.execute("DELETE FROM ledger_records WHERE source_file = 'keeptest'")
            cursor.execute("DELETE FROM org_identity WHERE value = 'UKEEP'")
            cursor.execute("DELETE FROM org_person WHERE name = '결정테스트'")
            cursor.execute(
                "INSERT INTO roster_observation (observed_at, source, row_count) "
                "VALUES (now(), 'roster_seed_2', 1) RETURNING observation_id"
            )
            observation = cursor.fetchone()[0]
            cursor.execute(
                "INSERT INTO org_person (person_id, name, first_seen, last_seen) "
                "VALUES (%s, '결정테스트', %s, %s)",
                (person, observation, observation),
            )
            cursor.execute(
                "INSERT INTO org_identity (person_id, kind, value, first_seen, "
                "last_seen, origin) VALUES (%s, 'slack', 'UKEEP', %s, %s, 'roster')",
                (person, observation, observation),
            )
            cursor.execute(*write("a", "UTHEM", 0, "먼저 한 질문입니다"))
            cursor.execute(*write("b", "UTHEM", 4, "나중에 한 질문입니다"))
            cursor.execute(*write("mine", "UKEEP", 5, "두 번째 건으로 가시죠"))
        connection.commit()

    names = {"UKEEP": "HK", "UTHEM": "상대"}
    try:
        propose_pairs(url, str(person), names=names, apply=True)
        queue = pair_queue(url, str(person), limit=5)
        answer = queue["answers"][0]
        # A correction, not an agreement: the case that matters.
        pick = next(
            (item for item in answer["candidates"] if not item["proposed"]),
            answer["candidates"][0],
        )
        choose_pair(url, answer["answer_ledger_id"], pick["pair_id"], actor="hk")

        propose_pairs(url, str(person), names=names, apply=True)

        after = pair_queue(url, str(person), state="all", limit=5)
        chosen = [
            item
            for row in after["answers"]
            for item in row["candidates"]
            if item["chosen"]
        ]
        assert len(chosen) == 1, "his decision is still there after a rebuild"
        assert chosen[0]["pair_id"] == pick["pair_id"]
    finally:
        with psycopg.connect(url) as connection:
            with connection.cursor() as cursor:
                cursor.execute("DELETE FROM answer_pairs WHERE channel = 'CKEEP'")
                cursor.execute(
                    "DELETE FROM ledger_records WHERE source_file = 'keeptest'"
                )
            connection.commit()


@REQUIRES_DATABASE
def test_a_message_collected_twice_is_one_candidate():
    """690 of his answers had no proposal at all (2026-09-22).

    `pairs --audit` showed them as "candidates: 1, top_rank: 1" -- a rank that
    can only exist if a rank-0 row was written and then overwritten. The
    nearby-messages query was the only one in this module that did not reduce
    the ledger's several observations of a message to one, so a re-collected
    message filled every candidate slot; since a candidate's id is derived
    from the message it points at, the duplicates collapsed on write and the
    proposal was overwritten by the copy ranked behind it.

    The audit count is what turned "I think it is staleness" -- which was
    wrong, and whose test passed with the fix reverted -- into this. A number
    that says which shape the wrongness has is worth more than an explanation.

    Mutation caught: remove the DISTINCT ON and this answer comes back with
    one candidate, ranked 1, proposed by nothing.
    """
    import uuid

    import psycopg
    from psycopg.types.json import Jsonb

    from rlwrld_worklog.blocks import pair_queue, propose_pairs
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

    def write(cursor, entity, author, minute, text, *, observation):
        fields = {
            "ledger_id": uuid.uuid5(uuid.NAMESPACE_URL, f"dup:{entity}:{observation}"),
            "schema_version": "v1",
            "capture_profile": "p",
            "source": "slack",
            "entity_type": "message",
            "tenant_workspace_id": "T",
            "tenant_status": "observed",
            "scope": Jsonb({"channel_id": "CDUP"}),
            "source_entity_id": f"T:CDUP:{entity}",
            "source_updated_at_status": "observed",
            "deleted_status": "observed",
            "raw_payload": Jsonb({"text": text}),
            "content_hash": f"dup-{entity}-{observation}",
            "relations": Jsonb({"author_user_id": author}),
            "source_file": "duptest",
            "source_file_sha256": "h",
            "record_pointer": "p",
            "legacy_layout_version": "v",
            "converter_version": "v",
            "observation_role": "current_head",
            "capture_completeness_status": "recorded",
            "source_created_at": START + timedelta(minutes=minute),
            # The thing that makes them different rows and the same message.
            "collected_at": START + timedelta(hours=observation),
        }
        cursor.execute(
            f"INSERT INTO ledger_records ({','.join(fields)}) "
            f"VALUES ({','.join('%s' for _ in fields)})",
            list(fields.values()),
        )

    with psycopg.connect(url) as connection:
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM answer_pairs WHERE channel = 'CDUP'")
            cursor.execute("DELETE FROM ledger_records WHERE source_file = 'duptest'")
            cursor.execute("DELETE FROM org_identity WHERE value = 'UDUP'")
            cursor.execute("DELETE FROM org_person WHERE name = '관측중복테스트'")
            cursor.execute(
                "INSERT INTO roster_observation (observed_at, source, row_count) "
                "VALUES (now(), 'roster_seed_2', 1) RETURNING observation_id"
            )
            observation = cursor.fetchone()[0]
            cursor.execute(
                "INSERT INTO org_person (person_id, name, first_seen, last_seen) "
                "VALUES (%s, '관측중복테스트', %s, %s)",
                (person, observation, observation),
            )
            cursor.execute(
                "INSERT INTO org_identity (person_id, kind, value, first_seen, "
                "last_seen, origin) VALUES (%s, 'slack', 'UDUP', %s, %s, 'roster')",
                (person, observation, observation),
            )
            # The same question, collected three times -- which is what a DM
            # that keeps being re-read looks like in the ledger.
            for attempt in (1, 2, 3):
                write(
                    cursor,
                    "q",
                    "UTHEM",
                    0,
                    "혹시 오늘 4시 30분 1on1 진행하는 것 맞을까요?",
                    observation=attempt,
                )
            write(cursor, "mine", "UDUP", 2, "지금 갑니다.", observation=1)
        connection.commit()

    try:
        result = propose_pairs(
            url, str(person), names={"UDUP": "HK", "UTHEM": "박승철"}, apply=True
        )
        assert result.answers == 1

        answer = pair_queue(url, str(person), limit=5)["answers"][0]
        candidates = answer["candidates"]
        assert len(candidates) == 1, (
            "three observations of one message are one candidate, not three: "
            f"{candidates}"
        )
        assert candidates[0]["rank"] == 0
        assert candidates[0]["proposed"], (
            "an answer in the review queue always carries a proposal; "
            "without one there is nothing to correct and the review starts "
            "from scratch"
        )
    finally:
        with psycopg.connect(url) as connection:
            with connection.cursor() as cursor:
                cursor.execute("DELETE FROM answer_pairs WHERE channel = 'CDUP'")
                cursor.execute(
                    "DELETE FROM ledger_records WHERE source_file = 'duptest'"
                )
            connection.commit()


@REQUIRES_DATABASE
def test_one_message_reached_by_two_routes_is_one_candidate():
    """The 66 that survived the previous fix.

    Deduplicating the nearby query took answers with no proposal from 690 to
    66. The rest had the same shape -- one surviving candidate carrying a rank
    of 2 -- from the other direction: the message he replied to in a thread is
    also, often, the message just before his reply, so the thread route and
    the window route both offered it. Two entries, one message, one row: the
    second overwrote the first and took the proposal with it.

    The rule now lives in one place, before ranking, and keeps the stronger
    claim: Slack's own thread link beats "this was nearby".

    Mutation caught: drop the collapse in `add` and this answer comes back
    with one candidate ranked 1 and no proposal.
    """
    import uuid

    import psycopg
    from psycopg.types.json import Jsonb

    from rlwrld_worklog.blocks import pair_queue, propose_pairs
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

    def write(cursor, entity, author, minute, text, parent=None):
        fields = {
            "ledger_id": uuid.uuid5(uuid.NAMESPACE_URL, f"both:{entity}"),
            "schema_version": "v1",
            "capture_profile": "p",
            "source": "slack",
            "entity_type": "message",
            "tenant_workspace_id": "T",
            "tenant_status": "observed",
            "scope": Jsonb({"channel_id": "CBOTH"}),
            "source_entity_id": f"T:CBOTH:{entity}",
            "source_updated_at_status": "observed",
            "deleted_status": "observed",
            "raw_payload": Jsonb({"text": text}),
            "content_hash": f"both-{entity}",
            "relations": Jsonb({"author_user_id": author, "thread_id": parent}),
            "source_file": "bothtest",
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

    with psycopg.connect(url) as connection:
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM answer_pairs WHERE channel = 'CBOTH'")
            cursor.execute("DELETE FROM ledger_records WHERE source_file = 'bothtest'")
            cursor.execute("DELETE FROM org_identity WHERE value = 'UBOTH'")
            cursor.execute(
                "INSERT INTO roster_observation (observed_at, source, row_count) "
                "VALUES (now(), 'roster_seed_2', 1) RETURNING observation_id"
            )
            observation = cursor.fetchone()[0]
            cursor.execute(
                "INSERT INTO org_person (person_id, name, first_seen, last_seen) "
                "VALUES (%s, '두경로테스트', %s, %s)",
                (person, observation, observation),
            )
            cursor.execute(
                "INSERT INTO org_identity (person_id, kind, value, first_seen, "
                "last_seen, origin) VALUES (%s, 'slack', 'UBOTH', %s, %s, 'roster')",
                (person, observation, observation),
            )
            # He replies in a thread to the message right before his own --
            # the ordinary case, where both routes point at the same message.
            write(cursor, "q", "UTHEM", 0, "이 건 어떻게 갈까요?")
            write(cursor, "mine", "UBOTH", 1, "롤백 경로부터 잡죠.", parent="q")
        connection.commit()

    try:
        propose_pairs(
            url, str(person), names={"UBOTH": "HK", "UTHEM": "상대"}, apply=True
        )
        answer = pair_queue(url, str(person), limit=5)["answers"][0]
        candidates = answer["candidates"]

        assert len(candidates) == 1, (
            "one message, however many routes found it: " f"{candidates}"
        )
        assert candidates[0]["basis"] == "thread", (
            "the thread link is the stronger claim about what he answered"
        )
        assert candidates[0]["rank"] == 0
        assert candidates[0]["proposed"]
    finally:
        with psycopg.connect(url) as connection:
            with connection.cursor() as cursor:
                cursor.execute("DELETE FROM answer_pairs WHERE channel = 'CBOTH'")
                cursor.execute(
                    "DELETE FROM ledger_records WHERE source_file = 'bothtest'"
                )
            connection.commit()
