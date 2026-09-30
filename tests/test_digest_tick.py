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
        'if [ "$1" = "blocks" ]; then\n'
        '  echo \'blocks={"blocks": 24641, "blocks_with_him": 3306, "thread_blocks": 1625}\'\n'
        "fi\n"
        'if [ "$1" = "embed" ]; then\n'
        '  echo \'embed={"candidates": 2924, "embedded": 2924, "model": "BAAI/bge-m3"}\'\n'
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


def test_the_blocks_and_their_vectors_are_built_nightly_before_the_digest(
    workspace: Path,
):
    """The step that was a command somebody typed, and so did not happen.

    Blocks were built on 2026-09-21 and the embedding was never run, so the
    precedent search spent nine days on an index of single messages -- the one
    measured to answer a question about deployment with "퇴근하고 운동중입니다".
    Nobody was wrong; it was nobody's job. A step that only works when
    somebody remembers to type it is not finished.

    Before the digest for the same reason the sweep is: both feed what the
    day is built from.
    """
    result, calls = _run(workspace)
    assert result.returncode == 0

    blocks = next(i for i, call in enumerate(calls) if call.startswith("blocks "))
    embed = next(i for i, call in enumerate(calls) if call.startswith("embed "))
    digest = next(
        i for i, call in enumerate(calls) if call.startswith("digest --catch-up")
    )
    assert blocks < embed < digest, "a block has to exist before it can be embedded"
    assert "--apply" in calls[blocks]
    assert "--blocks" in calls[embed] and "--apply" in calls[embed]
    # A cap, so an unexpected rebuild cannot turn the nightly batch into an
    # hour -- but a generous one, since 2,000 blocks took 38 seconds.
    assert "--limit 5000" in calls[embed]


def test_the_batch_says_how_many_blocks_are_still_unembedded(workspace: Path):
    """The one number worth reading the next morning.

    Anything but zero means the cap was hit or the model did not load, and a
    precedent search over a half-filled index answers confidently from
    whichever half it has -- which looks exactly like an answer.
    """
    _run(workspace)
    found = json.loads((workspace / "incoming" / "last-blocks.json").read_text())
    assert found["blocks"]["thread_blocks"] == 1625
    assert found["embed"]["embedded"] == 2924
    assert found["unembedded_after"] == 0


def test_the_embedding_can_be_turned_off_without_editing_the_batch(workspace: Path):
    """It loads a 2GB model onto the GPU. Somebody will need it off one night."""
    _, calls = _run(workspace, WORKLOG_EMBED_MAX="0")
    assert not any(call.startswith("embed ") for call in calls)
    assert any(call.startswith("digest --catch-up") for call in calls)
