from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from conftest_ledger import build_notion_tree, build_slack_tree  # noqa: E402

from rlwrld_worklog.ledger.convert import convert_source, iter_ledger_files, read_jsonl  # noqa: E402


def test_dry_run_writes_nothing_but_counts_everything(tmp_path):
    legacy = build_slack_tree(tmp_path / "legacy")
    out = tmp_path / "out"
    result = convert_source(legacy_root=legacy, out_root=out, source="slack", dry_run=True)
    assert result.records_written == 5
    assert result.dry_run is True
    assert not out.exists()
    assert result.manifest_path is None


def test_output_is_deterministic_across_runs(tmp_path):
    legacy = build_slack_tree(tmp_path / "legacy")
    out = tmp_path / "out"
    convert_source(legacy_root=legacy, out_root=out, source="slack")
    first = {path.name: path.read_bytes() for path in iter_ledger_files(out, "slack")}
    convert_source(legacy_root=legacy, out_root=out, source="slack")
    second = {path.name: path.read_bytes() for path in iter_ledger_files(out, "slack")}
    assert first == second, "a re-run must produce byte-identical ledger files"


def test_partitions_by_observation_date(tmp_path):
    legacy = build_slack_tree(tmp_path / "legacy")
    out = tmp_path / "out"
    convert_source(legacy_root=legacy, out_root=out, source="slack")
    files = iter_ledger_files(out, "slack")
    assert [path.name for path in files] == ["2026-05-01.jsonl"]
    records = list(read_jsonl(files[0]))
    assert len(records) == 5
    assert {record["observation_window"]["start"] for record in records} == {"2026-05-01"}


def test_manifest_records_counts_and_file_hashes(tmp_path):
    legacy = build_slack_tree(tmp_path / "legacy")
    out = tmp_path / "out"
    result = convert_source(legacy_root=legacy, out_root=out, source="slack")
    manifest = json.loads(Path(result.manifest_path).read_text(encoding="utf-8"))
    assert manifest["records_written"] == 5
    assert manifest["by_entity_type"] == {"message": 5}
    assert manifest["source_stats"]["files_skipped_partial"] == 1
    assert manifest["unknown_counts"]["source_updated_at_unknown"] == 4
    for path, digest in manifest["output_files"].items():
        assert digest.startswith("sha256:")
        assert Path(path).is_file()


def test_notion_writes_ledger_and_extracted_text_separately(tmp_path):
    legacy = build_notion_tree(tmp_path / "legacy")
    out = tmp_path / "out"
    result = convert_source(
        legacy_root=legacy, out_root=out, source="notion", salvage_comments=True
    )
    assert result.by_entity_type == {"page": 3, "block": 2, "comment": 1}
    assert result.artifacts_written == 1
    assert (out / "ledger" / "notion" / "2026-05-01.jsonl").is_file()
    assert (out / "ledger" / "notion" / "2026-02-01.jsonl").is_file()
    assert (out / "extracted_text" / "notion" / "2026-05-01.jsonl").is_file()


def test_google_calendar_is_explicitly_deferred(tmp_path):
    with pytest.raises(NotImplementedError) as error:
        convert_source(
            legacy_root=tmp_path, out_root=tmp_path / "out", source="google_calendar"
        )
    assert "calendarId" in str(error.value)


def test_schema_violations_are_counted_not_written(tmp_path, monkeypatch):
    legacy = build_slack_tree(tmp_path / "legacy")
    out = tmp_path / "out"
    monkeypatch.setattr(
        "rlwrld_worklog.ledger.convert.validate_record", lambda record: ["forced failure"]
    )
    result = convert_source(legacy_root=legacy, out_root=out, source="slack")
    assert result.records_written == 0
    assert result.schema_errors == 5
