"""One person on both tabs, joined -- and two people with one name, left alone.

HK, 2026-10-12: 카이스트의 김동영과 리얼월드의 김동영은 같은 사람이야 ...
김준수는 동명이인이야.

Run inside a transaction that always rolls back, like the reset tests: the
candidate search reads the newest observation of each tab, and a shared test
database is full of other tests' people.
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


def _migrated() -> str:
    from rlwrld_worklog.ledger.load import apply_migrations

    url = os.environ["WORKLOG_TEST_DATABASE_URL"]
    apply_migrations(
        database_url=url,
        migrations_dir=Path(__file__).resolve().parents[1] / "sql" / "migrations",
        dry_run=False,
    )
    return url


def _person(cursor, name, observation, *, affiliation, identities=()):
    person_id = f"p_m_{uuid.uuid4().hex[:10]}"
    cursor.execute(
        "INSERT INTO org_person (person_id, name, first_seen, last_seen) VALUES (%s,%s,%s,%s)",
        (person_id, name, observation, observation),
    )
    cursor.execute(
        "INSERT INTO org_person_state (observation_id, person_id, affiliation, access_level, "
        "status, department_raw) VALUES (%s,%s,%s,'staff_equivalent','active',%s)",
        (observation, person_id, affiliation, f"{affiliation} dept"),
    )
    for kind, value in identities:
        cursor.execute(
            "INSERT INTO org_identity (person_id, kind, value, first_seen, last_seen) "
            "VALUES (%s,%s,%s,%s,%s)",
            (person_id, kind, value, observation, observation),
        )
    return person_id


@pytest.fixture()
def world():
    import psycopg

    connection = psycopg.connect(_migrated())
    connection.autocommit = False
    cursor = connection.cursor()
    obs = {}
    for source in ("roster_seed_2", "roster_seed_ext"):
        cursor.execute(
            "INSERT INTO roster_observation (observed_at, source, row_count) "
            "VALUES (now() + interval '2 days', %s, 1) RETURNING observation_id",
            (source,),
        )
        obs[source] = cursor.fetchone()[0]
    tag = uuid.uuid4().hex[:6]
    people = {
        "same_in": _person(
            cursor,
            f"겸직{tag}",
            obs["roster_seed_2"],
            affiliation="internal",
            identities=[("employee_no", f"9{tag}"), ("email_official", f"a{tag}@rlwrld.ai")],
        ),
        "same_ext": _person(
            cursor,
            f"겸직{tag}",
            obs["roster_seed_ext"],
            affiliation="student",
            identities=[("email_school", f"a{tag}@kaist.ac.kr")],
        ),
        "twin_in": _person(
            cursor,
            f"동명{tag}",
            obs["roster_seed_2"],
            affiliation="internal",
            identities=[("employee_no", f"8{tag}")],
        ),
        "twin_ext": _person(
            cursor,
            f"동명{tag}",
            obs["roster_seed_ext"],
            affiliation="student",
            identities=[("email_school", f"b{tag}@snu.ac.kr")],
        ),
    }
    # The production shape: the sync writes a row for every known person in
    # every observation, marking whom a tab does not list as absent. The
    # first version of this fixture left those rows out, the tests passed,
    # and the first real run failed on org_person_state_pkey.
    home = {
        "same_in": "roster_seed_2",
        "same_ext": "roster_seed_ext",
        "twin_in": "roster_seed_2",
        "twin_ext": "roster_seed_ext",
    }
    for key, person_id in people.items():
        for source, observation in obs.items():
            if source == home[key]:
                continue
            cursor.execute(
                "INSERT INTO org_person_state (observation_id, person_id, affiliation, "
                "access_level, status) VALUES (%s,%s,'unknown','unknown','absent_from_sheet')",
                (observation, person_id),
            )
    cursor.execute(
        "INSERT INTO person_day_digest (person_id, day, generated_at, generator, "
        "events_total, counts, events) VALUES (%s,%s,now(),'digest/1',2,'{}','[]')",
        (people["same_ext"], date(2026, 10, 1)),
    )
    try:
        yield cursor, people, obs, tag
    finally:
        connection.rollback()
        connection.close()


@REQUIRES_DATABASE
def test_a_person_on_both_tabs_becomes_one_with_both_affiliations(world) -> None:
    from rlwrld_worklog.org.merge import merge_dual

    cursor, people, obs, tag = world
    merge_dual(cursor, exclude=[f"동명{tag}"], apply=True)

    cursor.execute("SELECT 1 FROM org_person WHERE person_id = %s", (people["same_ext"],))
    assert cursor.fetchone() is None
    cursor.execute(
        "SELECT observation_id, affiliation, status FROM org_person_state "
        "WHERE person_id = %s AND observation_id = ANY(%s) ORDER BY observation_id",
        (people["same_in"], [obs["roster_seed_2"], obs["roster_seed_ext"]]),
    )
    rows = {observation: (affiliation, status) for observation, affiliation, status in cursor.fetchall()}
    assert rows[obs["roster_seed_2"]] == ("internal", "active"), "the company tab keeps the company row"
    assert rows[obs["roster_seed_ext"]] == ("student", "active"), (
        "the lab tab's absence is replaced by the lab row: 겸직 keeps both"
    )


@REQUIRES_DATABASE
def test_the_lab_identities_follow_so_the_next_sync_keeps_them_joined(world) -> None:
    """The school email now names the kept person, so `resolve_person_id`
    maps tomorrow's external row onto them with no special case."""
    from rlwrld_worklog.org.merge import merge_dual
    from rlwrld_worklog.org.plan import resolve_person_id

    cursor, people, obs, tag = world
    merge_dual(cursor, exclude=[f"동명{tag}"], apply=True)
    cursor.execute(
        "SELECT kind, value, person_id FROM org_identity WHERE person_id = %s", (people["same_in"],)
    )
    known = {(kind, value): person for kind, value, person in cursor.fetchall()}
    assert ("email_school", f"a{tag}@kaist.ac.kr") in known

    tomorrow = resolve_person_id(
        {"name": f"겸직{tag}", "email_school": f"a{tag}@kaist.ac.kr"}, known
    )
    assert tomorrow == people["same_in"]


