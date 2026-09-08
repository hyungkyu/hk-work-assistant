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
    failing = [run["name"] for run in runs if run["outcome"] not in {"ok", "never-run"}]
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
