"""What has been deployed, read from what the deploy batch leaves behind.

HK, 2026-10-06: 배포 히스토리를 알아야 내가 딴 이야기를 안할거같아.

That sentence is the requirement. `incoming/last-deploy.json` says what runs
now and is overwritten every ten minutes, so three times in two days he
reasoned from a version that had already been replaced -- each time costing a
round trip to discover. These tests are mostly about the ways a log read by a
screen can mislead rather than fail: a truncated line, a stale clock, a
hundred lines of nothing burying the four that matter.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from rlwrld_worklog import release_notes


@pytest.fixture()
def deploy(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("RAW_ARCHIVE_ROOT", str(tmp_path))
    directory = tmp_path / "deploy"
    directory.mkdir()
    return directory


def _line(**fields) -> str:
    return json.dumps(fields, ensure_ascii=False)


def test_no_log_yet_is_said_plainly_rather_than_shown_as_empty(deploy) -> None:
    """A machine that has not deployed since this shipped has no file.

    An empty table with no explanation looks exactly like a query that
    failed, which is the reading that sends somebody to the terminal.
    """
    found = release_notes.read()
    assert found["current"] is None
    assert found["entries"] == []
    assert "아직 기록이 없습니다" in found["reason"]


def test_the_history_is_newest_first_with_the_subject_attached(deploy) -> None:
    """A seven-character sha on its own is not something anybody can act on."""
    (deploy / "deploy-log.jsonl").write_text(
        "\n".join(
            [
                _line(
                    finished_at="2026-10-06T01:00:00Z",
                    outcome="deployed",
                    head="aaaaaaa",
                    subject="첫 배포",
                ),
                _line(
                    finished_at="2026-10-06T03:20:00Z",
                    outcome="deployed",
                    head="bbbbbbb",
                    subject="두 번째",
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    found = release_notes.read()
    assert [entry["head"] for entry in found["entries"]] == ["bbbbbbb", "aaaaaaa"]
    assert found["entries"][0]["subject"] == "두 번째"
    assert found["landed"] == 2


def test_times_are_shown_in_the_clock_he_reads(deploy) -> None:
    """The log is written in UTC; 03:20Z is 12:20 the same day in Seoul.

    Worth asserting rather than assuming: a deploy history whose times are
    nine hours off is worse than none, because it looks authoritative.
    """
    (deploy / "current.json").write_text(
        _line(finished_at="2026-10-06T03:20:00Z", outcome="deployed", head="bbbbbbb"),
        encoding="utf-8",
    )
    assert release_notes.read()["current"]["at_kst"] == "2026-10-06 12:20"


def test_one_truncated_line_does_not_take_the_history_with_it(deploy) -> None:
    """A shell script appends this file, so a crash can leave half a line.

    The line you lost is exactly the one you wanted the others to explain.
    """
    (deploy / "deploy-log.jsonl").write_text(
        _line(finished_at="2026-10-06T01:00:00Z", outcome="deployed", head="aaaaaaa")
        + '\n{"started_at": "2026-10-06T02:0\n'
        + _line(finished_at="2026-10-06T03:00:00Z", outcome="deployed", head="ccccccc")
        + "\n",
        encoding="utf-8",
    )
    assert [entry["head"] for entry in release_notes.read()["entries"]] == [
        "ccccccc",
        "aaaaaaa",
    ]


def test_an_outcome_nobody_translated_shows_as_itself(deploy) -> None:
    """A new outcome in the batch must not render as a blank cell."""
    (deploy / "current.json").write_text(
        _line(finished_at="2026-10-06T03:00:00Z", outcome="something-new", head="d"),
        encoding="utf-8",
    )
    current = release_notes.read()["current"]
    assert current["outcome_label"] == "something-new"
    assert current["landed"] is False


def test_only_a_real_deploy_counts_as_one(deploy) -> None:
    """`current` means the batch found nothing to do. It is not a release."""
    (deploy / "deploy-log.jsonl").write_text(
        "\n".join(
            [
                _line(finished_at="2026-10-06T01:00:00Z", outcome="current", head="a"),
                _line(finished_at="2026-10-06T02:00:00Z", outcome="blocked", head="b"),
                _line(finished_at="2026-10-06T03:00:00Z", outcome="deployed", head="c"),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    assert release_notes.read()["landed"] == 1


# ------------------------------------------------------------ the batch side


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "deploy-tick.sh"


def test_the_deploy_script_is_valid_shell() -> None:
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0


def test_an_unchanged_tick_appends_nothing() -> None:
    """The batch runs every ten minutes and almost always finds nothing.

    Logging those would bury the few lines a day that mean something under a
    hundred that do not -- and a history nobody can skim is a history nobody
    reads, which is the problem this was built to fix.
    """
    source = SCRIPT.read_text(encoding="utf-8")
    assert '"$was" != "$head_sha $outcome"' in source
    assert "deploy-log.jsonl" in source


def test_the_log_is_written_where_the_app_can_read_it() -> None:
    """The container mounts the data root read-only and not this checkout.

    A log written next to the script would be invisible to the screen that
    exists to show it.
    """
    source = SCRIPT.read_text(encoding="utf-8")
    assert 'RAW_ARCHIVE_HOST_ROOT:-/data/rlwrld-worklog}/deploy' in source
    assert release_notes.deploy_dir().name == "deploy"


def test_the_repository_copy_still_gets_written() -> None:
    """Scripts and sessions read incoming/last-deploy.json; it stays."""
    source = SCRIPT.read_text(encoding="utf-8")
    assert '> "$state"' in source


def test_running_the_batch_actually_writes_the_two_files(tmp_path, monkeypatch) -> None:
    """The assertions above read the script; this one runs it.

    In a throwaway repository the tick stops early -- no verdict from
    통합데브, or no docker -- and goes straight to `finish`, which is the part
    under test: every exit path writes the current verdict and appends when
    it changed. A log that only works on the success path is empty exactly
    when you need it.
    """
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "scripts" / "deploy-tick.sh").write_text(
        SCRIPT.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (repo / "README.md").write_text("x", encoding="utf-8")
    for command in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "t@example.com"],
        ["git", "config", "user.name", "t"],
        ["git", "add", "-A"],
        ["git", "commit", "-qm", "배포 기록을 남긴다"],
    ):
        subprocess.run(command, cwd=repo, check=True)

    data = tmp_path / "data"
    environment = {
        "PATH": "/usr/bin:/bin",  # deliberately without docker
        "HOME": str(tmp_path),
        "RAW_ARCHIVE_HOST_ROOT": str(data),
    }
    subprocess.run(
        ["bash", "scripts/deploy-tick.sh"], cwd=repo, env=environment, check=False
    )

    monkeypatch.setenv("RAW_ARCHIVE_ROOT", str(data))
    found = release_notes.read()
    assert found["current"]["outcome"] in release_notes.OUTCOMES, (
        "an exit path the screen cannot name"
    )
    assert found["current"]["landed"] is False, "nothing was deployed here"
    assert found["current"]["subject"] == "배포 기록을 남긴다", "the sha alone is not a note"
    assert len(found["entries"]) == 1

    # The same verdict again adds nothing.
    subprocess.run(
        ["bash", "scripts/deploy-tick.sh"], cwd=repo, env=environment, check=False
    )
    assert len(release_notes.read()["entries"]) == 1

    # The repository's own copy is still written, for the scripts that read it.
    assert json.loads((repo / "incoming" / "last-deploy.json").read_text())["outcome"] == (
        found["current"]["outcome"]
    )
