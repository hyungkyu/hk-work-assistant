"""Accounts with activity and no owner: finding them, and answering once.

The question this closes is HK's, from 2026-09-11: an unrecognised Slurm name
is either somebody new or somebody's former name, no table maps the old names
to the current ones, and so the system must ask rather than guess -- once, and
then remember the answer.
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rlwrld_worklog.org.unmapped import (  # noqa: E402
    EMAIL_KINDS,
    identity_kind,
    list_unmapped,
    resolve,
    scan,
)

REQUIRES_DATABASE = pytest.mark.skipif(
    not os.environ.get("WORKLOG_TEST_DATABASE_URL"),
    reason="set WORKLOG_TEST_DATABASE_URL to a throwaway database to run the unmapped round trip",
)

PERSON = "p_unmappedtest"
MARK = "unmapped-test:"


def test_a_handles_kind_comes_from_what_the_projection_recorded() -> None:
    """A GitHub login is not a Slack id even when the strings are identical."""
    assert identity_kind("github", "github_login") == "github"
    assert identity_kind("slurm", "slurm_user") == "slurm"
    assert identity_kind("github", "git_email") in EMAIL_KINDS


def test_a_source_whose_actor_is_unambiguous_needs_no_actor_kind() -> None:
    assert identity_kind("slack", "unknown") == "slack"
    assert identity_kind("notion", None) == "notion"


def test_an_unclassifiable_source_yields_no_kind_rather_than_a_guess() -> None:
    assert identity_kind("carrier-pigeon", "unknown") is None


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
    """Empty the timeline, not only this suite's rows.

    `scan` asks a question about the whole database -- every actor with
    activity and no owner -- so a row another suite left behind is an answer
    to a different question landing in this one's assertions. Deleting only
    the rows tagged here left five accounts in the queue and one expected,
    which is a failure report about nothing.
    """
    import psycopg

    with psycopg.connect(url) as connection:
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM source_object_heads")
            cursor.execute("DELETE FROM source_object_observations")
            cursor.execute("DELETE FROM timeline_events")
            cursor.execute("DELETE FROM org_unmapped_account")
            cursor.execute("DELETE FROM org_identity WHERE person_id = %s", (PERSON,))
            cursor.execute("DELETE FROM org_person_state WHERE person_id = %s", (PERSON,))
            cursor.execute("DELETE FROM org_person WHERE person_id = %s", (PERSON,))
            cursor.execute(
                "DELETE FROM roster_observation WHERE source_digest = 'unmapped-test'"
            )
        connection.commit()


def _seed_person(url: str) -> None:
    import psycopg

    with psycopg.connect(url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO roster_observation (observed_at, source, source_digest, row_count)"
                " VALUES (now(), 'roster_seed_2', 'unmapped-test', 1) RETURNING observation_id"
            )
            observation = cursor.fetchone()[0]
            cursor.execute(
                "INSERT INTO org_person (person_id, name, first_seen, last_seen)"
                " VALUES (%s, '테스터', %s, %s)",
                (PERSON, observation, observation),
            )
        connection.commit()


def _identity(url: str, kind: str, value: str) -> None:
    import psycopg

    with psycopg.connect(url) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT max(observation_id) FROM roster_observation")
            observation = cursor.fetchone()[0]
            cursor.execute(
                "INSERT INTO org_identity (kind, value, person_id, first_seen, last_seen)"
                " VALUES (%s, %s, %s, %s, %s) ON CONFLICT (kind, value) DO NOTHING",
                (kind, value, PERSON, observation, observation),
            )
        connection.commit()


def _events(url: str, *, source: str, actor: str, actor_kind: str, count: int = 1) -> None:
    import psycopg
    from psycopg.types.json import Jsonb

    with psycopg.connect(url) as connection:
        with connection.cursor() as cursor:
            for index in range(count):
                cursor.execute(
                    """
                    INSERT INTO timeline_events (
                        event_id, source, event_type, external_id, actor_external_id,
                        occurred_at, ingested_at, payload
                    ) VALUES (%s, %s, 'x', %s, %s, %s, now(), %s)
                    """,
                    (
                        str(uuid.uuid4()),
                        source,
                        f"{MARK}{uuid.uuid4()}",
                        actor,
                        f"2026-09-{10 + (index % 3):02d}T09:00:00+09:00",
                        Jsonb({"actor_kind": actor_kind}),
                    ),
                )
        connection.commit()


@REQUIRES_DATABASE
def test_an_account_nobody_owns_is_queued_with_what_it_did(database) -> None:
    _events(database, source="slurm", actor="yunsg", actor_kind="slurm_user", count=14)
    result = scan(database, dry_run=False)

    assert result.unmapped == 1
    assert result.events_unmapped == 14
    assert result.by_kind == {"slurm": 1}

    queue = list_unmapped(database)["accounts"]
    assert [(row["kind"], row["value"], row["events"]) for row in queue] == [
        ("slurm", "yunsg", 14)
    ]


@REQUIRES_DATABASE
def test_an_account_the_roster_already_claims_is_not_a_question(database) -> None:
    _identity(database, "github", "mskim")
    _events(database, source="github", actor="mskim", actor_kind="github_login", count=3)
    assert scan(database, dry_run=False).unmapped == 0


@REQUIRES_DATABASE
def test_an_email_handle_matches_any_of_the_three_email_columns(database) -> None:
    """The roster keeps official, personal and school emails in separate columns."""
    _identity(database, "email_school", "someone@school.invalid")
    _events(database, source="github", actor="someone@school.invalid",
            actor_kind="git_email", count=2)
    assert scan(database, dry_run=False).unmapped == 0


@REQUIRES_DATABASE
def test_the_queue_is_ordered_by_how_much_the_account_did(database) -> None:
    """Four events is a curiosity; two hundred is a person missing from every report."""
    _events(database, source="slurm", actor="quiet", actor_kind="slurm_user", count=2)
    _events(database, source="slurm", actor="busy", actor_kind="slurm_user", count=40)
    scan(database, dry_run=False)
    queue = list_unmapped(database)["accounts"]
    assert [row["value"] for row in queue] == ["busy", "quiet"]


@REQUIRES_DATABASE
def test_answering_once_means_never_being_asked_again(database) -> None:
    """The answer becomes an identity, which is what makes it permanent.

    This is also the only path by which a former Slurm name reaches the
    person who used it: no old-to-new table exists.
    """
    _events(database, source="slurm", actor="m.seo", actor_kind="slurm_user", count=9)
    scan(database, dry_run=False)

    answer = resolve(database, kind="slurm", value="m.seo", person_id=PERSON,
                     note="과거 슬럼 이름")
    assert answer["ok"] and answer["state"] == "resolved"

    assert scan(database, dry_run=False).unmapped == 0
    assert list_unmapped(database, state="open")["accounts"] == []
    [resolved] = list_unmapped(database, state="resolved")["accounts"]
    assert resolved["resolved_person_id"] == PERSON
    assert resolved["note"] == "과거 슬럼 이름"


@REQUIRES_DATABASE
def test_a_resolved_account_is_recorded_as_a_resolved_identity(database) -> None:
    """So a reader can tell what the roster said from what a person decided."""
    import psycopg

    _events(database, source="github", actor="ci-bot-account", actor_kind="github_login")
    scan(database, dry_run=False)
    resolve(database, kind="github", value="ci-bot-account", person_id=PERSON)
    with psycopg.connect(database) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT origin FROM org_identity WHERE kind = 'github' AND value = %s",
                ("ci-bot-account",),
            )
            assert cursor.fetchone()[0] == "resolved"


@REQUIRES_DATABASE
def test_an_account_judged_not_a_person_stays_judged(database) -> None:
    """Re-asking a question somebody answered is how a queue becomes noise."""
    _events(database, source="github", actor="dependabot", actor_kind="github_login", count=50)
    scan(database, dry_run=False)
    resolve(database, kind="github", value="dependabot", ignore=True, note="bot")

    scan(database, dry_run=False)
    assert list_unmapped(database, state="open")["accounts"] == []
    [ignored] = list_unmapped(database, state="ignored")["accounts"]
    # Its counts still refresh, so the page stays honest about how much it does.
    assert ignored["events"] == 50


@REQUIRES_DATABASE
def test_an_account_the_roster_later_claims_closes_itself(database) -> None:
    _events(database, source="slack", actor="U0NEW", actor_kind="unknown", count=5)
    scan(database, dry_run=False)
    assert len(list_unmapped(database, state="open")["accounts"]) == 1

    _identity(database, "slack", "U0NEW")
    result = scan(database, dry_run=False)
    assert result.resolved_now >= 1
    assert list_unmapped(database, state="open")["accounts"] == []


@REQUIRES_DATABASE
def test_resolving_needs_a_person_or_an_explicit_judgement(database) -> None:
    _events(database, source="slurm", actor="someone", actor_kind="slurm_user")
    scan(database, dry_run=False)
    with pytest.raises(ValueError, match="person id or --ignore"):
        resolve(database, kind="slurm", value="someone")


@REQUIRES_DATABASE
def test_resolving_an_unknown_person_or_account_is_refused_by_name(database) -> None:
    _events(database, source="slurm", actor="someone", actor_kind="slurm_user")
    scan(database, dry_run=False)
    assert resolve(database, kind="slurm", value="nobody", person_id=PERSON)["reason"] == (
        "no such unmapped account"
    )
    assert resolve(database, kind="slurm", value="someone", person_id="p_nope")["reason"] == (
        "no such person"
    )


@REQUIRES_DATABASE
def test_a_dry_run_counts_without_queueing(database) -> None:
    _events(database, source="slurm", actor="counted", actor_kind="slurm_user", count=3)
    result = scan(database, dry_run=True)
    assert result.unmapped == 1
    assert list_unmapped(database, state="all")["accounts"] == []
