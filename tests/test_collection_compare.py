from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from rlwrld_worklog import cli
from rlwrld_worklog.collection_compare import ComparisonInputError, compare_runs


def write_ledger(path: Path, values: dict[str, list[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for entity_type, ids in values.items():
            for entity_id in ids:
                handle.write(
                    json.dumps(
                        {"entity_type": entity_type, "source_entity_id": entity_id}
                    )
                    + "\n"
                )


def write_manifest(
    root: Path,
    name: str,
    *,
    rule: str,
    files: list[dict[str, object]] | None = None,
    skips: list[dict[str, object]] | None = None,
) -> Path:
    path = root / "manifests" / "notion" / "production" / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "source": "notion",
                "run_id": name,
                "collection_rule_version": rule,
                "status": "success_with_skips" if skips else "success",
                "coverage_complete": not skips,
                "started_at": "2026-09-08T00:00:00+00:00",
                "finished_at": "2026-09-08T00:10:00+00:00",
                "requested_window": {
                    "since_effective": "2026-09-02T15:00:00+00:00",
                    "until": "2026-09-03T15:00:00+00:00",
                },
                "files": files or [],
                "skips": skips or [],
                "errors": [],
                "truncated": False,
                "counters": {
                    "api_call_counts": {"/search": 10},
                    "objects_failed_unresolved": 0,
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def test_supplemental_drop_does_not_fail_a_run(tmp_path: Path) -> None:
    old_manifest = write_manifest(tmp_path, "old", rule="V7")
    new_manifest = write_manifest(tmp_path, "new", rule="V8")
    old_ledger, new_ledger = tmp_path / "old.jsonl", tmp_path / "new.jsonl"
    write_ledger(old_ledger, {"page": ["a", "b"], "block": ["1", "2", "3"]})
    write_ledger(new_ledger, {"page": ["a", "b"], "block": ["1"]})

    report = compare_runs(
        baseline_manifest_path=old_manifest,
        candidate_manifest_path=new_manifest,
        baseline_ledger_path=old_ledger,
        candidate_ledger_path=new_ledger,
    )

    assert report["verdict"] == "pass"
    assert report["entities"]["block"]["role"] == "supplemental"
    assert report["entities"]["block"]["baseline_only"] == 2


def test_notion_page_moved_after_window_is_explained(tmp_path: Path) -> None:
    old_manifest = write_manifest(tmp_path, "old", rule="V7")
    raw_relative = "raw/notion/production/run/000001-search.json.gz"
    raw_path = tmp_path / raw_relative
    raw_path.parent.mkdir(parents=True)
    with gzip.open(raw_path, "wt", encoding="utf-8") as handle:
        json.dump(
            {"results": [{"id": "moved", "last_edited_time": "2026-09-07T00:00:00Z"}]},
            handle,
        )
    new_manifest = write_manifest(
        tmp_path, "new", rule="V8", files=[{"kind": "search", "path": raw_relative}]
    )
    old_ledger, new_ledger = tmp_path / "old.jsonl", tmp_path / "new.jsonl"
    write_ledger(old_ledger, {"page": ["kept", "moved"]})
    write_ledger(new_ledger, {"page": ["kept"]})

    report = compare_runs(
        baseline_manifest_path=old_manifest,
        candidate_manifest_path=new_manifest,
        baseline_ledger_path=old_ledger,
        candidate_ledger_path=new_ledger,
    )

    assert report["verdict"] == "pass"
    assert report["primary_coverage"]["moved_after_window"] == 1
    assert report["primary_coverage"]["missing_unexplained"] == 0


def test_material_primary_loss_fails(tmp_path: Path) -> None:
    old_manifest = write_manifest(tmp_path, "old", rule="V7")
    new_manifest = write_manifest(tmp_path, "new", rule="V8")
    old_ledger, new_ledger = tmp_path / "old.jsonl", tmp_path / "new.jsonl"
    write_ledger(old_ledger, {"page": [str(i) for i in range(100)]})
    write_ledger(new_ledger, {"page": [str(i) for i in range(80)]})

    report = compare_runs(
        baseline_manifest_path=old_manifest,
        candidate_manifest_path=new_manifest,
        baseline_ledger_path=old_ledger,
        candidate_ledger_path=new_ledger,
    )

    assert report["verdict"] == "fail"
    assert report["material_impact"] == "high"
    assert "primary_retention_below_material_threshold" in report["reasons"]


def test_small_unexplained_primary_loss_needs_review(tmp_path: Path) -> None:
    old_manifest = write_manifest(tmp_path, "old", rule="V7")
    new_manifest = write_manifest(tmp_path, "new", rule="V8")
    old_ledger, new_ledger = tmp_path / "old.jsonl", tmp_path / "new.jsonl"
    write_ledger(old_ledger, {"page": [str(i) for i in range(100)]})
    write_ledger(new_ledger, {"page": [str(i) for i in range(99)]})

    report = compare_runs(
        baseline_manifest_path=old_manifest,
        candidate_manifest_path=new_manifest,
        baseline_ledger_path=old_ledger,
        candidate_ledger_path=new_ledger,
    )

    assert report["verdict"] == "review"
    assert report["material_impact"] == "low"


def test_material_primary_gain_is_high_impact_review_not_failure(tmp_path: Path) -> None:
    old_manifest = write_manifest(tmp_path, "old", rule="V8")
    new_manifest = write_manifest(tmp_path, "new", rule="V9")
    old_ledger, new_ledger = tmp_path / "old.jsonl", tmp_path / "new.jsonl"
    write_ledger(old_ledger, {"page": [str(i) for i in range(100)]})
    write_ledger(new_ledger, {"page": [str(i) for i in range(110)]})

    report = compare_runs(
        baseline_manifest_path=old_manifest,
        candidate_manifest_path=new_manifest,
        baseline_ledger_path=old_ledger,
        candidate_ledger_path=new_ledger,
    )

    assert report["verdict"] == "review"
    assert report["material_impact"] == "high"
    assert report["primary_coverage"]["added"] == 10
    assert report["primary_coverage"]["gain_ratio"] == 0.1
    assert "material_primary_entities_added" in report["reasons"]


def test_scope_mismatch_fails_and_cli_returns_one(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    old_manifest = write_manifest(tmp_path / "old-root", "old", rule="V7")
    new_manifest = write_manifest(tmp_path / "new-root", "new", rule="V8")
    payload = json.loads(new_manifest.read_text())
    payload["requested_window"]["until"] = "2026-09-04T15:00:00+00:00"
    new_manifest.write_text(json.dumps(payload))
    old_ledger, new_ledger = tmp_path / "old.jsonl", tmp_path / "new.jsonl"
    write_ledger(old_ledger, {"page": ["a"]})
    write_ledger(new_ledger, {"page": ["a"]})

    code = cli.main(
        [
            "collection",
            "compare",
            "--baseline-manifest",
            str(old_manifest),
            "--candidate-manifest",
            str(new_manifest),
            "--baseline-ledger",
            str(old_ledger),
            "--candidate-ledger",
            str(new_ledger),
        ]
    )

    assert code == 1
    assert json.loads(capsys.readouterr().out)["scope"]["matches"] is False


def test_cli_summary_is_one_line(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    manifest = write_manifest(tmp_path, "run", rule="V8")
    ledger = tmp_path / "run.jsonl"
    write_ledger(ledger, {"page": ["a"]})

    code = cli.main(
        [
            "collection",
            "compare",
            "--baseline-manifest",
            str(manifest),
            "--candidate-manifest",
            str(manifest),
            "--baseline-ledger",
            str(ledger),
            "--candidate-ledger",
            str(ledger),
            "--summary",
        ]
    )

    output = capsys.readouterr().out
    assert code == 0
    assert output.count("\n") == 1
    assert json.loads(output)["adjusted_primary_retention"] == 1.0


def test_invalid_material_threshold_is_refused(tmp_path: Path) -> None:
    manifest = write_manifest(tmp_path, "run", rule="V8")
    ledger = tmp_path / "run.jsonl"
    write_ledger(ledger, {"page": ["a"]})
    with pytest.raises(ComparisonInputError, match="material_retention"):
        compare_runs(
            baseline_manifest_path=manifest,
            candidate_manifest_path=manifest,
            baseline_ledger_path=ledger,
            candidate_ledger_path=ledger,
            material_retention=0,
        )
