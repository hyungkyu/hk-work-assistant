"""What a ledger record becomes on the activity timeline.

GitHub and Slurm records reached the ledger and stopped there: 1,710 and
11,556 records against 0 timeline events on the nightly load of 2026-09-10
KST. These tests are the contract for the projection that closes that, and
they are written against the record shapes the live converter actually
produces (`ledger/live.py`), not against invented ones.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rlwrld_worklog.ledger.load import (  # noqa: E402
    EVENT_TYPE_BY_ENTITY,
    PROJECTED_ENTITY_TYPES,
    _payload_for_timeline,
    _projection,
)


def record(entity_type: str, **overrides) -> dict:
    base = {
        "ledger_id": "00000000-0000-0000-0000-000000000001",
        "capture_profile": "live-test/v1",
        "entity_type": entity_type,
        "source_entity_id": "entity-1",
        "scope": {},
        "relations": {},
        "raw_payload": {},
        "denormalized_label_snapshot": {},
        "provenance": {},
    }
    base.update(overrides)
    return base


def test_every_projected_entity_has_an_event_type_and_the_reverse() -> None:
    """A set and a mapping that drift produce a KeyError at load time.

    The loader looks the event type up by entity after deciding to project,
    so an entity in one and not the other is a crash on real data rather than
    a wrong row.
    """
    assert PROJECTED_ENTITY_TYPES == set(EVENT_TYPE_BY_ENTITY)


def test_event_types_are_unique_and_source_prefixed_where_they_could_collide() -> None:
    types = list(EVENT_TYPE_BY_ENTITY.values())
    assert len(types) == len(set(types))
    for entity in ("commit", "pull_request", "review", "issue", "issue_comment"):
        assert EVENT_TYPE_BY_ENTITY[entity].startswith("github_")
    assert EVENT_TYPE_BY_ENTITY["job"] == "slurm_job"


# ------------------------------------------------------------------- github


def test_a_rest_commit_is_attributed_to_the_github_account() -> None:
    projection = _projection(
        record(
            "commit",
            scope={"repository": "rlwrld-vla", "organization": "rlwrld"},
            raw_payload={
                "sha": "abc1234",
                "author": {"login": "mskim"},
                "commit": {"author": {"email": "mskim@rlwrld.ai"}},
                "html_url": "https://github.com/rlwrld/rlwrld-vla/commit/abc1234",
            },
        )
    )
    assert projection["actor"] == "mskim"
    assert projection["actor_kind"] == "github_login"
    assert projection["container"] == "rlwrld-vla"
    assert projection["permalink"].endswith("/commit/abc1234")


def test_a_mirror_commit_is_attributed_to_the_git_email_and_says_so() -> None:
    """A git object holds no GitHub account, so the projection must not invent one.

    The two handles identify the same person and live in different
    namespaces; recording which one this row holds is what lets identity
    mapping reconcile them later instead of guessing.
    """
    projection = _projection(
        record(
            "commit",
            scope={"repository": "rlwrld-vla"},
            relations={"author_email": "mskim@rlwrld.ai", "repository": "rlwrld-vla"},
            raw_payload={"sha": "def5678", "subject": "fix the walk"},
        )
    )
    assert projection["actor"] == "mskim@rlwrld.ai"
    assert projection["actor_kind"] == "git_email"
    assert projection["permalink"] is None


def test_a_commit_with_no_readable_author_says_unknown_rather_than_guessing() -> None:
    projection = _projection(record("commit", scope={"repository": "r"}))
    assert projection["actor"] is None
    assert projection["actor_kind"] == "unknown"


@pytest.mark.parametrize(
    "entity", ("pull_request", "review", "review_comment", "issue", "issue_comment")
)
def test_authored_github_activity_uses_the_login_the_collector_resolved(entity: str) -> None:
    projection = _projection(
        record(
            entity,
            scope={"repository": "rlwrld-vla"},
            relations={"author": "hyungkyu", "repository": "rlwrld-vla"},
            raw_payload={"html_url": f"https://github.com/rlwrld/rlwrld-vla/{entity}/7"},
        )
    )
    assert projection["actor"] == "hyungkyu"
    assert projection["actor_kind"] == "github_login"
    assert projection["container"] == "rlwrld-vla"


def test_a_review_is_threaded_under_the_pull_request_it_reviews() -> None:
    """A review thread has to read as one thing, or it reads as loose comments."""
    pull = "https://api.github.com/repos/rlwrld/rlwrld-vla/pulls/7"
    projection = _projection(
        record(
            "review",
            scope={"repository": "rlwrld-vla"},
            relations={"author": "storm", "pull_request_url": pull},
        )
    )
    assert projection["thread"] == pull


def test_an_issue_comment_threads_under_its_issue() -> None:
    issue = "https://api.github.com/repos/rlwrld/rlwrld-vla/issues/9"
    projection = _projection(
        record("issue_comment", relations={"author": "gerald", "issue_url": issue})
    )
    assert projection["thread"] == issue


def test_activity_with_no_thread_url_falls_back_to_its_own_id() -> None:
    projection = _projection(
        record("issue", source_entity_id="rlwrld-vla:issue:9", relations={"author": "a"})
    )
    assert projection["thread"] == "rlwrld-vla:issue:9"


# -------------------------------------------------------------------- slurm


def test_a_job_is_attributed_to_the_slurm_account_that_ran_it() -> None:
    projection = _projection(
        record(
            "job",
            source_entity_id="ncloud:12345",
            scope={"cluster": "gpu-a100", "cloud": "ncloud"},
            relations={"user": "mskim", "cluster": "gpu-a100", "state": "COMPLETED"},
        )
    )
    assert projection["actor"] == "mskim"
    # Not a GitHub login and not an email: the identity mapping has to know it
    # is a Slurm account name, because those were renamed once and the old
    # names map to current people only through that table.
    assert projection["actor_kind"] == "slurm_user"
    assert projection["container"] == "gpu-a100"
    assert projection["thread"] == "ncloud:12345"
    assert projection["permalink"] is None


def test_a_job_with_no_user_column_is_unknown_not_blank_attributed() -> None:
    projection = _projection(record("job", scope={"cluster": "c"}))
    assert projection["actor"] is None
    assert projection["actor_kind"] == "unknown"


# ------------------------------------------------- the existing four sources


def test_the_slack_and_notion_projections_are_unchanged() -> None:
    """Adding sources must not move the rows the timeline already holds."""
    message = _projection(
        record(
            "message",
            scope={"channel_id": "C1"},
            relations={"author_user_id": "U1", "thread_id": "1.0"},
            raw_payload={"permalink": "https://slack/x"},
        )
    )
    assert (message["actor"], message["container"], message["thread"]) == ("U1", "C1", "1.0")

    page = _projection(
        record(
            "page",
            source_entity_id="page-1",
            scope={"notion_source_id": "ds-1"},
            relations={"last_edited_by_user_id": "U9"},
            raw_payload={"url": "https://notion/p"},
        )
    )
    assert (page["actor"], page["container"], page["thread"]) == ("U9", "ds-1", "page-1")

    calendar = _projection(
        record("event", source_entity_id="e-1", scope={"calendar_id": "cal"},
               raw_payload={"htmlLink": "https://cal/e"})
    )
    assert (calendar["container"], calendar["permalink"]) == ("cal", "https://cal/e")


def test_the_timeline_payload_carries_the_actor_kind() -> None:
    """A consumer cannot use the handle without knowing its namespace."""
    row = record("job", relations={"user": "mskim"})
    payload = _payload_for_timeline(row, _projection(row))
    assert payload["actor_kind"] == "slurm_user"
    assert payload["ledger_id"] == row["ledger_id"]
