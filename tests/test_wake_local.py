"""`scripts/wake-local.sh`: one board item, one session, one honest outcome.

The script itself is run, with `bash`, exactly as `hkwa-wake.service` runs it.
What is replaced is the one thing that would start a real agent: a stub `claude`
goes in front of `PATH`, records every argument it was handed, and edits the
board only when the test asked it to. Everything else -- the picker, the
cooling-off ledger, the board read after the session, the state file -- is the
real thing, and the prompt is the real `scripts/local-work-prompt.md`.

It follows `tests/test_backfill_days.py`: a few helpers and no framework.

The incident these pin: the allowlist granted `Bash(work:*)`, a command that
exists on no machine, while the prompt told the session to run `worklog`. Every
board write was refused, the item never moved, `outcome` said `woke` anyway, and
the picker handed the same unclaimable item to the next tick three hundred times
while newer work sat behind it.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "wake-local.sh"

OLDEST = "item-oldest"
NEWER = "item-newer"

# A stub session that does nothing at all -- the shape of every tick in the
# incident: the CLI ran, exited zero, and the board was exactly as it was.
DOES_NOTHING = "exit 0\n"


def claims(item_id: str, tmp_path: Path) -> str:
    """A stub session that performs the one board write the prompt asks for."""
    return (
        f'python3 - "{item_id}" <<PY\n'
        "import json, sys\n"
        f'path = "{tmp_path}/config/work/items.json"\n'
        'doc = json.load(open(path, encoding="utf-8"))\n'
        'for item in doc["items"]:\n'
        '    if item["id"] == sys.argv[1]:\n'
        '        item["status"] = "in_progress"\n'
        '        item["revision"] += 1\n'
        'json.dump(doc, open(path, "w", encoding="utf-8"))\n'
        "PY\n"
    )


def board(tmp_path: Path, *items: dict, executor: str = "local") -> Path:
    """Write a board. With no arguments: two ready items, oldest first."""
    if not items:
        items = (
            {"id": OLDEST, "created_at": "2026-09-01T00:00:00Z"},
            {"id": NEWER, "created_at": "2026-09-02T00:00:00Z"},
        )
    path = tmp_path / "config" / "work" / "items.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "revision": 671,
                "updated_at": "2026-09-06T00:00:00Z",
                "items": [
                    {
                        "status": "ready",
                        "assigned_to": executor,
                        "revision": 3,
                        "archived_at": None,
                        **item,
                    }
                    for item in items
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def stub_bin(tmp_path: Path, body: str) -> Path:
    """A directory holding a fake `claude` with the given body, and a `worklog`."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    claude = bin_dir / "claude"
    claude.write_text(
        "#!/usr/bin/env bash\n"
        "index=0\n"
        'for argument in "$@"; do\n'
        "  index=$((index+1))\n"
        f'  printf "%s" "$argument" > "{tmp_path}/argv-$index"\n'
        "done\n"
        f'printf "%s" "$PATH" > "{tmp_path}/session-path"\n'
        f"{body}",
        encoding="utf-8",
    )
    claude.chmod(0o755)
    # `worklog` only has to exist for the script's refusal check; no stub
    # session here ever runs it.
    worklog = bin_dir / "worklog"
    worklog.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    worklog.chmod(0o755)
    return bin_dir


def wake(tmp_path: Path, *, session: str = DOES_NOTHING, script: Path = SCRIPT, **env_extra):
    """Run one tick the way the systemd unit runs it."""
    bin_dir = stub_bin(tmp_path, session)
    environment = dict(os.environ)
    environment.update(
        PATH=f"{bin_dir}:{environment['PATH']}",
        APP_CONFIG_ROOT=str(tmp_path / "config"),
        WAKE_STATE_DIR=str(tmp_path / "state"),
        WAKE_TIMEOUT="60",
    )
    environment.update({key: str(value) for key, value in env_extra.items()})
    return subprocess.run(
        ["bash", str(script)], capture_output=True, text=True, env=environment, timeout=120
    )


def last_wake(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "state" / "last-wake.json").read_text(encoding="utf-8"))


