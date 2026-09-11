"""run-logged.sh's start marker: written at start, removed only by a finish.

`last.json` alone made a running or killed batch invisible -- it is written at
the end, so a run that was Ctrl-C'd left nothing at all. These tests run the
real script, in a directory whose path contains a space because the production
checkout's does.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPOSITORY = Path(__file__).resolve().parents[1]


@pytest.fixture()
def spaced(tmp_path: Path) -> dict[str, Path]:
    root = tmp_path / "RLWRLD workspace"
    (root / "scripts").mkdir(parents=True)
    shutil.copy2(REPOSITORY / "scripts" / "run-logged.sh", root / "scripts" / "run-logged.sh")
    logs = tmp_path / "logs"
    return {"root": root, "logs": logs}


def _run(spaced: dict[str, Path], name: str, *command: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(spaced["root"] / "scripts" / "run-logged.sh"), name, "--", *command],
        env={"PATH": "/usr/bin:/bin", "WORKLOG_LOG_ROOT": str(spaced["logs"]), "HOME": "/tmp"},
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_the_marker_exists_while_the_command_runs_and_is_gone_after(spaced) -> None:
    marker = spaced["logs"] / "t" / "running.json"
    # The command under test checks for its own start marker: if the marker
    # were written late or not at all, this run would fail.
    result = _run(spaced, "t", "bash", "-c", f'test -f "{marker}"')
    assert result.returncode == 0
    assert not marker.exists()
    state = json.loads((spaced["logs"] / "t" / "last.json").read_text(encoding="utf-8"))
    assert state["outcome"] == "ok"


def test_the_marker_names_the_run_it_belongs_to(spaced) -> None:
    marker = spaced["logs"] / "t" / "running.json"
    result = _run(spaced, "t", "bash", "-c", f'cat "{marker}"')
    assert result.returncode == 0
    log = (spaced["logs"] / "t" / "latest.log").read_text(encoding="utf-8")
    captured = json.loads(log.split("\n\n", 1)[1].rsplit("\n=== exit", 1)[0])
    assert captured["command"].startswith("bash -c")
    assert isinstance(captured["pid"], int)
    assert captured["started_at"].endswith("Z")
    assert Path(captured["log"]).parent == spaced["logs"] / "t"


def test_a_leftover_marker_is_noted_in_the_next_runs_log(spaced) -> None:
    """A marker with no finish is evidence; replacing it silently would destroy it."""
    directory = spaced["logs"] / "t"
    directory.mkdir(parents=True)
    (directory / "running.json").write_text(
        json.dumps({"started_at": "2026-09-08T05:00:00Z", "pid": 1, "command": "x", "log": "y"}),
        encoding="utf-8",
    )
    result = _run(spaced, "t", "true")
    assert result.returncode == 0
    log = (directory / "latest.log").read_text(encoding="utf-8")
    assert "previous run left a running marker" in log
    assert "2026-09-08T05:00:00Z" in log
    assert not (directory / "running.json").exists()


def test_a_failing_command_still_removes_the_marker_and_records_the_failure(spaced) -> None:
    result = _run(spaced, "t", "bash", "-c", "exit 3")
    assert result.returncode == 3
    assert not (spaced["logs"] / "t" / "running.json").exists()
    state = json.loads((spaced["logs"] / "t" / "last.json").read_text(encoding="utf-8"))
    assert state["outcome"] == "failed"
    assert state["exit_code"] == 3
