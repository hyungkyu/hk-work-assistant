"""A person over a period, and building a day that has not been built.

HK, 2026-10-12: 기간 별로도 볼 수 있게 해줘 / 클릭하면 디비에서 실시간 생성하면
되지 않아?

Each day is one of three things, and the point of these tests is that they
stay three. Until 2026-10-13 a quiet day read as 미생성, because the builder
writes rows only for people who did something and the screen took "no row"
to mean "not built".
"""

from __future__ import annotations

import os
import uuid
from datetime import date
from pathlib import Path

import pytest

REQUIRES_DATABASE = pytest.mark.skipif(
    not os.environ.get("WORKLOG_TEST_DATABASE_URL"),
    reason="set WORKLOG_TEST_DATABASE_URL to a throwaway database",
)

# Far from any real day, so another test's digests cannot fall in range.
D1, D2, D3 = date(2031, 3, 1), date(2031, 3, 2), date(2031, 3, 3)


@pytest.fixture()
def seeded():
    import psycopg

    from rlwrld_worklog.ledger.load import apply_migrations

    url = os.environ["WORKLOG_TEST_DATABASE_URL"]
    apply_migrations(
        database_url=url,
        migrations_dir=Path(__file__).resolve().parents[1] / "sql" / "migrations",
        dry_run=False,
    )
    me = f"p_r_{uuid.uuid4().hex[:8]}"
    other = f"p_r_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(url) as connection, connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO roster_observation (observed_at, source, row_count) "
            "VALUES (now(), 'roster_seed_2', 1) RETURNING observation_id"
        )
        observation = cursor.fetchone()[0]
        for person_id, name in ((me, "기간"), (other, "다른사람")):
            cursor.execute(
                "INSERT INTO org_person (person_id, name, first_seen, last_seen) VALUES (%s,%s,%s,%s)",
                (person_id, name, observation, observation),
            )
        # D1: I did something. D2: only somebody else did -- a built, quiet
        # day for me. D3: nobody's row -- not built.
        for person_id, day, total in ((me, D1, 3), (other, D2, 5)):
            cursor.execute(
                "INSERT INTO person_day_digest (person_id, day, generated_at, generator, "
                "events_total, counts, events) VALUES (%s,%s,now(),'digest/1',%s,'{}','[]')",
                (person_id, day, total),
            )
        connection.commit()
    try:
        yield url, me
    finally:
        with psycopg.connect(url) as connection, connection.cursor() as cursor:
            cursor.execute("DELETE FROM person_day_digest WHERE person_id = ANY(%s)", ([me, other],))
            cursor.execute("DELETE FROM org_person WHERE person_id = ANY(%s)", ([me, other],))
            connection.commit()


@REQUIRES_DATABASE
def test_a_period_tells_activity_quiet_and_unbuilt_apart(seeded) -> None:
    from rlwrld_worklog.digest import read_range

    url, me = seeded
    found = read_range(url, me, D1, D3)
    assert [(entry["day"], entry["state"]) for entry in found["days"]] == [
        ("2031-03-01", "activity"),
        ("2031-03-02", "quiet"),
        ("2031-03-03", "unbuilt"),
    ]
    assert found["events_total"] == 3
    assert found["unbuilt"] == ["2031-03-03"], "only the day that can be built is offered"


@REQUIRES_DATABASE
def test_a_quiet_day_is_not_reported_as_unbuilt(seeded, monkeypatch, tmp_path) -> None:
    """The bug HK saw as 미생성 on a day that had been built."""

    from rlwrld_worklog import admin_web, org_web

    url, me = seeded
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("APP_CONFIG_ROOT", str(tmp_path))
    token, _ = admin_web.store().create_session(
        subject="o", email="hyungkyu.ryu@rlwrld.ai", role="super_admin", auth_method="google"
    )

    class Request:
        cookies = {admin_web.SESSION_COOKIE: token}
        headers: dict = {}

    quiet = org_web.person_day_route(Request(), me, D2.isoformat())
    assert quiet["built"] is True and quiet["quiet"] is True
    unbuilt = org_web.person_day_route(Request(), me, D3.isoformat())
    assert unbuilt["built"] is False


@REQUIRES_DATABASE
def test_a_period_has_a_ceiling(seeded) -> None:
    from rlwrld_worklog.digest import MAX_RANGE_DAYS, read_range

    url, me = seeded
    with pytest.raises(ValueError):
        read_range(url, me, date(2031, 1, 1), date(2031, 3, 1))
    assert MAX_RANGE_DAYS == 31


def test_building_is_the_owners_and_refuses_the_future() -> None:
    import inspect

    from rlwrld_worklog import org_web

    source = inspect.getsource(org_web.build_day_route)
    assert "require_super_admin_session" in source and "_require_csrf" in source
    assert "has not happened yet" in source
    # The whole day, not one person: a single row would mark the day covered
    # and make the nightly catch-up skip everybody else.
    assert "build_day(_database_url(), day)" in source


def test_the_screen_offers_to_build_and_shows_a_period() -> None:
    script = (
        Path(__file__).resolve().parents[1] / "src" / "rlwrld_worklog" / "static" / "admin.js"
    ).read_text(encoding="utf-8")
    assert "/api/v1/admin/org/digest/build" in script
    assert "/range?start=" in script
    assert "body.quiet" in script, "a quiet day is said to be quiet"
