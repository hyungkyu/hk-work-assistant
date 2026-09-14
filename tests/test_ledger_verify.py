from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from conftest_ledger import build_notion_tree, build_slack_tree  # noqa: E402

from rlwrld_worklog.ledger.convert import convert_source, iter_ledger_files  # noqa: E402
from rlwrld_worklog.ledger.verify import verify_ledger  # noqa: E402


def test_clean_conversion_verifies(tmp_path):
    legacy = build_slack_tree(tmp_path / "legacy")
    out = tmp_path / "out"
    convert_source(legacy_root=legacy, out_root=out, source="slack")
    report = verify_ledger(ledger_root=out, source="slack", legacy_root=legacy)
    assert report.ok, report.failures
    assert report.ledger_records == 5
    assert report.schema_invalid == 0
    assert report.duplicates["identical_content_rows"] == 0
    assert report.provenance["verified"] == report.provenance["checked"] > 0
    assert report.provenance["hash_mismatch"] == 0
    assert report.provenance["missing"] == 0


def test_unknowns_are_surfaced_not_hidden(tmp_path):
    legacy = build_slack_tree(tmp_path / "legacy")
    out = tmp_path / "out"
    convert_source(legacy_root=legacy, out_root=out, source="slack")
    report = verify_ledger(ledger_root=out, source="slack", legacy_root=legacy)
    unknown = report.unknown_counts
    assert unknown["deleted_state_unknown"] == 5
    assert unknown["source_updated_at_unknown"] == 4
    assert unknown["visibility_routing_anomaly"] == 1


def test_modified_legacy_file_breaks_provenance(tmp_path):
    legacy = build_slack_tree(tmp_path / "legacy")
    out = tmp_path / "out"
    convert_source(legacy_root=legacy, out_root=out, source="slack")

    from rlwrld_worklog.ledger.common import file_sha256

    file_sha256.cache_clear()
    target = legacy / "shared" / "daily_raw" / "2026-05-01" / "slack" / "common" / "test-channel.json"
    payload = json.loads(target.read_text(encoding="utf-8"))
    payload["message_count"] = 999
    target.write_text(json.dumps(payload), encoding="utf-8")

    report = verify_ledger(ledger_root=out, source="slack", legacy_root=legacy)
    assert not report.ok
    assert report.provenance["hash_mismatch"] >= 1
    assert any("no longer match" in failure for failure in report.failures)


def test_missing_legacy_file_is_reported(tmp_path):
    legacy = build_slack_tree(tmp_path / "legacy")
    out = tmp_path / "out"
    convert_source(legacy_root=legacy, out_root=out, source="slack")

    from rlwrld_worklog.ledger.common import file_sha256

    file_sha256.cache_clear()
    (legacy / "shared" / "daily_raw" / "2026-05-01" / "slack" / "dm" / "D0TESTDM01.json").unlink()
    report = verify_ledger(ledger_root=out, source="slack", legacy_root=legacy)
    assert not report.ok
    assert report.provenance["missing"] == 1


def test_two_capture_paths_in_one_window_are_kept_not_failed(tmp_path):
    """The same message seen by conversations.history and search.messages has
    different fields in each copy, so both are kept and neither is a failure."""
    import json as _json

    legacy = build_slack_tree(tmp_path / "legacy")
    target = (
        legacy / "shared" / "daily_raw" / "2026-05-01" / "slack" / "common" / "test-channel.json"
    )
    payload = _json.loads(target.read_text(encoding="utf-8"))
    primary = payload["messages"][0]
    supplemented = dict(primary)
    supplemented.pop("permalink")
    supplemented["username"] = "user-a"
    supplemented["edited"] = {"user": primary["user"], "ts": "1777000050.000000"}
    supplemented["_supplemented"] = True
    payload["messages"].append(supplemented)
    target.write_text(_json.dumps(payload), encoding="utf-8")

    out = tmp_path / "out"
    convert_source(legacy_root=legacy, out_root=out, source="slack")
    report = verify_ledger(ledger_root=out, source="slack", legacy_root=legacy)
    assert report.ok, report.failures
    assert report.duplicates["identical_content_rows"] == 0
    assert report.duplicates["entities_with_multiple_observations_in_window"] == 1
    assert report.duplicates["extra_observations_in_window"] == 1
    # the edit metadata survives only because both copies were kept
    profiles = report.duplicates["multi_observation_by_capture_profile"]
    assert sum(profiles.values()) == 1


def test_same_entity_in_public_and_restricted_container_warns(tmp_path):
    import json as _json

    legacy = build_slack_tree(tmp_path / "legacy")
    shared = (
        legacy / "shared" / "daily_raw" / "2026-05-01" / "slack" / "common" / "test-channel.json"
    )
    payload = _json.loads(shared.read_text(encoding="utf-8"))
    leaked = dict(payload["messages"][0])
    leaked["text"] = "same message, restricted copy"
    private = (
        legacy / "personal" / "daily_raw" / "2026-05-01" / "slack" / "private" / "team-private.json"
    )
    private_payload = _json.loads(private.read_text(encoding="utf-8"))
    private_payload["messages"].append(leaked)
    private.write_text(_json.dumps(private_payload), encoding="utf-8")

    out = tmp_path / "out"
    convert_source(legacy_root=legacy, out_root=out, source="slack")
    report = verify_ledger(ledger_root=out, source="slack", legacy_root=legacy)
    assert report.ok, report.failures
    assert report.duplicates["same_entity_in_public_and_restricted_container"] == 1
    assert any("visibility routing" in warning for warning in report.warnings)


