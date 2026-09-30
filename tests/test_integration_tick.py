"""The gate, moved to where everything converges.

`incoming-tick.sh` tests before it pushes, so for as long as every change
arrived as a patch the testing happened by accident of the route. On
2026-09-30 a session committed to main on this machine instead; two tests went
red and the carrier stopped pushing, which was right and only worked because
that work happened to pass through the carrier at all.

HK, 2026-09-30: 각자의 작업공간에서 작업하고, 깃헙에 푸시하고, 풀은 이 pc가
서버니까 여기로 한다. Under that flow the carrier is off the path, so the gate
has to sit on whatever is in origin/main.

The behaviours worth holding are all refusals: refuse without a database,
refuse to reset a clone that has diverged, and never call a run green that
skipped the tests the database was for.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "integration-tick.sh"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture()
def stage(tmp_path: Path) -> dict:
    """An origin, a local repo with the script, and somewhere to clone into."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)

    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    _git(seed, "config", "user.email", "t@t")
    _git(seed, "config", "user.name", "t")
    (seed / "a.txt").write_text("one\n")
    _git(seed, "add", "a.txt")
    _git(seed, "commit", "-qm", "first")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "-q", "origin", "main")

    local = tmp_path / "repo"
    local.mkdir()
    _git(local, "init", "-q", "-b", "main")
    _git(local, "remote", "add", "origin", str(origin))
    (local / "scripts").mkdir()
    shutil.copy(SCRIPT, local / "scripts" / "integration-tick.sh")
    return {"origin": origin, "local": local, "work": tmp_path / "integration"}


def _run(stage: dict, **env: str) -> dict:
    environment = {**os.environ, "WORKLOG_INTEGRATION_ROOT": str(stage["work"]), **env}
    subprocess.run(
        ["bash", "scripts/integration-tick.sh"],
        cwd=stage["local"],
        capture_output=True,
        env=environment,
    )
    return json.loads((stage["local"] / "incoming" / "last-integration.json").read_text())


def test_without_a_database_it_refuses_rather_than_skipping(stage) -> None:
    """The failure this tier exists to prevent, in its own batch.

    With no `WORKLOG_TEST_DATABASE_URL` the suite still passes -- it just
    skips every test that needs a database, 91 of them on 2026-09-30: the
    ledger, the projection, the conversation blocks, the pairing. A run that
    skipped those and reported green would be exactly the false green this
    project has already lost days to, so the absence is an outcome rather
    than a quiet default.
    """
    found = _run(stage, WORKLOG_INTEGRATION_DATABASE_URL="")
    assert found["outcome"] == "unconfigured"
    assert "skip" in found["detail"]


def test_a_diverged_clone_is_reported_not_reset(stage) -> None:
    """A batch that resets a worktree every 30 minutes erases work.

    If the integration clone has commits origin/main does not, somebody put
    them there. Saying so costs one line in a state file; `git reset --hard`
    on a timer costs whatever they were.
    """
    work = stage["work"]
    subprocess.run(
        ["git", "clone", "-q", str(stage["origin"]), str(work)], check=True
    )
    _git(work, "config", "user.email", "t@t")
    _git(work, "config", "user.name", "t")
    (work / "local-only.txt").write_text("somebody was here\n")
    _git(work, "add", "local-only.txt")
    _git(work, "commit", "-qm", "local work nobody pushed")

    found = _run(stage, WORKLOG_INTEGRATION_DATABASE_URL="postgresql://example/dev")

    assert found["outcome"] == "diverged"
    assert (work / "local-only.txt").exists(), "the batch did not erase it"


def test_it_clones_on_the_first_run_and_records_what_it_tested(stage) -> None:
    """The head is in the report, so "green" always names a commit.

    "The tests pass" is not a fact anybody can act on; "the tests pass on
    0a40080" is. This project has already spent a morning on the gap between
    what is committed and what is running.
    """
    found = _run(stage, WORKLOG_INTEGRATION_DATABASE_URL="postgresql://example/dev")

    assert (stage["work"] / ".git").exists(), "first run clones"
    assert found["head"] != "unknown"
    expected = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=stage["work"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert found["head"] == expected


def test_a_second_run_does_not_reclone(stage) -> None:
    """Idempotent, because it runs every half hour forever."""
    _run(stage, WORKLOG_INTEGRATION_DATABASE_URL="postgresql://example/dev")
    marker = stage["work"] / ".marker"
    marker.write_text("x")
    _run(stage, WORKLOG_INTEGRATION_DATABASE_URL="postgresql://example/dev")
    assert marker.exists()


def test_the_unit_reads_the_url_from_outside_the_repository() -> None:
    """The one rule with no exceptions: no collected data, and no secrets.

    The integration database URL holds a password, so it lives beside the
    collection's own URL in ~/.config and the unit reads it from there. A
    leading `-` so a missing file does not stop the unit from starting --
    the tick then refuses for a reason it can name, which is better than a
    unit that fails before writing any state at all.
    """
    unit = (ROOT / "deploy" / "systemd" / "hkwa-integration.service").read_text()
    assert "EnvironmentFile=-%h/.config/hk-work-assistant/collect.env" in unit
    # Naming the variable in a comment is fine; assigning it here is not.
    # The check is on the assignment, because that is the shape a password
    # would take if somebody ever put one in.
    assert "Environment=WORKLOG_INTEGRATION_DATABASE_URL" not in unit, (
        "the URL itself is never in the repository"
    )
    assert "postgresql://" not in unit

    timer = (ROOT / "deploy" / "systemd" / "hkwa-integration.timer").read_text()
    assert "OnUnitInactiveSec=30min" in timer


def test_it_refuses_to_test_one_checkout_with_another_s_interpreter(stage) -> None:
    """The first run of this batch reported three failures that meant nothing.

    It fell back to the carrier's `.venv`, which holds an editable install
    pointing at the carrier's working tree -- so it imported code from one
    checkout and collected tests from another. Three reds that described
    neither. The same mistake this repository keeps finding, two sides asking
    different questions, this time inside the batch built to catch it.

    There is no fallback now. Either the clone has its own interpreter with
    the package installed from the clone, or the run says it did not test
    anything. An untested run that says so is worth more than a result
    assembled from two checkouts.
    """
    work = stage["work"]
    subprocess.run(
        ["git", "clone", "-q", str(stage["origin"]), str(work)], check=True
    )
    # A virtualenv that exists but whose interpreter resolves the package
    # somewhere else -- which is exactly what the carrier's venv looks like
    # from in here, because its editable install points at the carrier's own
    # working tree. The stub only has to answer the import check the way that
    # interpreter would.
    venv = work / ".venv" / "bin"
    venv.mkdir(parents=True)
    shim = venv / "python"
    shim.write_text(
        "#!/bin/sh\n"
        "echo /somewhere/else/rlwrld_worklog/__init__.py\n"
        "exit 0\n"
    )
    shim.chmod(0o755)

    found = _run(stage, WORKLOG_INTEGRATION_DATABASE_URL="postgresql://example/dev")

    assert found["outcome"] == "untested"
    assert found["passed"] == 0, "nothing may be counted as passing"


def test_a_red_run_says_why_not_only_which(stage) -> None:
    """A report that names failures without their reasons costs a round trip.

    This batch reports to somebody who is not at this keyboard. "Three tests
    failed" sends them to a terminal; the assertion line often does not.
    """
    script = SCRIPT.read_text(encoding="utf-8")
    assert "grep -E '^E '" in script, (
        "the red detail carries pytest's assertion lines, not just the "
        "FAILED names"
    )
