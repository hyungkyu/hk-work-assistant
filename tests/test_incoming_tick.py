"""The carrier's refusal has to name the blockage, not just report one.

2026-09-16: a patch queue sat blocked from 10:48 to the evening and every tick
wrote the same line — "worktree is dirty" — with no file and no duration. I
read that line three times and passed over it, then theorised about
performance while HK ran hours-old code. A refusal that reads identically on
minute one and minute two hundred is not a signal.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "incoming-tick.sh"


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
    shutil.copy(SCRIPT, repo / "scripts" / "incoming-tick.sh")
    return repo


def _tick(repo: Path) -> dict:
    subprocess.run(["bash", "scripts/incoming-tick.sh"], cwd=repo, capture_output=True)
    return json.loads((repo / "incoming" / "last-run.json").read_text())


def test_a_dirty_worktree_refusal_names_the_files_and_the_wait(repo: Path):
    (repo / "incoming").mkdir()
    (repo / "incoming" / "0001-x.patch").write_text("not a real patch\n")
    (repo / "a.txt").write_text("changed by somebody\n")

    found = _tick(repo)

    assert found["outcome"] == "refused"
    # Which file. Without this the blockage is anonymous and nobody owns it.
    assert "a.txt" in found["detail"]
    # Since when. The number is what makes the second reading different from
    # the first.
    assert "dirty since" in found["detail"]
    assert "m);" in found["detail"]
    assert (repo / "incoming" / ".blocked-since").exists()


def test_the_blocked_clock_starts_once_and_keeps_running(repo: Path):
    (repo / "incoming").mkdir()
    (repo / "incoming" / "0001-x.patch").write_text("x\n")
    (repo / "a.txt").write_text("changed\n")

    _tick(repo)
    first = (repo / "incoming" / ".blocked-since").read_text()
    _tick(repo)
    # A tick that restarted the clock would report "0m" forever, which is the
    # bug this file exists to prevent.
    assert (repo / "incoming" / ".blocked-since").read_text() == first


def test_a_clean_tree_clears_the_marker(repo: Path):
    (repo / "incoming").mkdir()
    (repo / "incoming" / "0001-x.patch").write_text("x\n")
    (repo / "a.txt").write_text("changed\n")
    _tick(repo)
    assert (repo / "incoming" / ".blocked-since").exists()

    _git(repo, "checkout", "--", "a.txt")
    _tick(repo)
    assert not (repo / "incoming" / ".blocked-since").exists()


def test_an_empty_queue_is_not_reported_as_a_blockage(repo: Path):
    (repo / "a.txt").write_text("changed\n")
    found = _tick(repo)
    assert found["outcome"] == "idle"
