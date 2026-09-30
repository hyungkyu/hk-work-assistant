"""A missed collection day becomes one board item, filed once.

Every report here is a literal payload in the shape `worklog collection audit`
prints. No archive is opened and no board but a temporary one is touched.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from rlwrld_worklog.collection_gap_alarm import file_gaps, gap_keys, new_gap_item
from rlwrld_worklog.cli import main as cli_main


def work_main(argv: list[str]) -> int:
    return cli_main(["work", *argv])

NOW = datetime(2026, 10, 2, 17, 0, tzinfo=timezone.utc)


def report(*gaps: tuple[str, str]) -> dict:
    return {
        "ok": not gaps,
        "summary": "수집 감사 10-03 02:00 KST",
        "gaps": [{"source": source, "date": date, "coverage": "not_collected"} for source, date in gaps],
    }


def test_a_clean_night_files_nothing(tmp_path: Path) -> None:
    result = file_gaps(report(), state=tmp_path / "state.json", outbox=tmp_path / "outbox", now=NOW)
    assert result == {"filed": False, "new_gaps": []}
    assert not (tmp_path / "outbox").exists()


def test_a_gap_is_filed_once_across_the_nights_that_still_see_it(tmp_path: Path) -> None:
    """The audit looks back three days, so the same hole is in three reports."""
    state, outbox = tmp_path / "state.json", tmp_path / "outbox"
    first = file_gaps(report(("notion", "2026-09-06")), state=state, outbox=outbox, now=NOW)
    again = file_gaps(report(("notion", "2026-09-06")), state=state, outbox=outbox, now=NOW)
    assert first["filed"] is True and first["new_gaps"] == ["notion:2026-09-06"]
    assert again["filed"] is False
    assert len(list(outbox.glob("*.json"))) == 1


def test_a_new_gap_beside_an_old_one_files_only_the_new_one(tmp_path: Path) -> None:
    payload, fresh = new_gap_item(
        report(("notion", "2026-09-06"), ("slack", "2026-09-07")), ["notion:2026-09-06"]
    )
    assert fresh == ["slack:2026-09-07"]
    assert payload is not None and "slack 09-07" in payload["title"]
    assert "notion" not in payload["title"]


def test_malformed_gaps_are_skipped_rather_than_filed_as_blanks() -> None:
    assert gap_keys({"gaps": [{"source": "", "date": "2026-09-06"}, {"source": "slack"}]}) == []


def test_an_unreadable_memory_files_again_rather_than_never(tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    state.write_text("{ not json", encoding="utf-8")
    result = file_gaps(report(("github", "2026-09-05")), state=state, outbox=tmp_path / "o", now=NOW)
    assert result["filed"] is True
    assert json.loads(state.read_text(encoding="utf-8")) == ["github:2026-09-05"]


def test_the_queued_file_is_one_the_board_accepts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Checked through the real queue, because a file it refuses is no alarm at all."""
    outbox, config = tmp_path / "outbox", tmp_path / "config"
    file_gaps(report(("notion", "2026-09-06")), state=tmp_path / "s.json", outbox=outbox, now=NOW)

    work_main(["apply-outbox", "--config-root", str(config), "--outbox", str(outbox), "--actor", "mori"])
    result = json.loads(capsys.readouterr().out)
    assert (result["applied"], result["rejected"]) == (1, 0), result

    work_main(["show", "--config-root", str(config), result["results"][0]["work_id"]])
    item = json.loads(capsys.readouterr().out)["item"]
    assert item["status"] == "ready"
    assert item["assigned_to"] == "hk"
    assert "notion:2026-09-06" in item["detail"]
