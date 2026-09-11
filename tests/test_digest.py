"""Per-person daily digests: every activity, in time order, mechanically.

The round trip runs against `WORKLOG_TEST_DATABASE_URL` like the other
database suites and is skipped without it. What it verifies is the contract
HK set on 2026-09-11: all of it, not a selection, and nothing written by a
model.
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rlwrld_worklog.digest import (  # noqa: E402
    KST,
    build_day,
    digest_status,
    kst_day_bounds,
    read_digest,
)
from rlwrld_worklog.render import render_org_chart, render_person_day  # noqa: E402

REQUIRES_DATABASE = pytest.mark.skipif(
    not os.environ.get("WORKLOG_TEST_DATABASE_URL"),
    reason="set WORKLOG_TEST_DATABASE_URL to a throwaway database to run the digest round trip",
)

PERSON = "p_digesttest01"
DAY = date(2026, 9, 10)


def test_a_kst_day_starts_at_midnight_seoul_not_utc() -> None:
    """The 15-hour window defect this system already had once, in another place."""
    start, end = kst_day_bounds(DAY)
    assert start.isoformat() == "2026-09-10T00:00:00+09:00"
    assert end.isoformat() == "2026-09-11T00:00:00+09:00"
    assert start.tzinfo == KST


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
    _seed_person(url)
    yield url
    _clean(url)


def _clean(url: str) -> None:
    import psycopg

    with psycopg.connect(url) as connection:
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM person_day_digest WHERE person_id = %s", (PERSON,))
            cursor.execute("DELETE FROM org_identity WHERE person_id = %s", (PERSON,))
            cursor.execute("DELETE FROM org_person_state WHERE person_id = %s", (PERSON,))
            cursor.execute("DELETE FROM org_person WHERE person_id = %s", (PERSON,))
            cursor.execute("DELETE FROM timeline_events WHERE external_id LIKE 'digest-test:%'")
            cursor.execute(
                "DELETE FROM roster_observation WHERE source_digest = 'digest-test'"
            )
        connection.commit()


def _seed_person(url: str) -> None:
    import psycopg

    with psycopg.connect(url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO roster_observation (observed_at, source, source_digest, row_count)"
                " VALUES (now(), 'roster_seed_2', 'digest-test', 1) RETURNING observation_id"
            )
            observation = cursor.fetchone()[0]
            cursor.execute(
                "INSERT INTO org_person (person_id, name, first_seen, last_seen)"
                " VALUES (%s, %s, %s, %s)",
                (PERSON, "테스터", observation, observation),
            )
            cursor.execute(
                """
                INSERT INTO org_person_state (
                    observation_id, person_id, nickname, title, employment_type,
                    affiliation, access_level, status, department_raw
                ) VALUES (%s, %s, 'tester', '연구원', '정규직', 'internal',
                          'staff_equivalent', 'active', 'RLWRLD | Model Team')
                """,
                (observation, PERSON),
            )
            for kind, value in (("github", "tester"), ("slurm", "tester01")):
                cursor.execute(
                    "INSERT INTO org_identity (kind, value, person_id, first_seen, last_seen)"
                    " VALUES (%s, %s, %s, %s, %s)",
                    (kind, value, PERSON, observation, observation),
                )
        connection.commit()


def _event(url, *, at: str, source: str, event_type: str, actor: str, title=None, external=None):
    import psycopg
    from psycopg.types.json import Jsonb

    with psycopg.connect(url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO timeline_events (
                    event_id, source, event_type, external_id, actor_external_id,
                    occurred_at, ingested_at, container_id, payload
                ) VALUES (%s, %s, %s, %s, %s, %s, now(), 'container', %s)
                """,
                (
                    str(uuid.uuid4()),
                    source,
                    event_type,
                    external or f"digest-test:{uuid.uuid4()}",
                    actor,
                    at,
                    Jsonb({"labels": {"title": title} if title else {}}),
                ),
            )
        connection.commit()


@REQUIRES_DATABASE
def test_a_day_holds_every_activity_in_time_order(database) -> None:
    """Not a selection: 주요가 아니라 모든 것 (HK, 2026-09-11)."""
    _event(database, at="2026-09-10T09:00:00+09:00", source="github",
           event_type="github_commit", actor="tester", title="첫 커밋")
    _event(database, at="2026-09-10T17:30:00+09:00", source="slurm",
           event_type="slurm_job", actor="tester01", title="야간 학습")
    _event(database, at="2026-09-10T13:00:00+09:00", source="github",
           event_type="github_pull_request", actor="tester", title="PR")

    result = build_day(database, DAY, dry_run=False)
    assert result.events == 3

    found = read_digest(database, PERSON, DAY)
    assert [event["time"] for event in found["events"]] == ["09:00", "13:00", "17:30"]
    assert found["events_total"] == 3
    assert found["counts"]["by_source"] == {"github": 2, "slurm": 1}


