"""What the scheduled batches did, read from the logs they already write.

Every batch on this machine runs through `scripts/run-logged.sh`, which writes
`<log root>/<name>/last.json` on every path -- success, failure, and failure to
start. Those files were the only record that a batch ran, and nothing read
them: the backoffice showed manifests, which say what a *collection* found and
nothing about whether the batch that should have produced one ever ran.

That gap is not hypothetical. The nightly collection was locked out by a
long-running catch-up on two consecutive nights. Both times every manifest on
the coverage grid was fine, because no manifest is written by a run that never
started, and both times it was noticed by a person reading a log by hand days
later.

This module is the read side, and only the read side: it opens files, parses
them, and returns what they say. It never runs anything.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_LOG_ROOT = "/data/rlwrld-worklog/logs"

# A batch that has not reported for longer than this is stale rather than
# quiet. It is per batch because the batches run on very different cadences,
# and a single number would either cry wolf for the hourly ones or say nothing
# about the daily ones.
EXPECTED_INTERVAL_HOURS: dict[str, float] = {
    "daily-collect": 24.0,
    "daily-catchup": 24.0,
    "collection-audit": 24.0,
    "board-audit": 1.0,
}

# Directories under the log root that are one backfill day rather than a
# recurring batch. They are reported, but never as overdue: a backfill is asked
# for, not scheduled.
ONE_OFF_PREFIXES = ("backfill-",)

# A running batch whose log has been silent longer than this is reported as
# `stalled` rather than `running`. Three hours, because the quietest healthy
# stretch observed is the Notion capture, and even that logs within the hour;
# a shorter threshold would call a slow night a stall, and a page that cries
# wolf gets read as noise.
STALL_HOURS = 3.0


def log_root() -> Path:
    return Path(os.environ.get("WORKLOG_LOG_ROOT") or DEFAULT_LOG_ROOT)


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _read_one(directory: Path, *, now: datetime) -> dict[str, Any]:
    name = directory.name
    record: dict[str, Any] = {
        "name": name,
        "recurring": not name.startswith(ONE_OFF_PREFIXES),
        "outcome": "never-run",
        "started_at": None,
        "finished_at": None,
        "exit_code": None,
        "duration_seconds": None,
        "hours_since": None,
        "overdue": False,
        "command": None,
        "log": None,
        "detail": None,
    }
    # The start marker first. run-logged.sh writes running.json when a run
    # starts and removes it only after last.json is written, so a marker that
    # is newer than the last recorded finish is the latest event in this
    # directory: a run in progress, or one that was killed and never got to
    # write its finish. Liveness is deliberately judged by the log file's
    # mtime and not by the recorded pid -- this reader usually runs inside the
    # app container, whose pid namespace is not the host's, so a pid check
    # here would call every live host process dead.
    running_record = _read_running(directory, record, now=now)
    if running_record is not None:
        return running_record

    state_path = directory / "last.json"
    try:
        stored = json.loads(state_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        # The directory exists because a run started; no state file means the
        # wrapper itself did not reach its own write. That is a real outcome
        # and is reported as one, not as an absence.
        record["outcome"] = "no-state"
        return record
    except (OSError, ValueError) as error:
        record["outcome"] = "unreadable"
        record["detail"] = str(error)[:200]
        return record
    if not isinstance(stored, dict):
        record["outcome"] = "unreadable"
        return record

    started = _parse_time(stored.get("started_at"))
    finished = _parse_time(stored.get("finished_at"))
    record.update(
        {
            "outcome": str(stored.get("outcome") or "unknown"),
            "started_at": stored.get("started_at"),
            "finished_at": stored.get("finished_at"),
            "exit_code": stored.get("exit_code"),
            "command": stored.get("command"),
            "log": stored.get("log"),
        }
    )
    if started and finished:
        record["duration_seconds"] = round((finished - started).total_seconds(), 1)
    reference = finished or started
    if reference:
        hours = (now - reference).total_seconds() / 3600
        record["hours_since"] = round(hours, 2)
        expected = EXPECTED_INTERVAL_HOURS.get(name)
        # 1.5x the cadence before calling it overdue: a nightly batch that
        # started late is not a missed night, and a page that cries wolf gets
        # read as noise, which is how a real miss goes unseen.
        record["overdue"] = bool(
            record["recurring"] and expected is not None and hours > expected * 1.5
        )
    return record


def _read_running(
    directory: Path, record: dict[str, Any], *, now: datetime
) -> dict[str, Any] | None:
    """The record for a start marker newer than the last finish, or None."""
    try:
        marker = json.loads((directory / "running.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        marker = {}
    if not isinstance(marker, dict):
        marker = {}
    started = _parse_time(marker.get("started_at"))

    # A marker older than the last recorded finish is leftover, not a run:
    # the finish that should have removed it exists and is newer. Fall through
    # to the normal read rather than resurrecting it.
    try:
        stored = json.loads((directory / "last.json").read_text(encoding="utf-8"))
        finished = _parse_time(stored.get("finished_at")) if isinstance(stored, dict) else None
    except (OSError, ValueError):
        finished = None
    if started and finished and finished >= started:
        return None

    record.update(
        {
            "outcome": "running",
            "started_at": marker.get("started_at"),
            "command": marker.get("command"),
            "log": marker.get("log"),
            "pid": marker.get("pid"),
        }
    )
    if started:
        record["hours_since"] = round((now - started).total_seconds() / 3600, 2)

    idle_minutes: float | None = None
    log_path = marker.get("log")
    if isinstance(log_path, str) and log_path:
        try:
            modified = datetime.fromtimestamp(Path(log_path).stat().st_mtime, tz=timezone.utc)
            idle_minutes = round((now - modified).total_seconds() / 60, 1)
        except OSError:
            idle_minutes = None
    record["log_idle_minutes"] = idle_minutes

    # Unreadable log counts as stalled, not as running: "I cannot see it
    # moving" and "it is moving" must not render the same.
    reference_hours = (
        idle_minutes / 60
        if idle_minutes is not None
        else (record["hours_since"] if record["hours_since"] is not None else STALL_HOURS + 1)
    )
    if reference_hours > STALL_HOURS:
        record["outcome"] = "stalled"
        record["detail"] = (
            "start marker with no finish record, and no log output for "
            f"{round(reference_hours, 1)}h -- killed, or wedged"
        )
    return record


def read_batch_runs(
    root: Path | None = None, *, now: datetime | None = None
) -> dict[str, Any]:
    """Every batch's last run, newest first, plus what is wrong with the set."""
    current = now or datetime.now(timezone.utc)
    directory = root or log_root()
    runs: list[dict[str, Any]] = []
    try:
        entries = sorted(item for item in directory.iterdir() if item.is_dir())
    except (OSError, ValueError):
        entries = []
    for entry in entries:
        runs.append(_read_one(entry, now=current))

    # A batch that never reported at all is the one this page exists for, so
    # the missing ones are named rather than left to be noticed as gaps.
    known = {run["name"] for run in runs}
    for name in EXPECTED_INTERVAL_HOURS:
        if name not in known:
            runs.append(
                {
                    "name": name,
                    "recurring": True,
                    "outcome": "never-run",
                    "started_at": None,
                    "finished_at": None,
                    "exit_code": None,
                    "duration_seconds": None,
                    "hours_since": None,
                    "overdue": True,
                    "command": None,
                    "log": None,
                    "detail": "no log directory; this batch has never run on this machine",
                }
            )

    runs.sort(key=lambda run: (run["finished_at"] or run["started_at"] or ""), reverse=True)
    failing = [
        run["name"]
        for run in runs
        if run["outcome"] not in {"ok", "never-run", "running"}
    ]
    overdue = [run["name"] for run in runs if run["overdue"]]
    return {
        "generated_at": current.isoformat(),
        "log_root": str(directory),
        "runs": runs,
        "counts": {
            "total": len(runs),
            "failing": len(failing),
            "overdue": len(overdue),
        },
        "failing": failing,
        "overdue": overdue,
    }
