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
from datetime import date, datetime
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


def test_excerpt_reads_a_slack_message_body():
    from rlwrld_worklog.digest import excerpt

    assert excerpt("message", {"text": "  배포  스크립트 \n 고침 "}) == "배포 스크립트 고침"


def test_excerpt_pairs_a_pull_request_title_with_its_body():
    from rlwrld_worklog.digest import excerpt

    assert excerpt("pull_request", {"title": "Fix retry", "body": "429 handling"}) == (
        "Fix retry — 429 handling"
    )


def test_excerpt_reads_a_commit_message_from_its_nested_home():
    from rlwrld_worklog.digest import excerpt

    assert excerpt("commit", {"commit": {"message": "Add sweep"}}) == "Add sweep"


def test_excerpt_flattens_notion_rich_text():
    from rlwrld_worklog.digest import excerpt

    raw = {"title": [{"plain_text": "주간"}, {"plain_text": "회고"}]}
    assert excerpt("page", raw) == "주간 회고"


def test_excerpt_is_none_rather_than_empty_when_there_are_no_words():
    from rlwrld_worklog.digest import excerpt

    assert excerpt("message", {"text": "   "}) is None
    assert excerpt("job", {}) is None
    assert excerpt(None, None) is None


def test_excerpt_truncates_long_bodies_with_an_ellipsis():
    from rlwrld_worklog.digest import EXCERPT_CHARS, excerpt

    found = excerpt("message", {"text": "가" * 900})
    assert len(found) == EXCERPT_CHARS
    assert found.endswith("…")


_CAL_LEDGER_FIELDS = {
    "schema_version": "v1",
    "capture_profile": "live",
    "source": "google_calendar",
    "entity_type": "event",
    "tenant_workspace_id": "ws",
    "tenant_status": "known",
    "source_entity_id": "evt-fanout",
    "source_updated_at_status": "unknown",
    "deleted_status": "unknown",
    "content_hash": "h",
    "source_file": "f",
    "source_file_sha256": "s",
    "record_pointer": "p",
    "legacy_layout_version": "v1",
    "converter_version": "v1",
    "observation_role": "current_head",
    "capture_completeness_status": "recorded",
}