@REQUIRES_DATABASE
def test_activity_outside_the_kst_day_is_not_in_it(database) -> None:
    _event(database, at="2026-09-09T23:59:00+09:00", source="github",
           event_type="github_commit", actor="tester", title="전날")
    _event(database, at="2026-09-11T00:01:00+09:00", source="github",
           event_type="github_commit", actor="tester", title="다음날")
    result = build_day(database, DAY, dry_run=False)
    assert result.events == 0
    assert read_digest(database, PERSON, DAY) is None


@REQUIRES_DATABASE
def test_an_event_with_no_title_says_so_rather_than_being_given_one(database) -> None:
    """No model in this path, so an absent title stays absent."""
    _event(database, at="2026-09-10T10:00:00+09:00", source="slack",
           event_type="message", actor="tester")
    build_day(database, DAY, dry_run=False)
    found = read_digest(database, PERSON, DAY)
    assert found["events"][0]["title"] is None


@REQUIRES_DATABASE
def test_activity_by_an_account_nobody_owns_is_counted_not_attributed(database) -> None:
    """The size of what the org chart cannot account for, reported beside it."""
    _event(database, at="2026-09-10T11:00:00+09:00", source="slurm",
           event_type="slurm_job", actor="nobody-owns-this", title="고아 잡")
    result = build_day(database, DAY, dry_run=False)
    assert result.unattributed_events >= 1
    assert result.events == 0


@REQUIRES_DATABASE
def test_rebuilding_a_day_replaces_it_rather_than_duplicating(database) -> None:
    _event(database, at="2026-09-10T09:00:00+09:00", source="github",
           event_type="github_commit", actor="tester", title="하나")
    build_day(database, DAY, dry_run=False)
    _event(database, at="2026-09-10T10:00:00+09:00", source="github",
           event_type="github_commit", actor="tester", title="둘")
    build_day(database, DAY, dry_run=False)
    found = read_digest(database, PERSON, DAY)
    assert found["events_total"] == 2


@REQUIRES_DATABASE
def test_a_dry_run_writes_nothing(database) -> None:
    _event(database, at="2026-09-10T09:00:00+09:00", source="github",
           event_type="github_commit", actor="tester", title="x")
    result = build_day(database, DAY, dry_run=True)
    assert result.events == 1
    assert read_digest(database, PERSON, DAY) is None


@REQUIRES_DATABASE
def test_status_reports_which_days_exist(database) -> None:
    _event(database, at="2026-09-10T09:00:00+09:00", source="github",
           event_type="github_commit", actor="tester", title="x")
    build_day(database, DAY, dry_run=False)
    status = digest_status(database)
    assert status["last_day"] >= DAY.isoformat()
    assert status["rows"] >= 1


@REQUIRES_DATABASE
def test_the_rendered_page_lists_every_event_and_names_its_generator(database) -> None:
    for hour in range(9, 15):
        _event(database, at=f"2026-09-10T{hour:02d}:00:00+09:00", source="github",
               event_type="github_commit", actor="tester", title=f"커밋 {hour}")
    build_day(database, DAY, dry_run=False)
    page = render_person_day(read_digest(database, PERSON, DAY))
    for hour in range(9, 15):
        assert f"커밋 {hour}" in page
    assert "digest/1" in page
    assert "전부" in page


# ------------------------------------------------------------- rendering


def test_an_empty_chart_renders_its_reason_rather_than_an_empty_page() -> None:
    page = render_org_chart(
        {
            "observations": {},
            "people": 0,
            "tree": [],
            "headcount": {"people": 0, "by_affiliation": {}, "by_access": {}, "by_status": {}},
            "reason": "no roster observation yet; run `worklog org sync --apply`",
        }
    )
    assert "org sync" in page


def test_a_day_with_no_activity_says_so_rather_than_looking_broken() -> None:
    page = render_person_day(
        {
            "name": "테스터",
            "day": "2026-09-10",
            "generated_at": "2026-09-11T06:10:00+09:00",
            "generator": "digest/1",
            "events_total": 0,
            "counts": {},
            "events": [],
            "truncated_at": None,
            "state": {},
            "identities": [],
        }
    )
    assert "활동 없음" in page and "수집 실패" in page


def test_rendered_titles_are_escaped() -> None:
    """A title is somebody's text, and it lands in a page people open."""
    page = render_person_day(
        {
            "name": "테스터",
            "day": "2026-09-10",
            "generated_at": "x",
            "generator": "digest/1",
            "events_total": 1,
            "counts": {"by_source": {"slack": 1}},
            "events": [
                {
                    "time": "09:00",
                    "source": "slack",
                    "event_type": "message",
                    "container": "#c",
                    "thread": None,
                    "permalink": None,
                    "title": "<script>alert(1)</script>",
                }
            ],
            "truncated_at": None,
            "state": {},
            "identities": [],
        }
    )
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page