def ledger(tmp_path: Path) -> dict:
    path = tmp_path / "state" / "wake-skips.json"
    return json.loads(path.read_text(encoding="utf-8"))["skips"] if path.exists() else {}


def session_argv(tmp_path: Path) -> dict[str, str]:
    """What the script actually handed `claude`, by flag."""
    arguments = []
    index = 1
    while (tmp_path / f"argv-{index}").exists():
        arguments.append((tmp_path / f"argv-{index}").read_text(encoding="utf-8"))
        index += 1
    return {
        "prompt": arguments[arguments.index("-p") + 1],
        "allowed": arguments[arguments.index("--allowedTools") + 1],
    }


# ------------------------------------------------ did the board actually move?


def test_a_session_that_leaves_the_item_where_it_was_is_not_a_wake(tmp_path: Path) -> None:
    """`claude` exiting zero says the CLI ended, not that anything happened."""
    board(tmp_path)
    wake(tmp_path)
    state = last_wake(tmp_path)
    assert state["outcome"] == "unclaimed"
    assert state["item_id"] == OLDEST
    assert "still ready 3" in state["detail"]


def test_a_session_that_claims_the_item_is_a_wake(tmp_path: Path) -> None:
    board(tmp_path)
    wake(tmp_path, session=claims(OLDEST, tmp_path))
    state = last_wake(tmp_path)
    assert state["outcome"] == "woke"
    assert state["item_id"] == OLDEST


def test_a_claimed_item_is_not_cooled_off(tmp_path: Path) -> None:
    """It moved, so the next tick has no reason to pass it over."""
    board(tmp_path)
    wake(tmp_path, session=claims(OLDEST, tmp_path))
    assert ledger(tmp_path) == {}
    assert last_wake(tmp_path)["skipped"] == []


def test_an_item_that_leaves_the_board_counts_as_moved(tmp_path: Path) -> None:
    """Archived or deleted is movement; only standing still is not."""
    board(tmp_path, {"id": OLDEST, "created_at": "2026-09-01T00:00:00Z"})
    wake(
        tmp_path,
        session=(
            f'python3 -c \'import json; path="{tmp_path}/config/work/items.json"; '
            'doc=json.load(open(path)); doc["items"]=[]; json.dump(doc, open(path,"w"))\'\n'
        ),
    )
    assert last_wake(tmp_path)["outcome"] == "woke"


def test_a_board_that_cannot_be_re_read_is_still_not_a_wake(tmp_path: Path) -> None:
    """Not knowing whether the item moved is not the same as knowing it did."""
    board(tmp_path)
    wake(tmp_path, session=f'printf "{{" > "{tmp_path}/config/work/items.json"\n')
    state = last_wake(tmp_path)
    assert state["outcome"] == "unclaimed"
    assert "could not be re-read" in state["detail"]
    assert ledger(tmp_path) == {}, "nothing was learned, so nothing is held against it"


def test_a_failed_session_keeps_its_own_outcome_and_still_cools_the_item(
    tmp_path: Path,
) -> None:
    """`session-failed` says more than `unclaimed`; the starvation is the same."""
    board(tmp_path)
    wake(tmp_path, session="exit 3\n")
    assert last_wake(tmp_path)["outcome"] == "session-failed"
    assert OLDEST in ledger(tmp_path)


# ------------------------------------------------------------ the queue moves


def test_the_next_tick_picks_a_different_item(tmp_path: Path) -> None:
    """The whole point: one unclaimable item must not freeze the queue."""
    board(tmp_path)
    wake(tmp_path)
    assert last_wake(tmp_path)["item_id"] == OLDEST

    wake(tmp_path)
    assert last_wake(tmp_path)["item_id"] == NEWER


def test_a_skipped_item_is_named_in_the_state_file(tmp_path: Path) -> None:
    """Passed over is not the same as gone, so the tick says which and why."""
    board(tmp_path)
    wake(tmp_path)
    first = last_wake(tmp_path)["skipped"]
    assert [entry["item_id"] for entry in first] == [OLDEST]
    assert first[0]["attempts"] == 1
    assert "left the item at ready" in first[0]["reason"]

    # The second tick passes the first item over and then fails on the second,
    # so both are named: one still cooling, one newly cooled.
    wake(tmp_path)
    state = last_wake(tmp_path)
    assert [entry["item_id"] for entry in state["skipped"]] == [OLDEST, NEWER]
    assert state["item_id"] == NEWER


