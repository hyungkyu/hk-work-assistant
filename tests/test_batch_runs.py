"""What the batch-run reader says about the files run-logged.sh leaves.

The cases here are the ones that were invisible before the reader existed:
a run in progress, a run that was killed before it could write its finish,
and a batch that never ran at all. Each was noticed by a person reading a
log by hand days later; this module is what notices them instead.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rlwrld_worklog.batch_runs import STALL_HOURS, read_batch_runs  # noqa: E402

NOW = datetime(2026, 9, 11, 1, 0, tzinfo=timezone.utc)


def _finished(directory: Path, *, hours_ago: float = 2.0) -> None:
    finished = NOW - timedelta(hours=hours_ago)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "last.json").write_text(
        json.dumps(
            {
                "started_at": (finished - timedelta(hours=1)).isoformat(),
                "finished_at": finished.isoformat(),
                "exit_code": 0,
                "outcome": "ok",
                "command": "worklog daily-collect",
                "log": str(directory / "x.log"),
            }
        ),
        encoding="utf-8",
    )


def _running(directory: Path, *, started_hours_ago: float, log_touched_minutes_ago: float) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    log = directory / "current.log"
    log.write_text("collecting…", encoding="utf-8")
    import os

    stamp = (NOW - timedelta(minutes=log_touched_minutes_ago)).timestamp()
    os.utime(log, (stamp, stamp))
    (directory / "running.json").write_text(
        json.dumps(
            {
                "started_at": (NOW - timedelta(hours=started_hours_ago)).isoformat(),
                "command": "worklog daily-collect",
                "pid": 424242,
                "log": str(log),
            }
        ),
        encoding="utf-8",
    )


def _by_name(root: Path) -> dict:
    return {run["name"]: run for run in read_batch_runs(root, now=NOW)["runs"]}


def test_a_run_in_progress_is_reported_as_running_not_as_missing(tmp_path: Path) -> None:
    _running(tmp_path / "daily-collect", started_hours_ago=1.0, log_touched_minutes_ago=5.0)
    run = _by_name(tmp_path)["daily-collect"]
    assert run["outcome"] == "running"
    assert run["log_idle_minutes"] == 5.0
    assert run["finished_at"] is None
    assert run["overdue"] is False


def test_a_killed_run_shows_as_stalled_instead_of_leaving_no_trace(tmp_path: Path) -> None:
    """The 2026-09-08 case: Ctrl-C left neither finish record nor any sign.

    A start marker whose log has been silent past the stall threshold is a
    run that died or wedged, and the page says so instead of saying nothing.
    """
    _running(
        tmp_path / "notion-verify",
        started_hours_ago=STALL_HOURS + 2,
        log_touched_minutes_ago=(STALL_HOURS + 1) * 60,
    )
    run = _by_name(tmp_path)["notion-verify"]
    assert run["outcome"] == "stalled"
    assert "no log output" in run["detail"]
    assert read_batch_runs(tmp_path, now=NOW)["failing"] == ["notion-verify"]


def test_a_leftover_marker_older_than_the_last_finish_is_not_resurrected(tmp_path: Path) -> None:
    """A marker the finish should have removed but did not must lose to the finish."""
    directory = tmp_path / "daily-collect"
    _running(directory, started_hours_ago=30.0, log_touched_minutes_ago=30 * 60)
    _finished(directory, hours_ago=2.0)
    run = _by_name(tmp_path)["daily-collect"]
    assert run["outcome"] == "ok"
    assert run["exit_code"] == 0


def test_a_marker_newer_than_the_last_finish_wins_over_it(tmp_path: Path) -> None:
    """Yesterday's green finish must not hide that tonight's run is going."""
    directory = tmp_path / "daily-collect"
    _finished(directory, hours_ago=20.0)
    _running(directory, started_hours_ago=0.5, log_touched_minutes_ago=1.0)
    run = _by_name(tmp_path)["daily-collect"]
    assert run["outcome"] == "running"


def test_running_is_not_counted_as_failing(tmp_path: Path) -> None:
    _running(tmp_path / "daily-collect", started_hours_ago=0.5, log_touched_minutes_ago=1.0)
    payload = read_batch_runs(tmp_path, now=NOW)
    assert "daily-collect" not in payload["failing"]


def test_an_expected_batch_that_never_ran_is_named(tmp_path: Path) -> None:
    payload = read_batch_runs(tmp_path, now=NOW)
    named = {run["name"]: run for run in payload["runs"]}
    assert named["collection-audit"]["outcome"] == "never-run"
    assert named["collection-audit"]["overdue"] is True


def test_a_backfill_directory_is_never_overdue(tmp_path: Path) -> None:
    _finished(tmp_path / "backfill-2026-09-04", hours_ago=700.0)
    run = _by_name(tmp_path)["backfill-2026-09-04"]
    assert run["recurring"] is False
    assert run["overdue"] is False