@REQUIRES_DATABASE
def test_a_meeting_reaches_every_attendee_not_only_its_organiser(database):
    """What a person did yesterday was attend the meeting, not just book it.

    A timeline row has one actor column, so without this fan-out a meeting
    shows up only in the organiser's day and is invisible to everyone who sat
    in it. Declined invitations are not attendance; the organiser, who is also
    listed as an attendee, must still appear exactly once.
    """
    import uuid

    import psycopg
    from psycopg.types.json import Jsonb

    from rlwrld_worklog.digest import _ALL_KINDS, _EVENTS_SQL

    organiser, attendee, event_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    moment = datetime(2026, 9, 12, 10, tzinfo=KST)
    relations = {
        "organizer_email": "a@rlwrld.ai",
        "attendee_responses": [
            {"email": "a@rlwrld.ai", "responseStatus": "accepted"},
            # Upper case on purpose: an address is not case sensitive, and a
            # roster that stores it lowercase must still match.
            {"email": "B@RLWRLD.ai", "responseStatus": "accepted"},
            {"email": "c@rlwrld.ai", "responseStatus": "declined"},
        ],
    }
    with psycopg.connect(os.environ["WORKLOG_TEST_DATABASE_URL"]) as connection:
        with connection.cursor() as cursor:
            # The fixture recreates the base schema but the org tables outlive
            # it, so this test clears only what it is about to insert. Written
            # out rather than truncating: a test that wipes tables it does not
            # own destroys the next test's fixtures.
            cursor.execute(
                "DELETE FROM org_identity WHERE value IN ('a@rlwrld.ai', 'b@rlwrld.ai')"
            )
            cursor.execute(
                "DELETE FROM person_day_digest WHERE person_id IN "
                "(SELECT person_id FROM org_person WHERE name IN ('주최', '참석'))"
            )
            cursor.execute("DELETE FROM org_person WHERE name IN ('주최', '참석')")
            cursor.execute("DELETE FROM timeline_events WHERE external_id = 'evt-fanout'")
            cursor.execute("DELETE FROM ledger_records WHERE source_entity_id = 'evt-fanout'")
            cursor.execute(
                "INSERT INTO roster_observation (observed_at, source, row_count) "
                "VALUES (now(), 'roster_seed_2', 2) RETURNING observation_id"
            )
            observation = cursor.fetchone()[0]
            for person, name, email in (
                (organiser, "주최", "a@rlwrld.ai"),
                (attendee, "참석", "b@rlwrld.ai"),
            ):
                cursor.execute(
                    "INSERT INTO org_person (person_id, name, first_seen, last_seen) "
                    "VALUES (%s, %s, %s, %s)",
                    (person, name, observation, observation),
                )
                cursor.execute(
                    "INSERT INTO org_identity (person_id, kind, value, first_seen, last_seen, "
                    "origin) VALUES (%s, 'email_official', %s, %s, %s, 'roster')",
                    (person, email, observation, observation),
                )
            fields = dict(_CAL_LEDGER_FIELDS)
            fields.update(
                ledger_id=event_id,
                scope=Jsonb({"calendar_id": "cal"}),
                source_entity_key=Jsonb({"id": "evt-fanout"}),
                raw_payload=Jsonb({"summary": "주간 리뷰", "description": "로드맵 점검"}),
                relations=Jsonb(relations),
                provenance=Jsonb({}),
                coverage=Jsonb({}),
                observation_window=Jsonb({}),
                capture_completeness=Jsonb({}),
                supplement_provenance=Jsonb({}),
                visibility_routing=Jsonb({}),
                denormalized_label_snapshot=Jsonb({}),
                source_created_at=moment,
                collected_at=moment,
            )
            cursor.execute(
                f"INSERT INTO ledger_records ({','.join(fields)}) "
                f"VALUES ({','.join('%s' for _ in fields)})",
                list(fields.values()),
            )
            cursor.execute(
                "INSERT INTO timeline_events (event_id, source, event_type, external_id, "
                "actor_external_id, occurred_at, ingested_at, container_id, thread_id, payload) "
                "VALUES (%s, 'google_calendar', 'calendar_event', 'evt-fanout', 'a@rlwrld.ai', "
                "%s, now(), 'cal', 'evt-fanout', %s)",
                (event_id, moment, Jsonb({"labels": {}})),
            )
        connection.commit()
        with connection.cursor() as cursor:
            cursor.execute(
                _EVENTS_SQL,
                {
                    "start": datetime(2026, 9, 12, tzinfo=KST),
                    "end": datetime(2026, 9, 13, tzinfo=KST),
                    "kinds": list(_ALL_KINDS),
                },
            )
            rows = cursor.fetchall()

    # Other tests share this database and this window; this test is about the
    # calendar rows it inserted.
    rows = [row for row in rows if row[2] == "google_calendar"]
    people = [str(row[0]) for row in rows]
    assert sorted(people) == sorted([str(organiser), str(attendee)])
    assert people.count(str(organiser)) == 1
    from rlwrld_worklog.digest import _event as row_to_event

    assert all(row_to_event(row)["excerpt"] == "주간 리뷰 — 로드맵 점검" for row in rows)


def test_slack_markup_becomes_something_a_person_can_read():
    from rlwrld_worklog.digest import excerpt

    found = excerpt(
        "message",
        {"text": "<@U07EKRU6F7H> <#C01|eng> 배포 <https://x.dev|링크> 봐줘"},
        names={"U07EKRU6F7H": "류형규"},
    )
    assert found == "@류형규 #eng 배포 링크 봐줘"


def test_an_unknown_slack_id_keeps_its_id_rather_than_inventing_a_name():
    from rlwrld_worklog.digest import excerpt

    assert excerpt("message", {"text": "<@UNOBODY> 봐줘"}, names={}) == "<@UNOBODY> 봐줘"


def test_a_slack_line_says_the_channel_by_name():
    from rlwrld_worklog.digest import _where

    assert _where("slack", "message", {"channel_name": "eng"}, {}, "C07") == "#eng"
    assert _where("slack", "message", {}, {}, "C07") == "C07"


