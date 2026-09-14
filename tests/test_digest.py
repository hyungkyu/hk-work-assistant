"""Per-person daily digests: every activity, in time order, mechanically.

The round trip runs against `WORKLOG_TEST_DATABASE_URL` like the other
database suites and is skipped without it. What it verifies is the contract
HK set on 2026-09-11: all of it, not a selection, and nothing written by a
model.
"""

from __future__ import annotations

import json
import os
import pytest
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


@REQUIRES_DATABASE
def test_catch_up_builds_only_the_days_that_have_none(database) -> None:
    """A batch that needs a person to type a backfill is not a batch.

    A night the machine was off leaves a hole. `--catch-up` is what closes
    it on the next run, and it is bounded on purpose: a week, so a long
    outage catches up over several nights rather than timing out in one
    where nobody is watching.
    """
    from datetime import date as date_type

    from rlwrld_worklog.digest import catch_up, missing_days

    today = date_type(2026, 9, 14)
    _event(database, at="2026-09-10T09:00:00+09:00", source="github",
           event_type="github_commit", actor="tester", title="하나")
    _event(database, at="2026-09-12T09:00:00+09:00", source="github",
           event_type="github_commit", actor="tester", title="둘")

    # Nothing built yet: every day in the window is missing.
    assert missing_days(database, days=7, today=today) == [
        date_type(2026, 9, d) for d in range(7, 14)
    ]

    first = catch_up(database, days=7, today=today)
    assert first["missing"][0] == "2026-09-07"
    assert read_digest(database, PERSON, date_type(2026, 9, 10))["events_total"] == 1
    assert read_digest(database, PERSON, date_type(2026, 9, 12))["events_total"] == 1

    # A day that produced no rows stays "not built" -- which is honest, and
    # means the next run looks at it again rather than recording a lie.
    again = catch_up(database, days=7, today=today)
    assert "2026-09-10" not in again["missing"]
    assert "2026-09-12" not in again["missing"]


@REQUIRES_DATABASE
def test_catch_up_never_builds_today(database) -> None:
    """Today is not over; a digest of a partial day is one nothing corrects."""
    from datetime import date as date_type

    from rlwrld_worklog.digest import missing_days

    today = date_type(2026, 9, 14)
    assert date_type(2026, 9, 14) not in missing_days(database, days=7, today=today)
    assert max(missing_days(database, days=7, today=today)) == date_type(2026, 9, 13)


# --- The report: many people over a range, one page (HK, 2026-09-15) --------


def test_render_report_stitches_person_days_and_marks_not_built() -> None:
    from rlwrld_worklog.render import render_report

    sections = [
        {"built": True, "person_id": "p1", "name": "게럴드", "day": "2026-09-12",
         "generated_at": "x", "generator": "digest/1", "events_total": 2,
         "counts": {"by_source": {"slack": 2}}, "state": None, "identities": [],
         "events": [
             {"time": "09:10", "source": "slack", "event_type": "message",
              "container": "C1", "thread": None, "permalink": None, "title": "안녕"},
         ]},
        {"built": False, "person_id": "p2", "name": "샘", "day": "2026-09-12"},
    ]
    html = render_report(sections, subtitle="3명 · 2026-09-12")
    assert "게럴드" in html and "샘" in html
    assert "아직 생성 안 됨" in html          # the not-built marker
    assert html.count("<!doctype html>") == 1  # one page, not nested documents
    assert "안녕" in html


def test_render_report_is_one_page_per_call() -> None:
    from rlwrld_worklog.render import render_report

    html = render_report([{"built": False, "person_id": "p", "name": "n", "day": "2026-09-12"}])
    assert html.strip().startswith("<!doctype html>")
    assert html.count("</html>") == 1


def test_digest_report_cli_resolves_names_and_writes_one_page(tmp_path, monkeypatch, capsys) -> None:
    """The report command, with the DB layer faked: name -> id -> sections -> html."""
    from rlwrld_worklog import cli, digest as digest_module

    monkeypatch.setattr(
        digest_module, "resolve_people",
        lambda url, names: {"resolved": {"gerald": "p1"}, "unresolved": ["yum"], "ambiguous": {}},
    )
    monkeypatch.setattr(
        digest_module, "build_report_sections",
        lambda url, ids, days: [
            {"built": True, "person_id": "p1", "name": "게럴드", "day": d.isoformat(),
             "generated_at": "x", "generator": "digest/1", "events_total": 0,
             "counts": {"by_source": {}}, "state": None, "identities": [], "events": []}
            for d in days
        ],
    )
    out = tmp_path / "report.html"
    code = cli.main([
        "digest", "--report", "--database-url", "postgresql://x/y",
        "--person-name", "gerald", "--person-name", "yum",
        "--since", "2026-09-12", "--until", "2026-09-14", "--html", str(out),
    ])
    payload = json.loads(capsys.readouterr().out.split("=", 1)[1])
    assert out.exists()
    assert payload["days"] == ["2026-09-12", "2026-09-13", "2026-09-14"]
    assert payload["unresolved"] == ["yum"]
    # Unresolved names -> non-zero exit so it is noticed.
    assert code == 1
    assert "게럴드" in out.read_text(encoding="utf-8")


def test_digest_report_needs_names_or_all(monkeypatch, capsys) -> None:
    from rlwrld_worklog import cli
    with pytest.raises(SystemExit):
        cli.main(["digest", "--report", "--database-url", "postgresql://x/y"])