def test_injected_duplicate_row_is_caught(tmp_path):
    legacy = build_slack_tree(tmp_path / "legacy")
    out = tmp_path / "out"
    convert_source(legacy_root=legacy, out_root=out, source="slack")
    target = iter_ledger_files(out, "slack")[0]
    lines = target.read_text(encoding="utf-8").splitlines()
    duplicate = json.loads(lines[0])
    duplicate["ledger_id"] = "11111111-1111-5111-8111-111111111111"
    lines.append(json.dumps(duplicate, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")

    report = verify_ledger(ledger_root=out, source="slack", legacy_root=legacy)
    assert not report.ok
    assert report.duplicates["identical_content_rows"] == 1


def test_same_entity_in_two_windows_is_not_a_duplicate(tmp_path):
    legacy = build_slack_tree(tmp_path / "legacy", day="2026-05-01")
    build_slack_tree(legacy, day="2026-05-02")
    out = tmp_path / "out"
    convert_source(legacy_root=legacy, out_root=out, source="slack")
    report = verify_ledger(ledger_root=out, source="slack", legacy_root=legacy)
    assert report.ok, report.failures
    assert report.duplicates["identical_content_rows"] == 0
    assert report.duplicates["entities_seen_in_multiple_windows"] > 0
    assert report.duplicates["max_windows_for_one_entity"] == 2
    assert report.observation_dates["count"] == 2


def test_notion_verify_counts_entity_types(tmp_path):
    legacy = build_notion_tree(tmp_path / "legacy")
    out = tmp_path / "out"
    convert_source(legacy_root=legacy, out_root=out, source="notion", salvage_comments=True)
    report = verify_ledger(ledger_root=out, source="notion", legacy_root=legacy)
    assert report.ok, report.failures
    assert report.by_entity_type == {"page": 3, "block": 2, "comment": 1}
    assert report.extracted_text == 1


# --- Multiple ledger roots (P1, 2026-09-14) --------------------------------
#
# The ledger is spread over a live staging root and the backfill archives.
# The verifier read one root, compared its record count to the database, and
# failed — every time, for months, which made `ok:false` its permanent answer
# and the check something nobody could act on.

from pathlib import Path as _Path  # noqa: E402

import pytest  # noqa: E402

from rlwrld_worklog.ledger.verify import verify_ledger as _verify  # noqa: E402


def _write(root: _Path, source: str, name: str, records: list[dict]) -> None:
    directory = root / "ledger" / source
    directory.mkdir(parents=True, exist_ok=True)
    directory.joinpath(name).write_text(
        "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n",
        encoding="utf-8",
    )


def _record(ledger_id: str) -> dict:
    return {
        "ledger_id": ledger_id,
        "entity_type": "message",
        "source_entity_id": ledger_id,
        "capture_profile": "daily",
        "content_hash": ledger_id,
        "observation_window": {"start": "2026-09-01"},
        "provenance": {"source_file": f"{ledger_id}.json", "source_file_sha256": "x" * 64},
    }


def test_records_from_every_root_are_counted(tmp_path: _Path) -> None:
    live, archive = tmp_path / "live", tmp_path / "archive"
    _write(live, "slack", "a.jsonl", [_record("one")])
    _write(archive, "slack", "b.jsonl", [_record("two"), _record("three")])

    report = _verify(
        ledger_roots=[live, archive], source="slack", validate_schema=False
    )
    assert report.ledger_records == 3
    assert report.records_by_root == {str(live): 1, str(archive): 2}


def test_one_record_in_two_roots_is_counted_once(tmp_path: _Path) -> None:
    """A day collected live and then re-archived is one record, not two.

    Counted twice it would exceed the database and fail for the opposite
    reason from the bug this fixes — which would look like progress and be
    just as wrong.
    """
    live, archive = tmp_path / "live", tmp_path / "archive"
    _write(live, "slack", "a.jsonl", [_record("one")])
    _write(archive, "slack", "b.jsonl", [_record("one"), _record("two")])

    report = _verify(ledger_roots=[live, archive], source="slack", validate_schema=False)
    assert report.ledger_records == 2
    assert report.records_in_more_than_one_root == 1


def test_a_root_that_holds_nothing_is_named(tmp_path: _Path) -> None:
    """Usually a wrong path. Silence there is a clean run over a fraction."""
    live, empty = tmp_path / "live", tmp_path / "empty"
    _write(live, "slack", "a.jsonl", [_record("one")])
    report = _verify(ledger_roots=[live, empty], source="slack", validate_schema=False)
    assert report.roots_with_no_files == [str(empty)]


def test_the_same_root_named_twice_does_not_double(tmp_path: _Path) -> None:
    live = tmp_path / "live"
    _write(live, "slack", "a.jsonl", [_record("one")])
    report = _verify(ledger_roots=[live, live], source="slack", validate_schema=False)
    assert report.ledger_records == 1


def test_a_single_root_still_works_the_old_way(tmp_path: _Path) -> None:
    live = tmp_path / "live"
    _write(live, "slack", "a.jsonl", [_record("one")])
    report = _verify(ledger_root=live, source="slack", validate_schema=False)
    assert (report.ledger_records, report.ledger_root) == (1, str(live))


def test_no_root_at_all_is_refused(tmp_path: _Path) -> None:
    with pytest.raises(ValueError):
        _verify(source="slack", validate_schema=False)
