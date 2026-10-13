"""An account nobody owns, shown with what the sources say about it.

HK, 2026-10-13: 주인없는 계정은 구체적으로 이메일, 이름, 닉네임, 슬랙 아이디,
노션 아이디, 슬럼 아이디, 깃헙 아이디 등을 구체적으로 알려주시면 좋겠어. 내가
그걸 매핑할거야.

The queue showed a bare handle and a count -- U07EKRU6F7H, 212 events --
which is a question nobody can answer. Run inside a rolled-back transaction.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

REQUIRES_DATABASE = pytest.mark.skipif(
    not os.environ.get("WORKLOG_TEST_DATABASE_URL"),
    reason="set WORKLOG_TEST_DATABASE_URL to a throwaway database",
)


def _ledger(cursor, *, source, entity_type, entity_id, payload, label=None):
    from psycopg.types.json import Jsonb

    now = datetime.now(timezone.utc)
    fields = {
        "ledger_id": uuid.uuid4(),
        "schema_version": "v1",
        "capture_profile": "test",
        "source": source,
        "entity_type": entity_type,
        "tenant_workspace_id": "T",
        "tenant_status": "observed",
        "scope": Jsonb({}),
        "source_entity_id": entity_id,
        "source_updated_at_status": "observed",
        "deleted_status": "observed",
        "raw_payload": Jsonb(payload),
        "content_hash": uuid.uuid4().hex,
        "relations": Jsonb({}),
        "source_file": "f",
        "source_file_sha256": "h",
        "record_pointer": "p",
        "legacy_layout_version": "v",
        "converter_version": "v",
        "observation_role": "current_head",
        "capture_completeness_status": "recorded",
        "source_created_at": now,
        "collected_at": now,
        "denormalized_label_snapshot": Jsonb(label or {}),
    }
    cursor.execute(
        f"INSERT INTO ledger_records ({','.join(fields)}) VALUES ({','.join('%s' for _ in fields)})",
        list(fields.values()),
    )


@pytest.fixture()
def cursor():
    import psycopg

    from rlwrld_worklog.ledger.load import apply_migrations

    url = os.environ["WORKLOG_TEST_DATABASE_URL"]
    apply_migrations(
        database_url=url,
        migrations_dir=Path(__file__).resolve().parents[1] / "sql" / "migrations",
        dry_run=False,
    )
    connection = psycopg.connect(url)
    connection.autocommit = False
    try:
        yield connection.cursor()
    finally:
        connection.rollback()
        connection.close()


@REQUIRES_DATABASE
def test_a_slack_account_shows_its_name_nickname_email_and_channel(cursor) -> None:
    from psycopg.types.json import Jsonb

    from rlwrld_worklog.org.unmapped import account_details

    user = f"UTEST{uuid.uuid4().hex[:6].upper()}"
    channel = f"CTEST{uuid.uuid4().hex[:6].upper()}"
    _ledger(
        cursor,
        source="slack",
        entity_type="user",
        entity_id=f"T:{user}",
        payload={
            "name": "minji.k",
            "profile": {"real_name": "김민지", "display_name": "민지", "email": "minji@lab.kr"},
        },
    )
    _ledger(
        cursor,
        source="slack",
        entity_type="conversation",
        entity_id=f"T:{channel}",
        payload={},
        label={"name": "model-team"},
    )
    for _ in range(3):
        cursor.execute(
            "INSERT INTO timeline_events (event_id, source, event_type, external_id, "
            "actor_external_id, occurred_at, ingested_at, container_id, payload) "
            "VALUES (%s,'slack','message',%s,%s,now(),now(),%s,%s)",
            (str(uuid.uuid4()), uuid.uuid4().hex, user, channel, Jsonb({})),
        )

    found = account_details(cursor, "slack", user)
    assert found["name"] == "김민지"
    assert found["nickname"] == "민지"
    assert found["email"] == "minji@lab.kr"
    assert found["username"] == "minji.k"
    assert found["places"] == [{"where": "#model-team", "events": 3}], "a channel by its name"


@REQUIRES_DATABASE
def test_an_account_with_no_profile_still_says_where_it_worked(cursor) -> None:
    """GitHub and Slurm keep no profile; the repository is the hint."""
    from psycopg.types.json import Jsonb

    from rlwrld_worklog.org.unmapped import account_details

    login = f"ghost-{uuid.uuid4().hex[:6]}"
    cursor.execute(
        "INSERT INTO timeline_events (event_id, source, event_type, external_id, "
        "actor_external_id, occurred_at, ingested_at, container_id, payload) "
        "VALUES (%s,'github','push',%s,%s,now(),now(),'RLWRLD/vla',%s)",
        (str(uuid.uuid4()), uuid.uuid4().hex, login, Jsonb({})),
    )
    found = account_details(cursor, "github", login)
    assert found["handle"] == login
    assert found["places"] == [{"where": "RLWRLD/vla", "events": 1}]
    assert "name" not in found, "nothing is invented where the source said nothing"


def test_the_queue_shows_the_details_in_columns() -> None:
    script = (
        Path(__file__).resolve().parents[1] / "src" / "rlwrld_worklog" / "static" / "admin.js"
    ).read_text(encoding="utf-8")
    html = (
        Path(__file__).resolve().parents[1] / "src" / "rlwrld_worklog" / "static" / "admin.html"
    ).read_text(encoding="utf-8")
    assert "account.details" in script
    for heading in ("이름 · 닉네임", "이메일", "주로 활동한 곳"):
        assert heading in html


def _roster_person(cursor, name, *, nickname=None, status="active", identities=()):
    cursor.execute(
        "INSERT INTO roster_observation (observed_at, source, row_count) "
        "VALUES (now(), 'roster_seed_2', 1) RETURNING observation_id"
    )
    observation = cursor.fetchone()[0]
    person_id = f"p_c_{uuid.uuid4().hex[:10]}"
    cursor.execute(
        "INSERT INTO org_person (person_id, name, first_seen, last_seen) VALUES (%s,%s,%s,%s)",
        (person_id, name, observation, observation),
    )
    cursor.execute(
        "INSERT INTO org_person_state (observation_id, person_id, affiliation, access_level, "
        "status, nickname, department_raw) VALUES (%s,%s,'internal','staff_equivalent',%s,%s,'RLWRLD')",
        (observation, person_id, status, nickname),
    )
    for kind, value in identities:
        cursor.execute(
            "INSERT INTO org_identity (person_id, kind, value, first_seen, last_seen) "
            "VALUES (%s,%s,%s,%s,%s)",
            (person_id, kind, value, observation, observation),
        )
    return person_id


@REQUIRES_DATABASE
def test_a_second_account_is_offered_to_the_person_its_email_names(cursor) -> None:
    """HK, 2026-10-13: 학생/교수님들은 슬랙/노션 계정이 2개 일 수 있어.

    A lab Slack whose profile email is one the roster already holds for a
    person is that person's second account -- offered, strongest first.
    """
    from rlwrld_worklog.org.unmapped import owner_candidates

    tag = uuid.uuid4().hex[:6]
    owner = _roster_person(
        cursor, f"학생{tag}", identities=[("email_school", f"s{tag}@kaist.ac.kr")]
    )
    found = owner_candidates(cursor, {"email": f"S{tag}@KAIST.ac.kr", "name": "다른이름"})
    assert [item["person_id"] for item in found] == [owner], "the email decides, case aside"
    assert found[0]["why"].startswith("이메일 일치")


@REQUIRES_DATABASE
def test_a_name_match_lists_every_namesake_and_says_who_left(cursor) -> None:
    """동명이인 is listed, not resolved, and a departed person still counts --
    "리얼월드 퇴사자 계정" is one of the cases HK named."""
    from rlwrld_worklog.org.unmapped import owner_candidates

    tag = uuid.uuid4().hex[:6]
    name = f"동명{tag}"
    here = _roster_person(cursor, name)
    left = _roster_person(cursor, name, status="retired")
    found = owner_candidates(cursor, {"name": name})
    assert {item["person_id"] for item in found} == {here, left}
    assert {item["person_id"]: item["status"] for item in found}[left] == "retired"
    assert all(item["why"] == "이름 일치" for item in found)


@REQUIRES_DATABASE
def test_a_nickname_finds_the_person_too(cursor) -> None:
    """Slack display names are usually the nickname the roster also keeps."""
    from rlwrld_worklog.org.unmapped import owner_candidates

    tag = uuid.uuid4().hex[:6]
    person = _roster_person(cursor, f"정식이름{tag}", nickname=f"nick{tag}")
    found = owner_candidates(cursor, {"nickname": f"NICK{tag}"})
    assert [item["person_id"] for item in found] == [person]


@REQUIRES_DATABASE
def test_an_account_with_no_profile_offers_nobody(cursor) -> None:
    from rlwrld_worklog.org.unmapped import owner_candidates

    assert owner_candidates(cursor, {"handle": "ghost"}) == []


@REQUIRES_DATABASE
def test_bulk_answers_only_the_unambiguous_ones() -> None:
    """HK, 2026-10-13: 퇴사자는 조직도에서 매핑되고, 봇은 조직도 하단에 봇계정으로.

    One email match to one person -- including somebody who has left -- is
    linked; an account its source calls a bot is filed as a bot; a name
    match is left for a person to click, because it can be 동명이인.
    """
    import psycopg

    from rlwrld_worklog.org.unmapped import BOT_NOTE, resolve_in_bulk

    url = os.environ["WORKLOG_TEST_DATABASE_URL"]
    tag = uuid.uuid4().hex[:6].upper()
    leaver_email = f"left{tag}@rlwrld.ai".lower()
    accounts = {
        "email": f"UE{tag}",
        "bot": f"UB{tag}",
        "name": f"UN{tag}",
    }
    with psycopg.connect(url) as connection, connection.cursor() as cursor:
        leaver = _roster_person(
            cursor, f"퇴사자{tag}", status="retired", identities=[("email_official", leaver_email)]
        )
        _roster_person(cursor, f"동명{tag}")
        for key, handle in accounts.items():
            profile = {
                "email": {"real_name": "아무개", "email": leaver_email.upper()},
                "bot": {"real_name": "deploy-bot"},
                "name": {"real_name": f"동명{tag}"},
            }[key]
            _ledger(
                cursor,
                source="slack",
                entity_type="user",
                entity_id=f"T:{handle}",
                payload={"name": handle.lower(), "is_bot": key == "bot", "profile": profile},
            )
            cursor.execute(
                "INSERT INTO org_unmapped_account (kind, value, first_seen_at, last_seen_at, events) "
                "VALUES ('slack', %s, now(), now(), 5)",
                (handle,),
            )
        connection.commit()
    try:
        linked = resolve_in_bulk(url, what="email")
        bots = resolve_in_bulk(url, what="bots")
        with psycopg.connect(url) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT value, state, resolved_person_id, note FROM org_unmapped_account "
                "WHERE value = ANY(%s)",
                (list(accounts.values()),),
            )
            state = {row[0]: row[1:] for row in cursor.fetchall()}
        assert state[accounts["email"]][0] == "resolved"
        assert state[accounts["email"]][1] == leaver, "a leaver's old account lands on them"
        assert state[accounts["bot"]][0] == "ignored" and state[accounts["bot"]][2] == BOT_NOTE
        assert state[accounts["name"]][0] == "open", "a name match waits for a click"
        assert any(item["value"] == accounts["email"] for item in linked["accounts"])
        assert any(item["value"] == accounts["bot"] for item in bots["accounts"])
    finally:
        with psycopg.connect(url) as connection, connection.cursor() as cursor:
            cursor.execute("DELETE FROM org_identity WHERE value = ANY(%s)", (list(accounts.values()),))
            cursor.execute(
                "DELETE FROM org_unmapped_account WHERE value = ANY(%s)", (list(accounts.values()),)
            )
            cursor.execute(
                "DELETE FROM ledger_records WHERE source_entity_id = ANY(%s)",
                ([f"T:{handle}" for handle in accounts.values()],),
            )
            connection.commit()


@REQUIRES_DATABASE
def test_a_notion_bot_names_who_made_it_and_offers_that_person(cursor) -> None:
    """HK, 2026-10-13: 봇은 봇을 만든 사람이 있을 것 같고 ... 내가 보고 판정하기 쉽게."""
    from rlwrld_worklog.org.unmapped import account_details, owner_candidates

    tag = uuid.uuid4().hex[:8]
    maker_id, bot_id = f"maker-{tag}", f"bot-{tag}"
    _ledger(
        cursor,
        source="notion",
        entity_type="user",
        entity_id=f"W:{maker_id}",
        payload={"type": "person", "name": f"만든이{tag}", "person": {"email": f"m{tag}@x.ai"}},
    )
    _ledger(
        cursor,
        source="notion",
        entity_type="user",
        entity_id=f"W:{bot_id}",
        payload={"type": "bot", "name": "sync bot", "bot": {"owner": {"type": "user", "user": {"id": maker_id}}}},
    )
    maker = _roster_person(cursor, f"만든이{tag}", identities=[("notion", maker_id)])

    found = account_details(cursor, "notion", bot_id)
    assert found["bot"] is True
    assert found["owner_name"] == f"만든이{tag}", "the maker's name, looked up by id"
    assert found["owner_email"] == f"m{tag}@x.ai"
    offered = owner_candidates(cursor, found)
    assert [(item["person_id"], item["why"]) for item in offered] == [(maker, "봇을 만든 사람")]
