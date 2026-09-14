"""The board snapshot: the list the audit's counts refer to.

The audit says "22 live, 9 assigned outside the roster". Without the list,
that is a number a reviewer has to believe. This module produces the list.

The first version of it was a heredoc inside the audit script with `|| true`
on the end. It failed on the host, wrote a zero-byte file, and reported
nothing -- which is the same failure shape as the false green that cost this
project days. These tests exist because the code moved into a file so it
could have them.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "board_snapshot.py"

sys.path.insert(0, str(SCRIPT.parent))
from board_snapshot import snapshot, walk  # noqa: E402


def item(identifier: str, **fields) -> dict:
    base = {
        "id": identifier,
        "title": "t",
        "status": "backlog",
        "assigned_to": "mori",
        "next_action": "",
        "updated_at": "2026-09-14T00:00:00+00:00",
        "revision": 1,
        "phase": None,
        "detail": "볼 필요 없는 긴 설명",
    }
    base.update(fields)
    return base


def board(*items: dict) -> dict:
    return {
        "aged_out": 0,
        "columns": [
            {"key": "in_progress", "title": "진행 중", "count": len(items), "items": list(items)},
            {"key": "ready", "title": "다음 할 일", "count": 0, "items": []},
        ],
    }


def test_items_are_found_wherever_the_columns_put_them() -> None:
    """Structural, not positional: a layout change must not empty the list."""
    found = list(walk({"anything": {"nested": {"deeper": [item("wi_a" + "0" * 13)]}}}))
    assert [row["id"] for row in found] == ["wi_a" + "0" * 13]


def test_the_snapshot_keeps_the_checkable_fields_and_drops_the_rest() -> None:
    out = snapshot(board(item("wi_" + "a" * 16, title="제목", phase="P1")))
    assert out["items"] == 1
    row = out["board"][0]
    assert row["title"] == "제목" and row["phase"] == "P1"
    # Not a copy of the board: the store keeps the prose.
    assert "detail" not in row


def test_an_item_appearing_twice_is_listed_once() -> None:
    one = item("wi_" + "b" * 16)
    out = snapshot({"columns": [{"items": [one]}, {"items": [one]}]})
    assert out["items"] == 1


def test_p0_reads_first_and_the_unplaced_last() -> None:
    out = snapshot(board(
        item("wi_" + "1" * 16, phase="P2"),
        item("wi_" + "2" * 16, phase=None),
        item("wi_" + "3" * 16, phase="P0"),
    ))
    assert [row["phase"] for row in out["board"]] == ["P0", "P2", None]


def test_an_empty_board_is_an_empty_list_not_an_error() -> None:
    out = snapshot(board())
    assert (out["items"], out["board"], out["outcome"]) == (0, [], "snapshot")


def test_it_runs_as_a_process_and_writes_json_on_stdout() -> None:
    """What the batch actually invokes, exercised the way the batch does."""
    done = subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=json.dumps(board(item("wi_" + "c" * 16, phase="P0"))),
        capture_output=True,
        text=True,
        env={"STARTED": "2026-09-14T05:00:00Z", "PATH": "/usr/bin:/bin"},
    )
    assert done.returncode == 0, done.stderr
    payload = json.loads(done.stdout)
    assert payload["started_at"] == "2026-09-14T05:00:00Z"
    assert payload["board"][0]["phase"] == "P0"


def test_malformed_input_fails_loudly_rather_than_writing_nothing() -> None:
    """A snapshot that cannot be taken must not look like an empty board."""
    done = subprocess.run(
        [sys.executable, str(SCRIPT)],
        input="not json",
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin"},
    )
    assert done.returncode != 0
    assert done.stdout.strip() == ""
