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


def _origin(repo: Path) -> None:
    """A bare remote, so the tick gets past its fast-forward check."""
    bare = repo.parent / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    _git(repo, "remote", "add", "origin", str(bare))
    _git(repo, "push", "-q", "origin", "main")


def _patch(repo: Path, name: str, message: str, line: str) -> None:
    """A real patch file, made the way the cloud side makes them."""
    work = repo.parent / "work"
    if not work.exists():
        subprocess.run(
            ["git", "clone", "-q", str(repo.parent / "origin.git"), str(work)],
            check=True,
        )
        _git(work, "config", "user.email", "t@t")
        _git(work, "config", "user.name", "t")
    (work / "b.txt").write_text(line)
    _git(work, "add", "b.txt")
    _git(work, "commit", "-qm", message)
    subprocess.run(
        ["git", "format-patch", "-1", "-o", str(repo / "incoming")],
        cwd=work,
        check=True,
        capture_output=True,
    )


def test_the_carrier_records_which_board_items_a_patch_named(repo: Path):
    """HK, 2026-09-14: 주인업는 작업은 킬 하자. 네가 모르는 작업은 없어야해.

    Three separate P0 items exist because I agreed to announce work on the
    board before starting it and then did not, three times. A rule held in
    memory is a rule kept only when nothing is urgent, so the carrier reads
    it off the commits instead: any `wi_` id in the applied messages is
    recorded, and the board audit can then ask about a patch that named
    none.
    """
    _origin(repo)
    (repo / "incoming").mkdir(exist_ok=True)
    _patch(repo, "0001", "Do the thing\n\nRefs wi_1bda10e6938ed059", "b\n")

    found = _tick(repo)

    assert found["applied"] == 1
    assert found["items"] == ["wi_1bda10e6938ed059"]


def test_a_patch_that_named_no_item_says_so_instead_of_being_refused(repo: Path):
    """Recorded, not blocked.

    Refusing would stop the carrier over bookkeeping, and a batch that halts
    work to enforce a note about work is the wrong trade -- especially this
    one, where the person it would block is the person who forgot. So the
    empty list is the signal, and it is in the same file everything else
    about a tick is in.
    """
    _origin(repo)
    (repo / "incoming").mkdir(exist_ok=True)
    _patch(repo, "0001", "Do the thing with no item", "b\n")

    found = _tick(repo)

    assert found["applied"] == 1, "the patch still lands"
    assert found["items"] == []


def test_every_rule_that_cost_a_day_is_written_down_not_remembered():
    """The three P0 items that said 「docs/agent-onboarding.md에 적는다」.

    Each was created after I broke the rule, and each stayed open while I
    agreed to it again. A rule that lives only in a conversation is a rule
    the next session does not have, and this project runs sessions that
    start cold by design.
    """
    doc = (
        Path(__file__).resolve().parents[1] / "docs" / "agent-onboarding.md"
    ).read_text(encoding="utf-8")
    for rule in (
        "Put the item on the board when you start",
        "incoming/last-run.json` first",
        "Measure a query that could be heavy before handing it over",
        "does not use the production shape",
        "count it",
    ):
        assert rule in doc, f"the rule about {rule!r} is only in somebody's memory"


def test_the_session_protocol_states_the_rules_a_session_needs():
    """One file four sessions can read, since none of them can read each other.

    2026-09-30: two sessions edited cli.py the same day without either being
    able to see the other, and the collision went unnoticed for 26 minutes.
    Measuring the patches afterwards showed the shape of it -- every patch
    that went through the carrier landed without a collision, and the one
    piece of work that bypassed it caused the stall.

    So the protocol is short and its rules are load-bearing. This test names
    them, because the failure mode for a document like this is quiet erosion
    during an unrelated edit.
    """
    doc = (
        Path(__file__).resolve().parents[1] / "docs" / "session-protocol.md"
    ).read_text(encoding="utf-8")
    for rule in (
        # Branch per session: the one rule that alone prevents the incident.
        "main 을 체크아웃하고 작업하지 않는다",
        # One road onto main, so everything is serialized and tested.
        "git format-patch -1 -o incoming/",
        # Whichever road onto main a session takes, the suite runs first.
        "푸시 전에 이 기계에서 전체 테스트가 초록이어야 한다",
        # Announce at the start, which three P0 items already asked for.
        "착수할 때 보드에 올린다",
        # Fetch before you start: the other half of the 2026-09-30 incident.
        "git fetch origin",
        # And the tier that re-checks whatever actually landed.
        "incoming/last-integration.json",
        "Refs wi_",
        # Read the receipt before theorising about a fix that "did not work".
        "incoming/last-run.json",
        # And the rule that protects the other sessions' unfinished work.
        "내가 하지 않은 작업을 덮지 않는다",
    ):
        assert rule in doc, f"the protocol no longer states: {rule!r}"


def test_the_onboarding_points_at_the_protocol():
    """A document nobody is sent to is a document nobody reads."""
    doc = (
        Path(__file__).resolve().parents[1] / "docs" / "agent-onboarding.md"
    ).read_text(encoding="utf-8")
    assert "docs/session-protocol.md" in doc
