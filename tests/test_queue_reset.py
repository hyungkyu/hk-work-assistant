"""The one-time board reset, queued by the batch rather than typed by a person.

HK, 2026-09-14: 보드를 모두 리셋하자 — 한거 기록은 남기고, 할것과 하고 있는
것. 기타 알 수 없는 모든건 삭제.

Two dozen decisions taken together. They go through the same queue as every
other board edit, so each leaves a receipt and each archive carries its
reason. The property that matters most is that it cannot fire twice: a reset
queued a second time would archive items a second time and reject noisily,
and worse, would suggest the board can be reset by accident.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "queue-reset.sh"
MANIFEST = ROOT / "reset" / "2026-09-14-board-reset.json"


def run(manifest: Path, config_root: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SCRIPT), str(manifest)],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env={"APP_CONFIG_ROOT": str(config_root), "PATH": "/usr/bin:/bin", "HOME": str(config_root)},
    )


def outbox(config_root: Path) -> list[Path]:
    directory = config_root / "cowork" / "outbox" / "mori"
    return sorted(path for path in directory.glob("*.json"))


def test_the_manifest_queues_every_entry_once(tmp_path: Path) -> None:
    done = run(MANIFEST, tmp_path)
    assert done.returncode == 0, done.stderr
    entries = json.loads(MANIFEST.read_text(encoding="utf-8"))["entries"]
    assert len(outbox(tmp_path)) == len(entries)


def test_running_it_again_queues_nothing(tmp_path: Path) -> None:
    """Self-disarming. A reset that can fire twice is a reset nobody can leave
    in the batch."""
    run(MANIFEST, tmp_path)
    before = len(outbox(tmp_path))
    second = run(MANIFEST, tmp_path)
    assert second.returncode == 0
    assert "already queued" in second.stdout
    assert len(outbox(tmp_path)) == before


def test_a_missing_manifest_is_an_error_not_a_silent_success(tmp_path: Path) -> None:
    done = run(tmp_path / "nope.json", tmp_path)
    assert done.returncode != 0
    assert not (tmp_path / "cowork" / "outbox").exists()


@pytest.mark.parametrize("required", ["op", "work_id"])
def test_every_archive_entry_says_what_it_is_and_why(required: str) -> None:
    entries = json.loads(MANIFEST.read_text(encoding="utf-8"))["entries"]
    for entry in entries:
        if entry.get("op") == "archive":
            assert entry.get(required)
            assert len(entry.get("reason", "")) >= 8


def test_every_entry_is_pinned_to_the_revision_it_was_composed_against() -> None:
    """An item somebody moved since must be refused, not swept up."""
    entries = json.loads(MANIFEST.read_text(encoding="utf-8"))["entries"]
    archives = [entry for entry in entries if entry.get("op") == "archive"]
    assert archives
    assert all(isinstance(entry.get("expected_revision"), int) for entry in archives)


def test_every_kept_item_is_placed_in_a_phase() -> None:
    """The reset's other half: what survives is labelled, not left unplaced."""
    entries = json.loads(MANIFEST.read_text(encoding="utf-8"))["entries"]
    kept = [entry for entry in entries if entry.get("op") != "archive"]
    assert kept
    assert {entry["phase"] for entry in kept} == {"P0", "P1", "P2"}
