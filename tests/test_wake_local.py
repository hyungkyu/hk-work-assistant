"""`scripts/wake-local.sh`: one board item, one session, one honest outcome.

The script itself is run, with `bash`, exactly as `hkwa-wake.service` runs it.
What is replaced is the one thing that would start a real agent: a stub `claude`
goes in front of `PATH`, records every argument it was handed, and edits the
board only when the test asked it to. Everything else -- the picker, the tool
allowlist, the state file -- is the real thing, and the prompt handed to the
stub is the real `scripts/local-work-prompt.md`.

It follows `tests/test_backfill_days.py`: a few helpers and no framework.

The incident these pin: the allowlist granted a command that exists on no
machine, while the prompt told the session to run `worklog`. Every board write
was refused for some three hundred ticks, and the only symptom was an item that
never moved.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "wake-local.sh"

OLDEST = "item-oldest"
NEWER = "item-newer"

# A stub session that does nothing at all -- the shape of every tick in the
# incident: the CLI ran, exited zero, and the board was exactly as it was.
DOES_NOTHING = "exit 0\n"


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