def test_a_meeting_line_carries_its_clock_time_and_headcount():
    from rlwrld_worklog.digest import _detail

    raw = {
        "start": {"dateTime": "2026-09-15T10:00:00+09:00"},
        "end": {"dateTime": "2026-09-15T11:30:00+09:00"},
        "attendees": [{}, {}, {}],
    }
    assert _detail("google_calendar", "event", raw, None) == "10:00–11:30 · 3명"


def test_an_all_day_event_says_so_rather_than_claiming_midnight():
    from rlwrld_worklog.digest import _detail

    raw = {"start": {"date": "2026-09-15"}, "end": {"date": "2026-09-16"}}
    assert _detail("google_calendar", "event", raw, None) == "종일"


def test_a_page_saved_many_times_folds_into_one_line_with_a_count():
    from rlwrld_worklog.digest import collapse

    events = [
        {"source": "notion", "where": "주간 회고", "excerpt": "주간 회고", "time": "09:10"},
        {"source": "notion", "where": "주간 회고", "excerpt": "주간 회고", "time": "09:40"},
        {"source": "notion", "where": "주간 회고", "excerpt": "주간 회고", "time": "11:05"},
        {"source": "slack", "where": "#eng", "excerpt": "배포했어", "time": "11:10"},
    ]
    folded = collapse(events)
    assert [event["excerpt"] for event in folded] == ["주간 회고", "배포했어"]
    assert folded[0]["repeat"] == 3
    assert (folded[0]["time"], folded[0]["last_time"]) == ("09:10", "11:05")
    assert folded[1]["repeat"] == 1


def test_lines_with_no_words_are_never_folded_together():
    """Two Slurm jobs with no readable payload are two jobs, not one run twice."""
    from rlwrld_worklog.digest import collapse

    events = [
        {"source": "slurm", "where": "cluster", "excerpt": None, "time": "01:00"},
        {"source": "slurm", "where": "cluster", "excerpt": None, "time": "02:00"},
    ]
    assert len(collapse(events)) == 2


def test_a_notion_block_reads_its_words_out_of_whatever_shape_it_has():
    """A block names its rich_text after its own type, so there is no one field."""
    from rlwrld_worklog.digest import excerpt

    assert excerpt("block", {"paragraph": {"rich_text": [{"plain_text": "GPU 재분배"}]}}) == "GPU 재분배"
    assert excerpt("block", {"to_do": {"rich_text": [{"plain_text": "초안 정리"}]}}) == "초안 정리"
    assert excerpt("block", {"divider": {}}) is None


def test_a_block_is_placed_by_its_document_not_its_own_id():
    from rlwrld_worklog.digest import _where

    parent = {"properties": {"title": {"title": [{"plain_text": "주간 회고"}]}}}
    assert _where("notion", "block", {}, {}, "c-id", parent) == "주간 회고"
    assert _where("notion", "block", {}, {}, "c-id", None) == "c-id"


def test_a_document_edited_paragraph_by_paragraph_is_one_line():
    """Thirty blocks in one document is one piece of news, not thirty."""
    from rlwrld_worklog.digest import collapse

    events = [
        {"source": "notion", "where": "주간 회고", "excerpt": "첫 문단",
         "fold_by": "document", "time": "09:10"},
        {"source": "notion", "where": "주간 회고", "excerpt": "둘째 문단",
         "fold_by": "document", "time": "09:12"},
        {"source": "notion", "where": "로드맵", "excerpt": "셋째",
         "fold_by": "document", "time": "10:00"},
    ]
    folded = collapse(events)
    assert [event["where"] for event in folded] == ["주간 회고", "로드맵"]
    assert folded[0]["repeat"] == 2
    assert folded[0]["last_time"] == "09:12"