def test_the_cooling_off_holds_for_the_whole_period(tmp_path: Path) -> None:
    board(tmp_path, {"id": OLDEST, "created_at": "2026-09-01T00:00:00Z"})
    wake(tmp_path)
    assert last_wake(tmp_path)["outcome"] == "unclaimed"

    wake(tmp_path)
    assert last_wake(tmp_path)["outcome"] == "idle", "it is still cooling off"


def test_the_cooling_off_expires_and_the_item_comes_back(tmp_path: Path) -> None:
    """A transient failure must recover without anybody clearing a counter."""
    board(tmp_path, {"id": OLDEST, "created_at": "2026-09-01T00:00:00Z"})
    wake(tmp_path, WAKE_SKIP_HOURS=0)

    wake(tmp_path, session=claims(OLDEST, tmp_path))
    state = last_wake(tmp_path)
    assert state["item_id"] == OLDEST and state["outcome"] == "woke"


def test_repeated_failures_are_counted_not_forgotten(tmp_path: Path) -> None:
    board(tmp_path, {"id": OLDEST, "created_at": "2026-09-01T00:00:00Z"})
    wake(tmp_path, WAKE_SKIP_HOURS=0)
    wake(tmp_path, WAKE_SKIP_HOURS=0)
    assert ledger(tmp_path)[OLDEST]["attempts"] == 2


def test_a_queue_where_everything_is_cooling_off_says_so(tmp_path: Path) -> None:
    """Bare `idle` would read as "there is no work"."""
    board(tmp_path, {"id": OLDEST, "created_at": "2026-09-01T00:00:00Z"})
    wake(tmp_path)
    wake(tmp_path)
    state = last_wake(tmp_path)
    assert state["outcome"] == "idle"
    assert [entry["item_id"] for entry in state["skipped"]] == [OLDEST]
    assert "cooling off" in state["detail"]


def test_an_empty_queue_is_idle_with_nothing_skipped(tmp_path: Path) -> None:
    board(tmp_path, {"id": OLDEST, "created_at": "2026-09-01T00:00:00Z", "status": "done"})
    wake(tmp_path)
    state = last_wake(tmp_path)
    assert state["outcome"] == "idle" and state["skipped"] == []


def test_a_cooled_item_that_leaves_the_board_leaves_the_ledger(tmp_path: Path) -> None:
    """Otherwise the ledger grows one dead entry per closed item, forever."""
    board(tmp_path)
    wake(tmp_path)
    assert OLDEST in ledger(tmp_path)

    board(tmp_path, {"id": NEWER, "created_at": "2026-09-02T00:00:00Z"})
    wake(tmp_path, session=claims(NEWER, tmp_path))
    assert ledger(tmp_path) == {}


def test_the_script_never_writes_to_the_board(tmp_path: Path) -> None:
    """The launcher is not the executor. Marking the item blocked is the
    session's job, and a launcher filing reports on its behalf is the exact
    confusion the outbox rules exist to prevent."""
    path = board(tmp_path)
    before = path.read_bytes()
    wake(tmp_path)
    wake(tmp_path)
    assert path.read_bytes() == before


# ------------------------------------------ the prompt and the allowlist agree


def commands_the_prompt_prints(prompt: str) -> list[str]:
    """Every command in a fenced block of the prompt, by its first word."""
    commands: list[str] = []
    inside = continued = False
    for line in prompt.splitlines():
        if line.startswith("```"):
            inside, continued = not inside, False
            continue
        if not inside or not line.strip():
            continue
        if not continued:
            commands.append(line.split()[0])
        continued = line.rstrip().endswith("\\")
    return commands


