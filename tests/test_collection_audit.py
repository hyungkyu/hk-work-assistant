"""The collection audit: what it counts as a day that was not collected.

The judgement is a pure function over a coverage payload, so most of these
tests drive it with a literal grid — no archive, no clock, no store. The last
few run the command against a synthetic archive, because the window it asks
for (the last N *finished* KST days) is the command's decision and not the
module's.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from test_collection_status import paths, write_manifest  # noqa: E402,F401

from rlwrld_worklog import cli, collection_audit  # noqa: E402

KST = collection_audit.KST
NOW = datetime(2026, 9, 5, 17, 0, tzinfo=timezone.utc)  # 2026-09-06 02:00 KST


def cell(coverage: str, **extra: object) -> dict[str, object]:
    return {
        "coverage": coverage,
        "runs": 1,
        "last_status": "success",
        "last_run_id": "20260903T010000Z-aaaaaa",
        "time_coverage": "complete",
        "evidence_class": "manifest",
        **extra,
    }


def grid(*rows: tuple[str, dict[str, object]], sources: tuple[str, ...] = ("slack", "notion")):
    """A coverage payload of the shape `collection_status.coverage` returns."""
    return {
        "start": rows[0][0],
        "end": rows[-1][0],
        "sources": list(sources),
        "environment": "production",
        "environment_scope": "production",
        "rows": [{"date": date, "cells": cells} for date, cells in rows],
    }


# ------------------------------------------------------------- the verdict


def test_a_day_no_source_collected_is_reported_for_every_source() -> None:
    """The four days nobody could see, in the shape they had."""
    report = collection_audit.audit(
        grid(
            ("2026-09-02", {"slack": cell("not_collected"), "notion": cell("not_collected")}),
            ("2026-09-03", {"slack": cell("not_collected"), "notion": cell("not_collected")}),
        ),
        now=NOW,
    )
    assert report["ok"] is False
    assert report["source_days_missing"] == 4
    assert report["source_days_examined"] == 4
    assert report["counts"] == {"not_collected": 4}


def test_a_collected_day_is_not_a_finding() -> None:
    report = collection_audit.audit(
        grid(("2026-09-02", {"slack": cell("collected"), "notion": cell("collected")})),
        now=NOW,
    )
    assert report["ok"] is True
    assert report["gaps"] == []


def test_a_run_that_named_what_it_skipped_is_not_a_finding() -> None:
    """Honest reporting is not a gap.

    `collected_with_skips` is a run that finished and said what it could not
    reach. `partial` is a run that does not know what it missed, and that one
    is reported.
    """
    report = collection_audit.audit(
        grid(
            (
                "2026-09-02",
                {"slack": cell("collected_with_skips"), "notion": cell("partial")},
            )
        ),
        now=NOW,
    )
    assert [gap["source"] for gap in report["gaps"]] == ["notion"]


@pytest.mark.parametrize(
    "verdict",
    ["not_collected", "partial", "failed", "running", "unknown", "unverified", "unexamined"],
)
def test_every_verdict_that_is_not_collection_is_reported(verdict: str) -> None:
    """Absence of evidence is not evidence of collection.

    `unknown`, `unverified` and `unexamined` each mean something different
    about why the archive cannot say, and none of them means the day was read.
    """
    report = collection_audit.audit(
        grid(("2026-09-02", {"slack": cell(verdict)}), sources=("slack",)),
        now=NOW,
    )
    assert report["counts"] == {verdict: 1}
    assert report["gaps"][0]["coverage"] == verdict


def test_a_source_with_no_cell_at_all_is_reported_rather_than_assumed() -> None:
    """A source missing from the row is the loudest kind of not collected."""
    report = collection_audit.audit(
        grid(("2026-09-02", {"slack": cell("collected")}), sources=("slack", "notion")),
        now=NOW,
    )
    assert [gap["source"] for gap in report["gaps"]] == ["notion"]
    assert report["gaps"][0]["coverage"] == "unknown"


def test_a_finding_carries_what_the_next_step_needs() -> None:
    report = collection_audit.audit(
        grid(("2026-09-02", {"slack": cell("failed", runs=2, last_status="failed")}), sources=("slack",)),
        now=NOW,
    )
    gap = report["gaps"][0]
    assert gap["date"] == "2026-09-02" and gap["source"] == "slack"
    assert gap["runs"] == 2 and gap["last_status"] == "failed"
    assert gap["last_run_id"] == "20260903T010000Z-aaaaaa"


# ------------------------------------------------------------- the summary


def test_the_summary_names_the_missing_source_days_not_only_the_count() -> None:
    """A count says something is wrong; the names say what to run."""
    report = collection_audit.audit(
        grid(("2026-09-02", {"slack": cell("collected"), "notion": cell("not_collected")})),
        now=NOW,
    )
    assert "notion 09-02" in report["summary"]
    assert "미수집 1" in report["summary"]


def test_a_clean_grid_says_zero_rather_than_saying_nothing() -> None:
    """A silent audit and an audit that did not run must not look alike."""
    report = collection_audit.audit(
        grid(("2026-09-02", {"slack": cell("collected"), "notion": cell("collected")})),
        now=NOW,
    )
    assert "미수집 0" in report["summary"]
    assert "2026-09-02" in report["summary"]


def test_the_summary_fits_in_next_action_however_wide_the_gap_is() -> None:
    """The store caps `next_action` at 500 characters and rejects longer.

    A dispatch was rejected for exactly that on 2026-09-04, so the line is cut
    where it is made rather than where it is stored.
    """
    sources = ("slack", "notion", "google_calendar", "github", "slurm")
    rows = [
        (f"2026-08-{day:02d}", {source: cell("not_collected") for source in sources})
        for day in range(1, 31)
    ]
    report = collection_audit.audit(grid(*rows, sources=sources), now=NOW)
    assert report["source_days_missing"] == 150
    assert len(report["summary"]) <= collection_audit.MAX_SUMMARY_CHARS
    assert "외 " in report["summary"], "the ones it could not name are still counted"


def test_the_summary_is_stamped_in_kst_because_the_days_are_kst_days() -> None:
    report = collection_audit.audit(
        grid(("2026-09-02", {"slack": cell("collected")}), sources=("slack",)),
        now=NOW,
    )
    assert "09-06 02:00 KST" in report["summary"]


# ----------------------------------------------------------------- the CLI


def audit_payload(*arguments: str, capsys) -> dict:
    assert cli.main(["collection", "audit", *arguments]) == 0
    return json.loads(capsys.readouterr().out)


def test_the_window_ends_yesterday_because_today_is_not_over(paths, capsys) -> None:
    """A day still in progress is incomplete for a reason that is not a defect.

    Auditing it would report a finding every single day, which is the fastest
    way to teach a reader to ignore the report.
    """
    payload = audit_payload(capsys=capsys)
    yesterday = datetime.now(KST).date() - timedelta(days=1)
    assert payload["end"] == yesterday.isoformat()
    assert payload["days"] == collection_audit.DEFAULT_DAYS
    assert payload["start"] == (
        yesterday - timedelta(days=collection_audit.DEFAULT_DAYS - 1)
    ).isoformat()


def test_the_window_length_is_the_operators_to_choose(paths, capsys) -> None:
    payload = audit_payload("--days", "7", capsys=capsys)
    assert payload["days"] == 7


def test_an_empty_archive_reports_every_source_day_missing(paths, capsys) -> None:
    payload = audit_payload("--days", "1", capsys=capsys)
    assert payload["ok"] is False
    assert payload["source_days_missing"] == 5, "five sources, one day"


def test_a_day_with_a_finished_run_is_not_reported_for_that_source(
    paths, capsys
) -> None:
    day = datetime.now(KST).date() - timedelta(days=1)
    start = datetime.combine(day, time(12, 0), tzinfo=KST)
    write_manifest(
        paths,
        source="slack",
        run_id="20260101T000000Z-cccccc",
        started_at=start.isoformat(),
        finished_at=(start + timedelta(minutes=5)).isoformat(),
        requested_window={"since": (start - timedelta(hours=26)).isoformat()},
    )
    payload = audit_payload("--days", "1", capsys=capsys)
    assert "slack" not in [gap["source"] for gap in payload["gaps"]]
    assert payload["source_days_missing"] == 4


def test_only_the_named_sources_are_audited(paths, capsys) -> None:
    payload = audit_payload("--days", "1", "--source", "notion", capsys=capsys)
    assert payload["sources"] == ["notion"]
    assert payload["source_days_missing"] == 1


def test_a_test_capture_never_answers_a_question_about_production(
    paths, capsys
) -> None:
    """The default is production, and only the explicit sentinel widens it.

    Somebody reads this to decide whether real data was collected, and a smoke
    run answering that question is a lie.
    """
    day = datetime.now(KST).date() - timedelta(days=1)
    start = datetime.combine(day, time(12, 0), tzinfo=KST)
    write_manifest(
        paths,
        source="slack",
        environment="test",
        run_id="20260101T000000Z-dddddd",
        started_at=start.isoformat(),
        finished_at=(start + timedelta(minutes=5)).isoformat(),
        requested_window={"since": (start - timedelta(hours=26)).isoformat()},
    )
    assert audit_payload("--days", "1", capsys=capsys)["source_days_missing"] == 5
    widened = audit_payload("--days", "1", "--environment", "all", capsys=capsys)
    assert widened["source_days_missing"] == 4


def test_the_summary_flag_prints_the_line_and_nothing_else(paths, capsys) -> None:
    assert cli.main(["collection", "audit", "--days", "1", "--summary"]) == 0
    printed = capsys.readouterr().out.strip()
    assert printed.startswith("수집 감사 ")
    assert "\n" not in printed
    assert len(printed) <= collection_audit.MAX_SUMMARY_CHARS


def test_findings_do_not_make_the_command_fail(paths, capsys) -> None:
    """The batch reads `ok` from the payload.

    A non-zero exit would make the systemd unit fail on exactly the mornings
    the audit is doing its job, and a failed unit is a thing people mute.
    """
    assert cli.main(["collection", "audit", "--days", "1"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is False
