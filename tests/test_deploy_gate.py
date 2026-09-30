"""Production takes a commit 통합데브 called green, and nothing else.

HK, 2026-09-30: 업무목록 -> 깃헙 -> 데브 -> 프로덕션. Until this, the last
arrow carried no condition. The deploy batch built whatever was on main, and
the integration run that says whether main works was a report nobody was
obliged to read.

A gate that only reports is a gate that is open. These tests hold the three
answers it has to give: green deploys, red does not, and a commit nobody has
tested yet waits instead of failing -- integration runs on its own timer and
will reach it, so "not yet" is a state and not an error.

The build itself needs docker and is not exercised here; what is exercised is
everything the batch decides before it would reach one.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "deploy-tick.sh"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("one\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-qm", "first")
    (repo / "scripts").mkdir()
    shutil.copy(SCRIPT, repo / "scripts" / "deploy-tick.sh")
    (repo / "incoming").mkdir()
    # A `docker` that exists and does nothing, so the batch gets past its
    # availability check and reaches the decision this file is about.
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "docker").write_text("#!/bin/sh\nexit 0\n")
    (fake / "docker").chmod(0o755)
    return repo


def _head(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _verdict(repo: Path, *, head: str, outcome: str) -> None:
    (repo / "incoming" / "last-integration.json").write_text(
        json.dumps({"head": head, "outcome": outcome, "passed": 1553})
    )


def _tick(repo: Path) -> dict:
    import os

    environment = {**os.environ, "PATH": f"{repo.parent / 'bin'}:{os.environ['PATH']}"}
    subprocess.run(
        ["bash", "scripts/deploy-tick.sh"],
        cwd=repo,
        capture_output=True,
        env=environment,
    )
    return json.loads((repo / "incoming" / "last-deploy.json").read_text())


def test_a_commit_the_integration_called_red_is_not_deployed(repo: Path) -> None:
    """The whole point. On 2026-09-30 two tests were red on main for hours."""
    _verdict(repo, head=_head(repo), outcome="red")
    found = _tick(repo)
    assert found["outcome"] == "blocked"
    assert "red" in found["detail"]


def test_a_commit_nobody_has_tested_yet_waits_rather_than_failing(repo: Path) -> None:
    """"Not yet" is a state, not a breakage.

    Integration runs on its own timer, so a commit pushed a minute ago has no
    verdict and will have one shortly. Reporting that as a failure would teach
    whoever reads it to ignore this file, which is how the 26-minute stall
    went unnoticed in the first place.
    """
    _verdict(repo, head="0000000", outcome="green")
    found = _tick(repo)
    assert found["outcome"] == "awaiting-verification"
    assert _head(repo) in found["detail"], "it names the commit it is waiting on"


def test_a_missing_verdict_is_not_treated_as_permission(repo: Path) -> None:
    """No file at all must not read as "go ahead".

    An absent report and a passing one are different answers. This project has
    already lost days to a batch that reported ok while writing nothing.
    """
    found = _tick(repo)
    assert found["outcome"] == "awaiting-verification"


def test_an_untested_verdict_is_not_green(repo: Path) -> None:
    """`untested` means the integration run could not run the suite.

    It is the outcome the integration batch reports when it has no database or
    cannot build its own interpreter -- precisely the states in which a green
    would be meaningless. Treating anything but `green` as green would undo
    both gates at once.
    """
    _verdict(repo, head=_head(repo), outcome="untested")
    found = _tick(repo)
    assert found["outcome"] == "blocked"


def test_a_dirty_tree_still_stops_it_before_any_of_this(repo: Path) -> None:
    """The older refusal, kept: an image nobody can name is not deployable."""
    _verdict(repo, head=_head(repo), outcome="green")
    (repo / "a.txt").write_text("changed by somebody\n")
    found = _tick(repo)
    assert found["outcome"] == "refused"
    assert "dirty" in found["detail"]


def test_the_integration_timer_is_short_enough_to_be_waited_on() -> None:
    """Production now waits for this verdict, so the interval is the delay.

    Thirty minutes was fine for a report. As a gate it is half an hour between
    a fix being green and it reaching the running system, which is long enough
    that somebody starts deploying by hand.
    """
    timer = (ROOT / "deploy" / "systemd" / "hkwa-integration.timer").read_text()
    assert "OnUnitInactiveSec=10min" in timer