def test_every_command_the_prompt_prints_is_one_the_allowlist_permits(
    tmp_path: Path,
) -> None:
    """Two strings in two files, kept in step by hand, drifted apart.

    Nothing about `Bash(work:*)` looked wrong next to a prompt saying `worklog`,
    and the only symptom was an item that never moved. This is the check that
    was missing, run against the strings the script really passes.
    """
    board(tmp_path)
    wake(tmp_path)
    argv = session_argv(tmp_path)
    permitted = set(re.findall(r"Bash\(([^:)]+):\*\)", argv["allowed"]))

    printed = commands_the_prompt_prints(argv["prompt"])
    assert printed, "the prompt prints no commands at all, so this proves nothing"
    for command in printed:
        assert command in permitted, (
            f"the prompt tells the session to run {command!r}, which the "
            f"allowlist does not permit: {sorted(permitted)}"
        )


def test_the_allowlist_no_longer_names_a_command_that_exists_nowhere(
    tmp_path: Path,
) -> None:
    """The specific pair that broke, named, so a rename has to touch both."""
    board(tmp_path)
    wake(tmp_path)
    argv = session_argv(tmp_path)
    assert "worklog work update" in argv["prompt"]
    assert "Bash(worklog:*)" in argv["allowed"]
    assert "Bash(work:*)" not in argv["allowed"]


def test_the_prompt_leaves_no_placeholder_unrendered(tmp_path: Path) -> None:
    board(tmp_path)
    wake(tmp_path)
    assert "{{" not in session_argv(tmp_path)["prompt"]


def test_the_session_is_given_a_path_on_which_worklog_resolves(tmp_path: Path) -> None:
    """The prompt says `worklog`; the script is what makes that name resolve.

    An absolute path threaded into the prompt would have to be quoted -- this
    repository's own directory on the production host contains a space -- and
    quoting is not something an allowlist prefix survives. Putting the
    virtualenv in front of `PATH` leaves both files saying the bare name.
    """
    assert (ROOT / ".venv" / "bin" / "worklog").exists(), "the venv provides it"
    board(tmp_path)
    wake(tmp_path)
    handed = (tmp_path / "session-path").read_text(encoding="utf-8")
    assert handed.split(":")[0] == str(ROOT / ".venv" / "bin")


def test_a_wake_with_no_worklog_anywhere_is_refused_by_name(tmp_path: Path) -> None:
    """A session that cannot reach the board can only burn a tick.

    The copy is how the virtualenv is made absent: `wake-local.sh` finds its own
    repository, and this one has a `.venv`.
    """
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "scripts").mkdir(parents=True)
    for name in ("wake-local.sh", "local-work-prompt.md"):
        shutil.copy(ROOT / "scripts" / name, elsewhere / "scripts" / name)
    board(tmp_path)
    bin_dir = stub_bin(tmp_path, DOES_NOTHING)
    (bin_dir / "worklog").unlink()

    subprocess.run(
        ["bash", str(elsewhere / "scripts" / "wake-local.sh")],
        capture_output=True,
        timeout=120,
        env=dict(
            os.environ,
            PATH=f"{bin_dir}:/usr/bin:/bin",
            APP_CONFIG_ROOT=str(tmp_path / "config"),
            WAKE_STATE_DIR=str(tmp_path / "state"),
        ),
    )
    state = last_wake(tmp_path)
    assert state["outcome"] == "no-worklog"
    assert not (tmp_path / "argv-1").exists(), "no session was started"


# ------------------------------------------------------ the state file itself


@pytest.mark.parametrize(
    "field", ["started_at", "finished_at", "outcome", "item_id", "skipped", "detail"]
)
def test_the_state_carries_the_keys_a_reader_of_any_tick_file_expects(
    tmp_path: Path, field: str
) -> None:
    board(tmp_path)
    wake(tmp_path)
    assert field in last_wake(tmp_path)


def test_a_tick_that_does_nothing_still_writes_the_state_file(tmp_path: Path) -> None:
    """A missing file means the unit never reached its own bookkeeping."""
    board(tmp_path, {"id": OLDEST, "created_at": "2026-09-01T00:00:00Z", "status": "done"})
    wake(tmp_path)
    assert (tmp_path / "state" / "last-wake.json").is_file()
