"""Reset the roster to what the sheet says, and keep what a person decided.

HK, 2026-10-12: 그냥 리셋해. 명단, 새로 가져오는 걸로 하면 안돼?

Run inside a transaction that is always rolled back. "Delete everybody the
sheet does not list" run for real against a shared test database would take
every other test's fixtures with it -- which is also a fair picture of what
it would do in production without the guards below.
"""

from __future__ import annotations

import os
import uuid
from datetime import date, datetime, timezone
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


def _person(cursor, name: str, observation: int, status: str | None) -> str:
    person_id = f"p_test_{uuid.uuid4().hex[:10]}"
    cursor.execute(
        "INSERT INTO org_person (person_id, name, first_seen, last_seen) VALUES (%s, %s, %s, %s)",
        (person_id, name, observation, observation),
    )
    if status:
        cursor.execute(
            "INSERT INTO org_person_state (observation_id, person_id, affiliation, "
            "access_level, status) VALUES (%s, %s, 'internal', 'staff_equivalent', %s)",
            (observation, person_id, status),
        )
    return person_id


@pytest.fixture()
def world():
    """A fresh latest observation per tab, with every kind of row in it."""
    import psycopg

    url = _migrated()
    connection = psycopg.connect(url)
    connection.autocommit = False
    cursor = connection.cursor()
    observations = {}
    for source in ("roster_seed_2", "roster_seed_ext"):
        cursor.execute(
            "INSERT INTO roster_observation (observed_at, source, row_count) "
            "VALUES (now() + interval '1 day', %s, 1) RETURNING observation_id",
            (source,),
        )
        observations[source] = cursor.fetchone()[0]
    internal = observations["roster_seed_2"]
    people = {
        "on_sheet": _person(cursor, "시트에있음", internal, "active"),
        "retired": _person(cursor, "퇴사자", internal, "retired"),
        # The sync writes this state for everybody it no longer sees, which
        # is why "has a row in the latest observation" counted every leftover
        # as present.
        "absent": _person(cursor, "사본", internal, "absent_from_sheet"),
        "no_state": _person(cursor, "흔적만", internal, None),
        # On the external tab only -- a Virtual Lab person is still on the
        # sheet, just not on the internal tab.
        "external": _person(cursor, "버추얼랩", observations["roster_seed_ext"], "active"),
    }
    cursor.execute(
        "INSERT INTO person_day_digest (person_id, day, generated_at, generator, "
        "events_total, counts, events) VALUES (%s, %s, now(), 'digest/1', 3, '{}', '[]')",
        (people["absent"], date(2026, 9, 20)),
    )
    try:
        yield cursor, people, observations
    finally:
        connection.rollback()
        connection.close()


@REQUIRES_DATABASE
def test_people_on_the_sheet_are_left_alone(world) -> None:
    from rlwrld_worklog.org.reset import reset_in

    cursor, people, _ = world
    reset_in(cursor, apply=True)
    cursor.execute(
        "SELECT person_id FROM org_person WHERE person_id = ANY(%s)",
        ([people["on_sheet"], people["retired"], people["external"]],),
    )
    survivors = {row[0] for row in cursor.fetchall()}
    assert people["on_sheet"] in survivors
    assert people["retired"] in survivors, "퇴사 is still on the sheet; it is a status, not absence"
    assert people["external"] in survivors, "the external tab counts as the sheet"


@REQUIRES_DATABASE
def test_a_row_marked_absent_is_treated_as_gone(world) -> None:
    """The criterion three hand-written queries got wrong.

    `absent_from_sheet` is itself a state row in the latest observation, so
    any test of mere presence counts the leftovers as present.
    """
    from rlwrld_worklog.org.reset import reset_in

    cursor, people, _ = world
    found = reset_in(cursor, apply=True)
    cursor.execute(
        "SELECT person_id FROM org_person WHERE person_id = ANY(%s)",
        ([people["absent"], people["no_state"]],),
    )
    assert cursor.fetchall() == []
    assert found.deleted >= 2


@REQUIRES_DATABASE
def test_their_digests_go_with_them_and_the_days_are_named_for_rebuild(world) -> None:
    """Derived rows are deleted and rebuilt, so the activity that landed on a
    stale copy lands on the person it belongs to."""
    from rlwrld_worklog.org.reset import reset_in

    cursor, people, _ = world
    found = reset_in(cursor, apply=True)
    assert "2026-09-20" in found.days_to_rebuild
    cursor.execute(
        "SELECT count(*) FROM person_day_digest WHERE person_id = %s", (people["absent"],)
    )
    assert cursor.fetchone()[0] == 0


@REQUIRES_DATABASE
def test_a_person_carrying_a_decision_is_kept_and_named(world) -> None:
    """`answer_pairs` cascades on the person.

    Deleting a row that carries HK's hand-chosen Q&A pairs would erase those
    choices without an error -- the one thing in this database nobody can
    rebuild. So such a person is kept, and said to be kept, by name.
    """
    from rlwrld_worklog.org.reset import reset_in

    cursor, people, observations = world
    cursor.execute(
        "INSERT INTO org_identity (person_id, kind, value, first_seen, last_seen, origin) "
        "VALUES (%s, 'slurm', %s, %s, %s, 'resolved')",
        (
            people["absent"],
            f"old-{uuid.uuid4().hex[:6]}",
            observations["roster_seed_2"],
            observations["roster_seed_2"],
        ),
    )
    found = reset_in(cursor, apply=True)

    cursor.execute("SELECT 1 FROM org_person WHERE person_id = %s", (people["absent"],))
    assert cursor.fetchone() is not None
    kept = {entry["person_id"]: entry for entry in found.kept}
    assert people["absent"] in kept
    assert any("미확인 계정" in why for why in kept[people["absent"]]["why"])


@REQUIRES_DATABASE
def test_without_apply_nothing_changes(world) -> None:
    from rlwrld_worklog.org.reset import reset_in

    cursor, people, _ = world
    found = reset_in(cursor, apply=False)
    assert found.deleted >= 2
    cursor.execute("SELECT 1 FROM org_person WHERE person_id = %s", (people["absent"],))
    assert cursor.fetchone() is not None, "a dry run that deletes is not a dry run"
