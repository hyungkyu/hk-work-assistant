"""The board audit: what it counts as the board disagreeing with the work.

Every test drives the pure function over literal items and history. Nothing
here opens a store, and nothing reads a clock: `now` is passed in, so a test
that passes today passes in a year.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from rlwrld_worklog import board_audit

NOW = datetime(2026, 9, 5, 6, 0, tzinfo=timezone.utc)  # 15:00 KST


def _item(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": "wi_0000000000000001",
        "title": "티켓",
        "status": "ready",
        "assigned_to": "noa",
        "requested_by": "mori",
        "next_action": "다음 할 것",
        "progress_summary": "",
        "updated_at": NOW.isoformat(),
        "archived_at": None,
    }
    base.update(overrides)
    return base


def _touch(item_id: str, actor: str, hours_ago: float) -> dict[str, object]:
    return {
        "item_id": item_id,
        "actor": actor,
        "at": (NOW - timedelta(hours=hours_ago)).isoformat(),
    }


def test_an_in_progress_item_its_assignee_has_not_touched_is_reported() -> None:
    """P0 condition 3, the one that kept coming back."""
    report = board_audit.audit(
        [_item(id="wi_1", status="in_progress", assigned_to="noa")],
        [_touch("wi_1", "noa", hours_ago=30)],
        now=NOW,
    )
    assert report["counts"]["in_progress_without_assignee_activity"] == 1
    assert report["ok"] is False
    row = report["checks"]["in_progress_without_assignee_activity"][0]
    assert row["hours_since_assignee_touched"] == 30.0


def test_the_requester_touching_a_ticket_is_not_the_work_moving() -> None:
    """`updated_at` would have been refreshed by this edit and hidden it.

    That is why the check reads the history for the assignee's own entries
    rather than the item's timestamp.
    """
    report = board_audit.audit(
        [_item(id="wi_1", status="in_progress", assigned_to="noa",
               updated_at=NOW.isoformat())],
        [_touch("wi_1", "mori", hours_ago=0.1), _touch("wi_1", "noa", hours_ago=40)],
        now=NOW,
    )
    assert report["counts"]["in_progress_without_assignee_activity"] == 1


def test_a_recently_worked_item_is_not_reported() -> None:
    report = board_audit.audit(
        [_item(id="wi_1", status="in_progress", assigned_to="noa")],
        [_touch("wi_1", "noa", hours_ago=1)],
        now=NOW,
    )
    assert report["counts"]["in_progress_without_assignee_activity"] == 0
    assert report["ok"] is True


def test_an_in_progress_item_with_no_history_at_all_is_reported() -> None:
    """Silence is not evidence of work, whatever else it might be."""
    report = board_audit.audit(
        [_item(id="wi_1", status="in_progress", assigned_to="noa")], [], now=NOW
    )
    assert report["counts"]["in_progress_without_assignee_activity"] == 1
    assert (
        report["checks"]["in_progress_without_assignee_activity"][0][
            "hours_since_assignee_touched"
        ]
        is None
    )


def test_the_roster_check_is_skipped_when_no_roster_is_given() -> None:
    """This command has no way to know who exists; it will not guess."""
    items = [_item(id="wi_1", assigned_to="someone-who-left")]
    assert board_audit.audit(items, [], now=NOW)["counts"]["assigned_outside_roster"] == 0
    named = board_audit.audit(items, [], now=NOW, roster=["mori", "local"])
    assert named["counts"]["assigned_outside_roster"] == 1


def test_done_and_cancelled_items_are_not_audited() -> None:
    """Neither one is anybody's problem, so the board cannot be wrong about it."""
    items = [
        _item(id="wi_1", status="done", assigned_to="gone", next_action=""),
        _item(id="wi_2", status="cancelled", assigned_to="gone", next_action=""),
    ]
    report = board_audit.audit(items, [], now=NOW, roster=["mori"])
    assert report["live_items"] == 0
    assert report["ok"] is True


def test_an_archived_item_is_not_audited() -> None:
    report = board_audit.audit(
        [_item(id="wi_1", next_action="", archived_at=NOW.isoformat())], [], now=NOW
    )
    assert report["live_items"] == 0


def test_a_live_item_with_no_next_action_is_reported() -> None:
    report = board_audit.audit([_item(next_action="   ")], [], now=NOW)
    assert report["counts"]["live_without_next_action"] == 1


def test_a_progress_summary_over_three_lines_is_reported() -> None:
    """P0 condition 6: the summary is the current state, not the archive."""
    assert (
        board_audit.audit([_item(progress_summary="1\n2\n3")], [], now=NOW)["counts"][
            "progress_summary_over_three_lines"
        ]
        == 0
    )
    over = board_audit.audit([_item(progress_summary="1\n\n2\n3\n4")], [], now=NOW)
    assert over["counts"]["progress_summary_over_three_lines"] == 1
    assert over["checks"]["progress_summary_over_three_lines"][0]["lines"] == 4


def test_a_ready_item_nobody_has_touched_for_a_day_is_reported() -> None:
    old = (NOW - timedelta(hours=40)).isoformat()
    report = board_audit.audit([_item(status="ready", updated_at=old)], [], now=NOW)
    assert report["counts"]["ready_untouched"] == 1
    assert report["checks"]["ready_untouched"][0]["hours_since_change"] == 40.0


def test_the_summary_fits_in_next_action() -> None:
    """The store caps next_action at 500 characters and refuses anything longer.

    A dispatch was rejected for exactly that on 2026-09-04, so the audit's own
    report is truncated at the source rather than at the queue.
    """
    items = [_item(id=f"wi_{n}", next_action="", progress_summary="1\n2\n3\n4") for n in range(400)]
    report = board_audit.audit(items, [], now=NOW, roster=["nobody"])
    assert len(report["summary"]) <= board_audit.MAX_SUMMARY_CHARS
    assert report["summary"].startswith("보드 감사 ")


def test_a_clean_board_says_so_in_one_line() -> None:
    report = board_audit.audit([_item()], [], now=NOW)
    assert report["ok"] is True
    assert report["summary"].endswith("위반 0")
