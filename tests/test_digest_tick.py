"""The nightly batch, including the sweep step added on 2026-09-18.

Run against a fake `worklog` so the ordering and the bounds are checked
without touching a database or Slack.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "digest-tick.sh"


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "scripts").mkdir()
    shutil.copy(SCRIPT, tmp_path / "scripts" / "digest-tick.sh")
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    config = tmp_path / "config"
    config.mkdir()
    (config / "collect.env").write_text("DATABASE_URL=postgresql://fake\n")
    worklog = tmp_path / ".venv" / "bin" / "worklog"
    worklog.write_text(
        "#!/usr/bin/env bash\n"
        'echo "$@" >> "$PWD/calls.txt"\n'
        'if [ "$1" = "slack-thread-sweep" ]; then\n'
        '  echo \'slack_thread_sweep={"ok": true, "channels": 3, "parents": 150}\'\n'
        "fi\n"
        "exit 0\n"
    )
    worklog.chmod(0o755)
    # The script checks for openpyxl in the batch environment.
    python = tmp_path / ".venv" / "bin" / "python"
    python.write_text("#!/usr/bin/env bash\nexit 0\n")
    python.chmod(0o755)
    return tmp_path


def _run(workspace: Path, **environment: str):
    result = subprocess.run(
        ["bash", "scripts/digest-tick.sh"],
        cwd=workspace,
        capture_output=True,
        text=True,
        env=dict(
            os.environ,
            APP_CONFIG_ROOT=str(workspace / "config"),
            WORKLOG_DIGEST_OUT=str(workspace / "out"),
            **environment,
        ),
    )
    calls = (workspace / "calls.txt").read_text().splitlines()
    return result, calls


def test_the_sweep_runs_bounded_and_before_the_digest(workspace: Path):
    """Recovered parents have to be in the ledger before the day is built."""
    result, calls = _run(workspace)
    assert result.returncode == 0

    sweep = next(index for index, call in enumerate(calls) if call.startswith("slack-thread-sweep"))
    digest = next(index for index, call in enumerate(calls) if call.startswith("digest --catch-up"))
    assert sweep < digest
    assert "--max-parents 150" in calls[sweep]
    assert "--apply" in calls[sweep]


def test_the_sweep_leaves_its_answer_in_a_file(workspace: Path):
    _run(workspace)
    found = json.loads((workspace / "incoming" / "last-sweep.json").read_text())
    assert found["summary"] == {"ok": True, "channels": 3, "parents": 150}


def test_the_sweep_can_be_turned_off_without_editing_the_batch(workspace: Path):
    _, calls = _run(workspace, WORKLOG_SWEEP_MAX="0")
    assert not any(call.startswith("slack-thread-sweep") for call in calls)


def test_a_failing_sweep_does_not_stop_the_digest(workspace: Path):
    """A network step that fails must not cost the day its digest."""
    worklog = workspace / ".venv" / "bin" / "worklog"
    worklog.write_text(
        "#!/usr/bin/env bash\n"
        'echo "$@" >> "$PWD/calls.txt"\n'
        'if [ "$1" = "slack-thread-sweep" ]; then echo "rate limited" >&2; exit 1; fi\n'
        "exit 0\n"
    )
    worklog.chmod(0o755)

    result, calls = _run(workspace)
    assert result.returncode == 1, "the run reports the failure"
    assert any(call.startswith("digest --catch-up") for call in calls), (
        "and still builds the digest"
    )