@REQUIRES_DATABASE
def test_a_name_you_excluded_is_left_as_two_people(world) -> None:
    """김준수는 동명이인이야. A name is not evidence; the person is."""
    from rlwrld_worklog.org.merge import merge_dual

    cursor, people, obs, tag = world
    found = merge_dual(cursor, exclude=[f"동명{tag}"], apply=True)
    assert f"동명{tag}" in found.excluded
    cursor.execute(
        "SELECT count(*) FROM org_person WHERE person_id = ANY(%s)",
        ([people["twin_in"], people["twin_ext"]],),
    )
    assert cursor.fetchone()[0] == 2


@REQUIRES_DATABASE
def test_the_dropped_rows_digests_are_rebuilt_not_moved(world) -> None:
    """Moving would collide with the kept person's row for the same day."""
    from rlwrld_worklog.org.merge import merge_dual

    cursor, people, obs, tag = world
    found = merge_dual(cursor, exclude=[f"동명{tag}"], apply=True)
    assert "2026-10-01" in found.days_to_rebuild
    cursor.execute(
        "SELECT count(*) FROM person_day_digest WHERE person_id = %s", (people["same_ext"],)
    )
    assert cursor.fetchone()[0] == 0


@REQUIRES_DATABASE
def test_without_apply_it_only_lists(world) -> None:
    from rlwrld_worklog.org.merge import merge_dual

    cursor, people, obs, tag = world
    found = merge_dual(cursor, exclude=[], apply=False)
    names = {pair["name"] for pair in found.pairs}
    assert {f"겸직{tag}", f"동명{tag}"} <= names
    cursor.execute("SELECT 1 FROM org_person WHERE person_id = %s", (people["same_ext"],))
    assert cursor.fetchone() is not None
