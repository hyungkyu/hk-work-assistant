"""A collection run has to leave its answer where it can be read.

2026-09-17: the calendar occurrence sweep shipped, the reconcile table did not
move, and the one fact that separates "the sweep is broken" from "no collection
has run since" existed only in a terminal. Asked for three times, never
arriving -- which is a design problem, not a person problem.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "collect-once.sh"


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "scripts").mkdir()
    shutil.copy(SCRIPT, tmp_path / "scripts" / "collect-once.sh")
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    return tmp_path


def _fake_worklog(workspace: Path, *, body: str, status: int = 0) -> None:
    path = workspace / ".venv" / "bin" / "worklog"
    path.write_text(f"#!/usr/bin/env bash\n{body}\nexit {status}\n")
    path.chmod(0o755)


def _run(workspace: Path, *args: str):
    environment = dict(os.environ, APP_CONFIG_ROOT=str(workspace / "config"))
    result = subprocess.run(
        ["bash", "scripts/collect-once.sh", *args],
        cwd=workspace,
        capture_output=True,
        text=True,
        env=environment,
    )
    state = workspace / "incoming" / "last-collect.json"
    return result, json.loads(state.read_text()) if state.is_file() else None


def test_the_run_summary_lands_in_a_file_anyone_can_read(workspace: Path):
    _fake_worklog(
        workspace,
        body='echo \'{"counters": {"recurring_occurrences": 41}, "status": "success"}\'',
    )
    result, found = _run(workspace, "google-calendar", "--since", "2026-09-10")

    assert found["outcome"] == "ok"
    assert found["source"] == "google-calendar"
    assert found["summary"]["counters"]["recurring_occurrences"] == 41
    # And the operator still sees everything they saw before.
    assert "recurring_occurrences" in result.stdout


def test_a_failed_run_says_so_rather_than_leaving_the_last_success_in_place(workspace: Path):
    _fake_worklog(workspace, body='echo \'{"status": "success"}\'')
    _run(workspace, "google-calendar")
    _fake_worklog(workspace, body='echo "google token expired" >&2', status=1)
    result, found = _run(workspace, "google-calendar")

    assert found["outcome"] == "failed"
    assert found["exit_status"] == 1
    assert "summary" not in found
    assert any("google token expired" in line for line in found["tail"])
    assert result.returncode == 1


def test_output_that_is_not_json_is_kept_rather_than_dropped(workspace: Path):
    _fake_worklog(workspace, body='echo "collected 12 events"')
    _, found = _run(workspace, "notion")
    assert found["tail"] == ["collected 12 events"]


def test_the_last_object_is_the_run_not_the_first_source(workspace: Path):
    _fake_worklog(
        workspace,
        body="printf '%s\\n' '{\"source\": \"slack\"}' '{\"run\": \"whole\"}'",
    )
    _, found = _run(workspace, "slack")
    assert found["summary"] == {"run": "whole"}
