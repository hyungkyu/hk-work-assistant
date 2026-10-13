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