@REQUIRES_DATABASE
def test_a_block_line_names_its_document_through_the_parent_join(database):
    """The LATERAL lookup that turns a page id into a page title, run for real."""
    import uuid

    import psycopg
    from psycopg.types.json import Jsonb

    from rlwrld_worklog.digest import _ALL_KINDS, _EVENTS_SQL
    from rlwrld_worklog.digest import _event as row_to_event

    person, page_id, block = uuid.uuid4(), "page-1", uuid.uuid4()
    page_ledger = uuid.uuid4()
    moment = datetime(2026, 9, 12, 10, tzinfo=KST)

    def ledger_row(cursor, ledger_id, entity_type, source_entity_id, raw, relations):
        fields = dict(_CAL_LEDGER_FIELDS)
        fields.update(
            source="notion",
            entity_type=entity_type,
            source_entity_id=source_entity_id,
            ledger_id=ledger_id,
            scope=Jsonb({}),
            source_entity_key=Jsonb({"id": source_entity_id}),
            raw_payload=Jsonb(raw),
            relations=Jsonb(relations),
            provenance=Jsonb({}),
            coverage=Jsonb({}),
            observation_window=Jsonb({}),
            capture_completeness=Jsonb({}),
            supplement_provenance=Jsonb({}),
            visibility_routing=Jsonb({}),
            denormalized_label_snapshot=Jsonb({}),
            source_created_at=moment,
            collected_at=moment,
        )
        cursor.execute(
            f"INSERT INTO ledger_records ({','.join(fields)}) "
            f"VALUES ({','.join('%s' for _ in fields)})",
            list(fields.values()),
        )

    with psycopg.connect(os.environ["WORKLOG_TEST_DATABASE_URL"]) as connection:
        with connection.cursor() as cursor:
            # Repeatable on a database other tests have already written to:
            # this clears exactly the rows it is about to insert.
            cursor.execute("DELETE FROM org_identity WHERE value = 'notion-user-1'")
            cursor.execute(
                "DELETE FROM person_day_digest WHERE person_id IN "
                "(SELECT person_id FROM org_person WHERE name = '블록편집자')"
            )
            cursor.execute("DELETE FROM org_person WHERE name = '블록편집자'")
            cursor.execute(
                "DELETE FROM timeline_events WHERE external_id IN ('block-1')"
            )
            cursor.execute(
                "DELETE FROM ledger_records WHERE source_entity_id IN ('block-1', 'page-1')"
            )
            cursor.execute(
                "INSERT INTO roster_observation (observed_at, source, row_count) "
                "VALUES (now(), 'roster_seed_2', 1) RETURNING observation_id"
            )
            observation = cursor.fetchone()[0]
            cursor.execute(
                "INSERT INTO org_person (person_id, name, first_seen, last_seen) "
                "VALUES (%s, '블록편집자', %s, %s)",
                (person, observation, observation),
            )
            cursor.execute(
                "INSERT INTO org_identity (person_id, kind, value, first_seen, last_seen, "
                "origin) VALUES (%s, 'notion', 'notion-user-1', %s, %s, 'roster')",
                (person, observation, observation),
            )
            ledger_row(
                cursor,
                page_ledger,
                "page",
                page_id,
                {"properties": {"title": {"title": [{"plain_text": "주간 회고"}]}}},
                {},
            )
            ledger_row(
                cursor,
                block,
                "block",
                "block-1",
                {"paragraph": {"rich_text": [{"plain_text": "GPU 재분배 정리"}]}},
                {"page_id": page_id, "last_edited_by_user_id": "notion-user-1"},
            )
            cursor.execute(
                "INSERT INTO timeline_events (event_id, source, event_type, external_id, "
                "actor_external_id, occurred_at, ingested_at, container_id, thread_id, payload) "
                "VALUES (%s, 'notion', 'notion_block', 'block-1', 'notion-user-1', %s, now(), "
                "%s, 'block-1', %s)",
                (block, moment, page_id, Jsonb({"labels": {}})),
            )
        connection.commit()
        with connection.cursor() as cursor:
            cursor.execute(
                _EVENTS_SQL,
                {
                    "start": datetime(2026, 9, 12, tzinfo=KST),
                    "end": datetime(2026, 9, 13, tzinfo=KST),
                    "kinds": list(_ALL_KINDS),
                },
            )
            rows = cursor.fetchall()

    # Other tests share this database and this window; this test is about the
    # Notion rows it inserted.
    mine = [row for row in rows if row[2] == "notion"]
    assert len(mine) == 1
    event = row_to_event(mine[0])
    assert event["where"] == "주간 회고"
    assert event["excerpt"] == "GPU 재분배 정리"
    assert event["fold_by"] == "document"
