"""`scripts/backfill-days.sh`: one `daily-collect` run per KST day.

The script itself is run, with `bash`, exactly as an operator runs it. What is
replaced is the one thing that would reach a network: `WORKLOG_BACKFILL_COMMAND`
points at a stub that records the arguments it was given and exits with the code
the test asked for. Everything else — the day list, the refusals, the log
wrapper, the state file, the resume — is the real thing.

This is the first shell script in the repository with tests. The harness is
three functions and no framework, because what is worth pinning here is the
behaviour a backfill depends on and not the shell.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

KST = timezone(timedelta(hours=9))
SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "backfill-days.sh"

# A range that is over, and stays over however long from now this test is run.
FIRST = "2026-09-01"
LAST = "2026-09-04"
DAYS = ("2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04")


def stub(tmp_path: Path, *, fails_on: tuple[str, ...] = ()) -> Path:
    """A stand-in for `worklog daily-collect` that records how it was called."""
    path = tmp_path / "stub-collect.sh"
    failing = " ".join(fails_on)
    path.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$*" >> "{tmp_path}/calls.log"\n'
        f'for day in {failing}; do\n'
        '  case "$*" in *"--since $day"*) exit 7 ;; esac\n'
        "done\n"
        "exit 0\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def backfill(tmp_path: Path, *arguments: str, command: Path | None = None):
    """Run the script the way an operator would."""
    environment = dict(os.environ)
    environment["WORKLOG_LOG_ROOT"] = str(tmp_path / "logs")
    environment["WORKLOG_BACKFILL_COMMAND"] = str(command or stub(tmp_path))
    return subprocess.run(
        ["bash", str(SCRIPT), *arguments],
        capture_output=True,
        text=True,
        env=environment,
        timeout=120,
    )


def calls(tmp_path: Path) -> list[str]:
    log = tmp_path / "calls.log"
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def summary(tmp_path: Path) -> dict:
    return json.loads(
        (tmp_path / "logs" / "backfill-days" / "summary.json").read_text(encoding="utf-8")
    )


# ------------------------------------------------------------- the day list


def test_a_range_becomes_one_run_per_kst_day_in_order(tmp_path: Path) -> None:
    """The whole point: four days are four runs, not one run over four days."""
    result = backfill(tmp_path, FIRST, LAST)
    assert result.returncode == 0, result.stderr
    assert len(calls(tmp_path)) == 4
    for line, day in zip(calls(tmp_path), DAYS):
        assert f"--since {day}T00:00:00+09:00" in line


def test_each_day_is_bounded_by_the_next_days_midnight_kst(tmp_path: Path) -> None:
    """The bound is exclusive, so a day ends where the next one starts."""
    backfill(tmp_path, FIRST, LAST)
    assert "--since 2026-09-01T00:00:00+09:00 --until 2026-09-02T00:00:00+09:00" in calls(
        tmp_path
    )[0]
    assert "--until 2026-09-05T00:00:00+09:00" in calls(tmp_path)[-1]


def test_a_single_day_range_is_one_run(tmp_path: Path) -> None:
    result = backfill(tmp_path, FIRST, FIRST)
    assert result.returncode == 0, result.stderr
    assert len(calls(tmp_path)) == 1


def test_every_day_is_logged_under_the_log_root(tmp_path: Path) -> None:
    """A run whose output only exists on one machine is not a logged run."""
    backfill(tmp_path, FIRST, LAST)
    for day in DAYS:
        assert (tmp_path / "logs" / f"backfill-{day}" / "last.json").is_file()


# --------------------------------------------------------------- refusals


def test_a_reversed_range_is_refused_rather_than_quietly_swapped(tmp_path: Path) -> None:
    result = backfill(tmp_path, LAST, FIRST)
    assert result.returncode == 64
    assert "is after" in result.stderr
    assert calls(tmp_path) == []


def test_a_range_reaching_into_the_future_is_refused(tmp_path: Path) -> None:
    tomorrow = (datetime.now(KST).date() + timedelta(days=1)).isoformat()
    result = backfill(tmp_path, FIRST, tomorrow)
    assert result.returncode == 64
    assert "not over yet" in result.stderr
    assert calls(tmp_path) == []


def test_a_range_ending_today_is_refused_because_today_is_not_over(tmp_path: Path) -> None:
    """A partial day filed as a whole one reads afterwards as a quiet day."""
    today = datetime.now(KST).date().isoformat()
    result = backfill(tmp_path, FIRST, today)
    assert result.returncode == 64
    assert "not over yet" in result.stderr


def test_a_date_that_is_not_a_date_is_refused_by_name(tmp_path: Path) -> None:
    result = backfill(tmp_path, "last tuesday", LAST)
    assert result.returncode == 64
    assert "YYYY-MM-DD" in result.stderr


def test_missing_dates_print_the_usage_and_refuse(tmp_path: Path) -> None:
    result = backfill(tmp_path)
    assert result.returncode == 64
    assert "usage:" in result.stderr


def test_no_state_directory_is_created_for_a_refused_range(tmp_path: Path) -> None:
    """A refusal that leaves state behind is a refusal somebody has to undo."""
    backfill(tmp_path, LAST, FIRST)
    assert not (tmp_path / "logs" / "backfill-days").exists()


# ---------------------------------------------------------------- sources


def test_the_default_sources_are_the_ones_an_upper_bound_can_be_expressed_for(
    tmp_path: Path,
) -> None:
    backfill(tmp_path, FIRST, FIRST)
    line = calls(tmp_path)[0]
    for source in ("slack", "notion", "github", "slurm"):
        assert f"--source {source}" in line
    assert "google-calendar" not in line


def test_naming_google_calendar_is_refused_before_the_first_run(tmp_path: Path) -> None:
    """Every run here carries `--until`, which Calendar cannot express.

    Discovering that on the first day of a backfill is discovering it late: it
    was knowable before the range was typed.
    """
    result = backfill(tmp_path, FIRST, LAST, "--source", "google-calendar")
    assert result.returncode == 64
    assert "sync token" in result.stderr
    assert calls(tmp_path) == []


def test_named_sources_replace_the_defaults(tmp_path: Path) -> None:
    backfill(tmp_path, FIRST, FIRST, "--source", "slack")
    line = calls(tmp_path)[0]
    assert "--source slack" in line
    assert "--source notion" not in line


# ------------------------------------------------------ failure and resume


def test_it_stops_at_the_first_failed_day_and_says_which_one(tmp_path: Path) -> None:
    """Continuing past a failure buries the gap under later successes."""
    result = backfill(tmp_path, FIRST, LAST, command=stub(tmp_path, fails_on=("2026-09-02",)))
    assert result.returncode == 1
    assert "2026-09-02" in result.stderr and "FAILED" in result.stderr
    assert len(calls(tmp_path)) == 2, "the two days after the failure were not attempted"


def test_the_state_records_each_day_as_it_finishes_not_at_the_end(tmp_path: Path) -> None:
    """The run that failed still banked the day before it."""
    backfill(tmp_path, FIRST, LAST, command=stub(tmp_path, fails_on=("2026-09-02",)))
    state = summary(tmp_path)
    assert state["succeeded"] == ["2026-09-01"]
    assert state["failed"] == [{"day": "2026-09-02", "exit_code": 7}]
    assert state["remaining"] == ["2026-09-03", "2026-09-04"]
    assert state["outcome"] == "failed" and state["failed_day"] == "2026-09-02"


def test_a_re_run_skips_the_banked_days_and_picks_up_where_it_stopped(
    tmp_path: Path,
) -> None:
    backfill(tmp_path, FIRST, LAST, command=stub(tmp_path, fails_on=("2026-09-02",)))
    (tmp_path / "calls.log").unlink()

    result = backfill(tmp_path, FIRST, LAST)
    assert result.returncode == 0, result.stderr
    retried = calls(tmp_path)
    assert len(retried) == 3
    assert "--since 2026-09-02T00:00:00+09:00" in retried[0]
    assert "2026-09-01" not in " ".join(retried), "an already banked day was collected twice"


def test_a_completed_range_re_run_collects_nothing_again(tmp_path: Path) -> None:
    backfill(tmp_path, FIRST, LAST)
    (tmp_path / "calls.log").unlink()

    result = backfill(tmp_path, FIRST, LAST)
    assert result.returncode == 0
    assert calls(tmp_path) == []
    assert summary(tmp_path)["outcome"] == "complete"


def test_the_summary_names_the_days_that_succeeded_failed_and_are_left(
    tmp_path: Path,
) -> None:
    """What the cloud side reads. It can see all four states without the log."""
    backfill(tmp_path, FIRST, LAST)
    state = summary(tmp_path)
    assert state["outcome"] == "complete"
    assert state["succeeded"] == list(DAYS)
    assert state["failed"] == [] and state["remaining"] == []
    assert state["first_day"] == FIRST and state["last_day"] == LAST
    assert state["sources"] == ["slack", "notion", "github", "slurm"]


@pytest.mark.parametrize("field", ["started_at", "updated_at", "outcome", "log_root"])
def test_the_summary_carries_the_keys_a_reader_of_any_tick_file_expects(
    tmp_path: Path, field: str
) -> None:
    backfill(tmp_path, FIRST, FIRST)
    assert field in summary(tmp_path)


def test_a_day_that_failed_and_then_succeeded_reads_as_succeeded(tmp_path: Path) -> None:
    """The ledger is append-only, so the last word about a day is the word."""
    backfill(tmp_path, FIRST, FIRST, command=stub(tmp_path, fails_on=(FIRST,)))
    backfill(tmp_path, FIRST, FIRST)
    state = summary(tmp_path)
    assert state["succeeded"] == [FIRST]
    assert state["failed"] == []


def test_a_day_is_a_kst_day_whatever_the_hosts_timezone_is(tmp_path: Path) -> None:
    """The offset is written into the window, not taken from the machine.

    A host on UTC reading these as local dates would put every slice boundary
    nine hours out, which is the defect the collectors were ported to remove.
    """
    environment = dict(os.environ, TZ="UTC")
    result = subprocess.run(
        ["bash", str(SCRIPT), "2026-08-31", "2026-08-31"],
        capture_output=True,
        text=True,
        env={
            **environment,
            "WORKLOG_LOG_ROOT": str(tmp_path / "logs"),
            "WORKLOG_BACKFILL_COMMAND": str(stub(tmp_path)),
        },
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert (
        "--since 2026-08-31T00:00:00+09:00 --until 2026-09-01T00:00:00+09:00"
        in calls(tmp_path)[0]
    )


# ------------------------------------------------- the command it builds itself


def repository_copy_at(tmp_path: Path, name: str) -> Path:
    """A working copy of the scripts under a directory named `name`.

    The point of the name is that the caller chooses it, and one caller chooses
    a name with a space in it -- because the real repository lives at
    `~/Documents/ChatGPT/RLWRLD workspace` and the script has to survive that.
    """
    root = tmp_path / name
    (root / "scripts").mkdir(parents=True)
    (root / ".venv" / "bin").mkdir(parents=True)
    for script in ("backfill-days.sh", "run-logged.sh"):
        target = root / "scripts" / script
        target.write_text((SCRIPT.parent / script).read_text(encoding="utf-8"), encoding="utf-8")
        target.chmod(0o755)
    worklog = root / ".venv" / "bin" / "worklog"
    worklog.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$*" >> "{tmp_path}/calls.log"\n'
        "exit 0\n",
        encoding="utf-8",
    )
    worklog.chmod(0o755)
    return root


def test_the_default_command_survives_a_space_in_the_repository_path(tmp_path: Path) -> None:
    """No override: the script builds its own command and must not split it.

    Every test above hands the script a `WORKLOG_BACKFILL_COMMAND`, so none of
    them ever ran the command the script computes for itself. That command was
    a string word-split into argv, and the repository this runs in is at
    `~/Documents/ChatGPT/RLWRLD workspace`: every day of a backfill died at
    once with exit 127 trying to execute `/home/hk/Documents/ChatGPT/RLWRLD`.
    A tested script is not a run script until something runs it as installed.
    """
    root = repository_copy_at(tmp_path, "RLWRLD workspace")
    result = subprocess.run(
        ["bash", str(root / "scripts" / "backfill-days.sh"), FIRST, FIRST],
        capture_output=True,
        text=True,
        env={**os.environ, "WORKLOG_LOG_ROOT": str(tmp_path / "logs")},
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "No such file or directory" not in result.stdout + result.stderr
    assert calls(tmp_path) == [
        "daily-collect --source slack --source notion --source github --source slurm "
        f"--since {FIRST}T00:00:00+09:00 --until 2026-09-02T00:00:00+09:00"
    ]


def test_an_explicit_override_is_still_split_into_words(tmp_path: Path) -> None:
    """A caller writing the string chooses its words; the script's own path does not."""
    root = repository_copy_at(tmp_path, "plain")
    override = stub(tmp_path)
    result = subprocess.run(
        ["bash", str(root / "scripts" / "backfill-days.sh"), FIRST, FIRST, "--source", "notion"],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "WORKLOG_LOG_ROOT": str(tmp_path / "logs"),
            "WORKLOG_BACKFILL_COMMAND": f"{override} daily-collect",
        },
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert calls(tmp_path)[0].startswith("daily-collect --source notion --since")
