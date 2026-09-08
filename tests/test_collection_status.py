"""The 수집 현황 reader: classification, KST dates, and safe reading.

Every test drives a synthetic archive in a temporary directory. Nothing here
reads the real ``/data`` tree, and nothing here writes to an archive it did
not create.
"""

from __future__ import annotations

import gzip
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

import pytest

from rlwrld_worklog import collection_status as status
from rlwrld_worklog.collection_progress import PROGRESS_SCHEMA_VERSION
from rlwrld_worklog.collection_rules import ACTIVE_RULE_VERSION, active_rule, active_rule_stamp

KST = status.KST
NOW = datetime(2026, 9, 2, 3, 0, tzinfo=timezone.utc)  # 2026-09-02 12:00 KST


@pytest.fixture()
def paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[status.CollectionPaths]:
    archive_root = tmp_path / "archive"
    config_root = tmp_path / "config"
    ledger_root = tmp_path / "archive" / "staging" / "ledger"
    legacy_root = archive_root / "legacy" / "claude" / "weekly"
    for directory in (archive_root, config_root, ledger_root, legacy_root):
        directory.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("RAW_ARCHIVE_ROOT", str(archive_root))
    monkeypatch.setenv("LEDGER_ROOT", str(ledger_root))
    monkeypatch.setenv("APP_CONFIG_ROOT", str(config_root))
    monkeypatch.setenv("LEGACY_ROOT", str(legacy_root))
    status.clear_caches()
    yield status.paths_from_environment()
    status.clear_caches()


def write_manifest(
    paths: status.CollectionPaths,
    *,
    source: str = "slack",
    environment: str = "production",
    run_id: str = "20260901T000000Z-aaaaaa",
    name: str | None = None,
    raw: str | None = None,
    **overrides: Any,
) -> Path:
    payload: dict[str, Any] = {
        "schema_version": 2,
        "source": source,
        "environment": environment,
        "run_id": run_id,
        "status": "success",
        "capture_profile": "live-slack-web-api/v1",
        "capture_density": "full",
        "dry_run": False,
        "started_at": "2026-09-01T00:00:00+00:00",
        "finished_at": "2026-09-01T00:05:00+00:00",
        "requested_window": {"since": "2026-08-30T22:00:00+00:00"},
        "checkpoint_in": {"run_id": "previous"},
        "checkpoint_advanced": True,
        "api_coverage": {},
        "coverage_notes": ["slack.search_index_lag: search can lag the live channel."],
        "pages_archived": 2,
        "rate_limit_hits": 0,
        "truncated": False,
        "truncation": [],
        "skips": [],
        "errors": [],
        "files": [
            {"path": "raw/a", "compressed_bytes": 100, "kind": "history"},
            {"path": "raw/b", "compressed_bytes": 250, "kind": "history"},
        ],
        **active_rule_stamp(),
    }
    payload.update(overrides)
    directory = paths.manifest_root / source / environment
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (name or f"{run_id}.json")
    path.write_text(
        raw if raw is not None else json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def write_raw_run(
    paths: status.CollectionPaths,
    *,
    source: str = "slack",
    environment: str = "production",
    day: str = "2026/09/02",
    run_id: str = "20260902T010000Z-bbbbbb",
    pages: int = 3,
    secret: str = "TOP-SECRET-MESSAGE-BODY",
    mtime: datetime | None = None,
) -> Path:
    directory = paths.raw_root / source / environment / day / run_id
    directory.mkdir(parents=True, exist_ok=True)
    for index in range(pages):
        target = directory / f"{index + 1:06d}-history-abcdef012345.json.gz"
        target.write_bytes(gzip.compress(json.dumps({"text": secret}).encode(), mtime=0))
        if mtime is not None:
            stamp = mtime.timestamp()
            os.utime(target, (stamp, stamp))
    if mtime is not None:
        stamp = mtime.timestamp()
        os.utime(directory, (stamp, stamp))
    return directory


def write_legacy_day(
    paths: status.CollectionPaths,
    *,
    day: str,
    root: str = "shared",
    sources: tuple[str, ...] = ("slack", "notion", "gcal"),
    meta: dict[str, Any] | None = None,
) -> None:
    for name in sources:
        directory = paths.legacy_root / root / "daily_raw" / day / name
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "payload.json").write_text("{}", encoding="utf-8")
        body = {"status": "ok", "target_date": day, "truncation_warnings": []}
        if meta is not None:
            body.update(meta)
        (directory / "meta.json").write_text(json.dumps(body), encoding="utf-8")


def write_snapshot(paths: status.CollectionPaths, **fields: Any) -> Path:
    payload: dict[str, Any] = {
        "schema_version": PROGRESS_SCHEMA_VERSION,
        "source": "notion",
        "environment": "production",
        "run_id": "20260902T020000Z-cccccc",
        "phase": "capture",
        "status": "running",
        "started_at": "2026-09-02T02:00:00+00:00",
        "updated_at": "2026-09-02T02:58:00+00:00",
        "pid": os.getpid(),
        "host": os.uname().nodename,
        "raw_run_dir": "raw/notion/production/2026/09/02/20260902T020000Z-cccccc",
        "capture_density": "full",
        "dry_run": False,
        "files_written": 42,
        "bytes_written": 4242,
        "manifest_path": None,
        "ledger": None,
    }
    payload.update(fields)
    directory = (
        paths.config_root
        / "collection-status"
        / "progress"
        / str(payload["source"])
        / str(payload["environment"])
    )
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{payload['run_id']}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def find_run(index: status.RunIndex, run_id: str) -> dict[str, Any]:
    for run in index.runs.values():
        if run["run_id"] == run_id:
            return run
    raise AssertionError(f"run {run_id} was not indexed")


# ------------------------------------------------------- rule attribution


def test_a_stamped_manifest_reports_its_rule_as_declared(paths: status.CollectionPaths) -> None:
    write_manifest(paths, run_id="20260901T000000Z-aaaaaa")
    run = find_run(status.build_run_index(paths, now=NOW), "20260901T000000Z-aaaaaa")
    # The stamp names whichever rule was active when the manifest was written,
    # so this follows the registry rather than pinning a version string.
    assert run["rule"]["version"] == ACTIVE_RULE_VERSION
    assert run["rule"]["attribution"] == "declared"
    assert run["rule"]["digest"] == active_rule().digest
    assert run["rule"]["digest_matches_registry"] is True
    assert run["rule"]["evidence"] == ["manifest.collection_rule_version"]


def test_a_declared_version_the_registry_does_not_know_is_flagged(
    paths: status.CollectionPaths,
) -> None:
    # A version number far above anything the registry will plausibly reach.
    # This test used "V9" until the registry published V8 and renumbered the
    # pending rule to V9, at which point the "unknown version" case was quietly
    # testing a known one.
    write_manifest(
        paths,
        run_id="20260901T000001Z-aaaaab",
        collection_rule_version="V999",
        collection_rule_digest="sha256:deadbeef",
    )
    run = find_run(status.build_run_index(paths, now=NOW), "20260901T000001Z-aaaaab")
    assert run["rule"]["version"] == "V999"
    assert run["rule"]["attribution"] == "declared"
    assert run["rule"]["known_version"] is False
    assert run["rule"]["digest_matches_registry"] is None


def test_an_unstamped_manifest_of_the_current_collector_is_inferred_as_active(
    paths: status.CollectionPaths,
) -> None:
    """Rule 6: the reconstruction is reported as inferred, never as declared."""
    write_manifest(
        paths,
        run_id="20260901T000002Z-aaaaac",
        collection_rule_version=None,
        collection_rule_digest=None,
        collection_rule_schema_version=None,
    )
    run = find_run(status.build_run_index(paths, now=NOW), "20260901T000002Z-aaaaac")
    assert run["rule"]["version"] == ACTIVE_RULE_VERSION
    assert run["rule"]["attribution"] == "inferred"
    assert run["rule"]["digest"] is None
    assert "manifest.capture_profile" in run["rule"]["evidence"]


def test_an_old_manifest_without_a_capture_profile_is_still_inferred_as_active(
    paths: status.CollectionPaths,
) -> None:
    write_manifest(
        paths,
        run_id="20260825T105742Z-66cc09832a",
        schema_version=1,
        capture_profile=None,
        capture_density=None,
        collection_rule_version=None,
        collection_rule_digest=None,
        collection_rule_schema_version=None,
        started_at="2026-08-25T10:57:42+00:00",
        finished_at="2026-08-25T10:58:00+00:00",
    )
    run = find_run(status.build_run_index(paths, now=NOW), "20260825T105742Z-66cc09832a")
    assert run["rule"]["attribution"] == "inferred"
    assert set(run["rule"]["evidence"]) >= {"manifest_location", "run_id_format"}


def test_a_manifest_that_identifies_no_rule_is_unknown_not_guessed(
    paths: status.CollectionPaths,
) -> None:
    write_manifest(
        paths,
        run_id="importedbatch",
        capture_profile=None,
        collection_rule_version=None,
        collection_rule_digest=None,
        collection_rule_schema_version=None,
        schema_version=None,
        started_at="2024-03-04T01:00:00+00:00",
        finished_at="2024-03-04T01:10:00+00:00",
    )
    run = find_run(status.build_run_index(paths, now=NOW), "importedbatch")
    assert run["rule"]["version"] is None
    assert run["rule"]["attribution"] == "unknown"


# ---------------------------------------------------------- run aggregation


def test_success_degraded_failed_running_and_stale_are_all_distinguished(
    paths: status.CollectionPaths,
) -> None:
    write_manifest(paths, run_id="20260901T010000Z-000001", status="success")
    write_manifest(
        paths, run_id="20260901T020000Z-000002", status="success_with_skips",
        skips=[{"kind": "channel_not_found"}, {"kind": "channel_not_found"}, {"kind": "is_archived"}],
    )
    write_manifest(paths, run_id="20260901T030000Z-000003", status="degraded")
    write_manifest(
        paths, run_id="20260901T040000Z-000004", status="failed",
        errors=[{"kind": "http_500"}],
    )
    write_raw_run(paths, run_id="20260902T025500Z-000005", mtime=NOW - timedelta(minutes=2))
    write_raw_run(paths, run_id="20260901T000000Z-000006", day="2026/09/01",
                  mtime=NOW - timedelta(days=1))

    index = status.build_run_index(paths, now=NOW)
    states = {run["run_id"]: run["state"] for run in index.runs.values()}
    assert states["20260901T010000Z-000001"] == "success"
    assert states["20260901T020000Z-000002"] == "success_with_skips"
    assert states["20260901T030000Z-000003"] == "degraded"
    assert states["20260901T040000Z-000004"] == "failed"
    assert states["20260902T025500Z-000005"] == "running"
    assert states["20260901T000000Z-000006"] == "stale"

    stale = find_run(index, "20260901T000000Z-000006")
    assert "no manifest exists" in stale["state_reason"]

    skipped = find_run(index, "20260901T020000Z-000002")
    assert skipped["skips_total"] == 3
    assert skipped["skip_kinds"][0] == {"kind": "channel_not_found", "count": 2}
    failed = find_run(index, "20260901T040000Z-000004")
    assert failed["errors_total"] == 1

    overview = status.overview(paths, now=NOW)
    slack_card = next(card for card in overview["cards"] if card["source"] == "slack")
    assert slack_card["states"] == {
        "success": 1,
        "success_with_skips": 1,
        "degraded": 1,
        "failed": 1,
        "running": 1,
        "stale": 1,
    }
    assert [run["run_id"] for run in slack_card["active"]] == [
        "20260902T025500Z-000005",
        "20260901T000000Z-000006",
    ]


def test_a_finished_run_is_summarized_from_its_manifest_not_by_walking_raw(
    paths: status.CollectionPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_manifest(paths, run_id="20260902T000000Z-dddddd")
    write_raw_run(paths, run_id="20260902T000000Z-dddddd", pages=9)

    def explode(path: Path) -> dict[str, Any]:  # pragma: no cover - must not run
        raise AssertionError(f"a completed run must not be walked: {path}")

    monkeypatch.setattr(status, "scan_raw_run", explode)
    run = find_run(status.build_run_index(paths, now=NOW), "20260902T000000Z-dddddd")
    assert run["raw_from_manifest"] is True
    assert run["raw_file_count"] == 2
    assert run["raw_bytes"] == 350


def test_repeat_manifests_for_one_run_collapse_into_one_row(
    paths: status.CollectionPaths,
) -> None:
    write_manifest(paths, run_id="20260901T050000Z-eeeeee", status="failed")
    write_manifest(
        paths, run_id="20260901T050000Z-eeeeee", name="20260901T050000Z-eeeeee.1.json",
        status="success",
    )
    index = status.build_run_index(paths, now=NOW)
    run = find_run(index, "20260901T050000Z-eeeeee")
    assert len([key for key in index.runs if key[2] == "20260901T050000Z-eeeeee"]) == 1
    assert run["manifest_revisions"] == 2
    assert run["state"] == "success"


def test_a_manifest_whose_run_directory_vanished_is_still_reported(
    paths: status.CollectionPaths,
) -> None:
    write_manifest(paths, run_id="20260901T060000Z-ffffff")
    run = find_run(status.build_run_index(paths, now=NOW), "20260901T060000Z-ffffff")
    assert run["state"] == "success"
    assert run.get("raw_run_dir") is None
    view = status.run_view(paths, run)
    assert view["ledger_records"] is None
    assert view["ledger_reason"] == "no ledger file for this run"


# ------------------------------------------------------------- malformed


@pytest.mark.parametrize(
    "raw",
    [
        '{"schema_version": 2, "run_id": "x", "status": "suc',  # partially written
        "[]",  # not an object
        "not json at all",
    ],
)
def test_a_malformed_manifest_is_quarantined_and_never_counted_as_success(
    paths: status.CollectionPaths, raw: str
) -> None:
    write_manifest(paths, run_id="20260901T070000Z-999999", raw=raw)
    index = status.build_run_index(paths, now=NOW)
    run = find_run(index, "20260901T070000Z-999999")
    assert run["state"] == "malformed"
    assert run["malformed"] is True
    assert run["malformed_reason"]
    assert index.manifest_errors and index.manifest_errors[0]["run_id"] == "20260901T070000Z-999999"

    overview = status.overview(paths, now=NOW)
    slack_card = next(card for card in overview["cards"] if card["source"] == "slack")
    assert slack_card["last_success"] is None
    assert slack_card["states"] == {"malformed": 1}


def test_a_manifest_with_an_unrecognised_status_is_not_treated_as_success(
    paths: status.CollectionPaths,
) -> None:
    write_manifest(paths, run_id="20260901T080000Z-888888", status="halfway")
    run = find_run(status.build_run_index(paths, now=NOW), "20260901T080000Z-888888")
    assert run["state"] == "unknown"
    assert "halfway" in run["malformed_reason"]


# ------------------------------------------------------ raw run progress


def test_an_active_run_reports_counts_and_never_raw_content(
    paths: status.CollectionPaths,
) -> None:
    mtime = NOW - timedelta(minutes=3)
    directory = write_raw_run(
        paths, run_id="20260902T024500Z-777777", pages=4, secret="NEVER-SHOW-THIS", mtime=mtime
    )
    expected = sum(entry.stat().st_size for entry in directory.iterdir())
    run = find_run(status.build_run_index(paths, now=NOW), "20260902T024500Z-777777")
    assert run["state"] == "running"
    assert run["raw_file_count"] == 4
    assert run["raw_bytes"] == expected
    assert run["raw_last_mtime"] == mtime.astimezone(timezone.utc).isoformat()
    assert run["raw_scan_truncated"] is False
    assert run["raw_run_dir"] == "raw/slack/production/2026/09/02/20260902T024500Z-777777"

    serialized = json.dumps(status.overview(paths, now=NOW), ensure_ascii=False, default=str)
    assert "NEVER-SHOW-THIS" not in serialized


def test_the_raw_scan_is_capped_and_says_so(
    paths: status.CollectionPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_raw_run(paths, run_id="20260902T024600Z-666666", pages=5, mtime=NOW)
    monkeypatch.setattr(status, "MAX_RAW_ENTRIES_SCANNED", 2)
    status.clear_caches()
    run = find_run(status.build_run_index(paths, now=NOW), "20260902T024600Z-666666")
    assert run["raw_file_count"] == 2
    assert run["raw_scan_truncated"] is True


def test_manifest_skip_and_error_details_are_reduced_to_kinds(
    paths: status.CollectionPaths,
) -> None:
    write_manifest(
        paths,
        run_id="20260901T090000Z-555555",
        status="success_with_skips",
        skips=[{"kind": "channel_not_found", "channel": "C-PRIVATE-SECRET"}],
        errors=[{"kind": "http_500", "detail": "token xoxp-secret leaked into a detail"}],
    )
    run = find_run(status.build_run_index(paths, now=NOW), "20260901T090000Z-555555")
    payload = json.dumps(status.run_view(paths, run), ensure_ascii=False)
    assert "channel_not_found" in payload and "http_500" in payload
    assert "C-PRIVATE-SECRET" not in payload
    assert "xoxp-secret" not in payload


# --------------------------------------------------------- KST boundaries


def test_kst_date_and_weekday_are_computed_at_the_seoul_boundary() -> None:
    last_moment = status.parse_instant("2026-09-01T14:59:59+00:00")
    first_moment = status.parse_instant("2026-09-01T15:00:00+00:00")
    assert status.kst_date(last_moment) == "2026-09-01"
    assert status.kst_date(first_moment) == "2026-09-02"
    assert status.weekday_label(status.parse_iso_date("2026-09-01")) == "화"
    assert status.weekday_label(status.parse_iso_date("2026-08-31")) == "월"
    assert status.weekday_label(status.parse_iso_date("2026-09-06")) == "일"
    start, end = status.kst_day_bounds(status.parse_iso_date("2026-09-01"))
    assert start.isoformat() == "2026-09-01T00:00:00+09:00"
    assert end.isoformat() == "2026-09-02T00:00:00+09:00"
    assert start.astimezone(timezone.utc).isoformat() == "2026-08-31T15:00:00+00:00"


def test_a_run_finishing_just_before_kst_midnight_belongs_to_that_day(
    paths: status.CollectionPaths,
) -> None:
    write_manifest(
        paths,
        run_id="20260901T145959Z-111111",
        started_at="2026-09-01T14:59:00+00:00",
        finished_at="2026-09-01T14:59:59+00:00",
        requested_window={"since": "2026-09-01T14:00:00+00:00"},
    )
    grid = status.coverage(
        paths,
        start=status.parse_iso_date("2026-09-01"),
        end=status.parse_iso_date("2026-09-02"),
        sources=["slack"],
        now=NOW,
    )
    by_date = {row["date"]: row["cells"]["slack"] for row in grid["rows"]}
    assert by_date["2026-09-01"]["coverage"] == "collected"
    assert by_date["2026-09-02"]["coverage"] == "not_collected"


def test_a_run_crossing_kst_midnight_is_attributed_to_both_dates(
    paths: status.CollectionPaths,
) -> None:
    write_manifest(
        paths,
        run_id="20260901T145500Z-222222",
        started_at="2026-09-01T14:55:00+00:00",
        finished_at="2026-09-01T15:10:00+00:00",
        requested_window={"since": "2026-09-01T14:55:00+00:00"},
    )
    grid = status.coverage(
        paths,
        start=status.parse_iso_date("2026-09-01"),
        end=status.parse_iso_date("2026-09-02"),
        sources=["slack"],
        now=NOW,
    )
    by_date = {row["date"]: row["cells"]["slack"] for row in grid["rows"]}
    assert by_date["2026-09-01"]["coverage"] == "collected"
    assert by_date["2026-09-02"]["coverage"] == "collected"
    assert by_date["2026-09-01"]["runs"] == 1 and by_date["2026-09-02"]["runs"] == 1


# ------------------------------------------------------------- coverage


def test_a_date_with_no_evidence_is_not_collected_never_assumed(
    paths: status.CollectionPaths,
) -> None:
    grid = status.coverage(
        paths,
        start=status.parse_iso_date("2026-07-01"),
        end=status.parse_iso_date("2026-07-03"),
        sources=["slack", "notion", "google_calendar"],
        now=NOW,
    )
    for row in grid["rows"]:
        for cell in row["cells"].values():
            assert cell["coverage"] == "not_collected"
            assert cell["rule_versions"] == []
            assert cell["evidence"] == []
            assert cell["completeness"] == "unknown"


def test_legacy_dates_are_reported_as_v0_with_no_run_count(
    paths: status.CollectionPaths,
) -> None:
    write_legacy_day(paths, day="2026-06-10")
    grid = status.coverage(
        paths,
        start=status.parse_iso_date("2026-06-09"),
        end=status.parse_iso_date("2026-06-11"),
        sources=["slack", "notion", "google_calendar"],
        now=NOW,
    )
    by_date = {row["date"]: row["cells"] for row in grid["rows"]}
    for source in ("slack", "notion", "google_calendar"):
        cell = by_date["2026-06-10"][source]
        # A directory is not a claim of coverage; V0 can only ever be unverified.
        assert cell["coverage"] == "unverified"
        assert cell["evidence_class"] == "directory_only"
        assert cell["rule_versions"] == [
            {"version": "V0", "attribution": "legacy", "count": 1}
        ]
        assert cell["runs"] is None and cell["runs_known"] is False
        assert cell["density"] == "day_slice"
        assert cell["completeness"] == "unknown"
        assert any("run identity" in note for note in cell["notes"])
    assert by_date["2026-06-10"]["google_calendar"]["evidence"] == [
        "legacy/claude/weekly/shared/daily_raw/2026-06-10/gcal"
    ]
    assert by_date["2026-06-09"]["slack"]["coverage"] == "not_collected"
    assert grid["legacy_inventory"]["observed"]["slack"] == {
        "first": "2026-06-10",
        "last": "2026-06-10",
        "dates": 1,
    }


def test_legacy_truncation_warnings_make_a_date_partial(
    paths: status.CollectionPaths,
) -> None:
    write_legacy_day(
        paths,
        day="2026-06-12",
        sources=("slack",),
        meta={"truncation_warnings": [{"where": "channel_history"}], "rate_limit_hits": 9},
    )
    grid = status.coverage(
        paths,
        start=status.parse_iso_date("2026-06-12"),
        end=status.parse_iso_date("2026-06-12"),
        sources=["slack"],
        now=NOW,
    )
    cell = grid["rows"][0]["cells"]["slack"]
    assert cell["coverage"] == "partial"
    assert cell["completeness"] == "incomplete"
    assert cell["legacy_meta"][0]["declared_status_is_evidence"] is False
    assert cell["legacy_meta"][0]["rate_limit_hits"] == 9


def test_a_bounded_run_stops_observing_where_its_window_ends(
    paths: status.CollectionPaths,
) -> None:
    """A date slice must not be attributed to the days after it.

    The run's window end came from `finished_at`, so a slice covering one KST
    day in August looked like it had observed everything from that day up to
    the moment the process exited. The grid attributes a run to every date its
    window touches, so a single 8/19 slice counted toward 8/20 onward and a run
    that never looked at a date got a vote on that date's verdict.
    """
    write_manifest(
        paths,
        source="notion",
        run_id="20260901T120000Z-51ce01",
        started_at="2026-09-01T12:00:00+00:00",
        finished_at="2026-09-01T13:00:00+00:00",
        requested_window={
            "since_effective": "2026-08-19T00:00:00+09:00",
            "until": "2026-08-20T00:00:00+09:00",
            "mode": "date_slice",
        },
    )
    index = status.build_run_index(paths, now=NOW)
    run = find_run(index, "20260901T120000Z-51ce01")
    assert run["window"]["end"] == "2026-08-20T00:00:00+09:00"
    assert run["window"]["end_is_declared"] is True

    grid = status.coverage(
        paths,
        start=status.parse_iso_date("2026-08-19"),
        end=status.parse_iso_date("2026-08-22"),
        sources=["notion"],
        now=NOW,
        index=index,
    )
    runs_by_date = {row["date"]: row["cells"]["notion"]["runs"] for row in grid["rows"]}
    assert runs_by_date["2026-08-19"] == 1
    for iso in ("2026-08-20", "2026-08-21", "2026-08-22"):
        assert not runs_by_date[iso], f"{iso} was never observed by this run"


def test_an_unbounded_run_still_ends_when_it_finished(
    paths: status.CollectionPaths,
) -> None:
    """Only a declared `until` moves the end; incremental runs are unchanged."""
    write_manifest(
        paths,
        source="notion",
        run_id="20260901T120000Z-51ce02",
        started_at="2026-09-01T12:00:00+00:00",
        finished_at="2026-09-01T13:00:00+00:00",
        requested_window={"since_effective": "2026-09-01T00:00:00+00:00"},
    )
    run = find_run(status.build_run_index(paths, now=NOW), "20260901T120000Z-51ce02")
    assert run["window"]["end"] == "2026-09-01T13:00:00+00:00"
    assert run["window"]["end_is_declared"] is False


def test_a_wide_range_says_it_did_not_read_the_legacy_record(
    paths: status.CollectionPaths,
) -> None:
    """The query's width must not silently change a date's verdict.

    The same date reads `partial` in a narrow window, because meta.json records
    a truncation warning. Widen the window past the probe limit and that file is
    never opened, so the truncation branch cannot be reached. Reporting
    `unverified` there would say the record was read and proved nothing. The
    cell has to say instead that nobody looked.
    """
    write_legacy_day(
        paths,
        day="2026-06-12",
        sources=("slack",),
        meta={"truncation_warnings": [{"where": "channel_history"}]},
    )
    narrow = status.coverage(
        paths,
        start=status.parse_iso_date("2026-06-12"),
        end=status.parse_iso_date("2026-06-12"),
        sources=["slack"],
        now=NOW,
    )
    assert narrow["legacy_meta_probed"] is True
    narrow_cell = narrow["rows"][0]["cells"]["slack"]
    assert narrow_cell["coverage"] == "partial"
    assert narrow_cell["legacy_meta_probed"] is True

    span = status.MAX_LEGACY_META_PROBE_DAYS + 30
    wide = status.coverage(
        paths,
        start=status.parse_iso_date("2026-06-12") - timedelta(days=span),
        end=status.parse_iso_date("2026-06-12"),
        sources=["slack"],
        now=NOW,
    )
    assert wide["legacy_meta_probed"] is False
    assert wide["legacy_meta_probe_limit_days"] == status.MAX_LEGACY_META_PROBE_DAYS
    assert wide["requested_span_days"] > status.MAX_LEGACY_META_PROBE_DAYS
    wide_cell = next(
        row["cells"]["slack"] for row in wide["rows"] if row["date"] == "2026-06-12"
    )
    assert wide_cell["coverage"] == status.COVERAGE_UNEXAMINED
    assert wide_cell["coverage"] != status.COVERAGE_UNVERIFIED
    assert wide_cell["legacy_meta_probed"] is False
    assert wide_cell["legacy_meta"] == []
    assert any("읽지 않았습니다" in note for note in wide_cell["notes"])


def test_a_probed_legacy_date_without_truncation_stays_unverified(
    paths: status.CollectionPaths,
) -> None:
    """Having looked and found no truncation is not a claim of coverage.

    It is also not the same as not having looked, which is the whole point of
    keeping `unexamined` apart from `unverified`.
    """
    write_legacy_day(paths, day="2026-06-14", sources=("slack",))
    grid = status.coverage(
        paths,
        start=status.parse_iso_date("2026-06-14"),
        end=status.parse_iso_date("2026-06-14"),
        sources=["slack"],
        now=NOW,
    )
    cell = grid["rows"][0]["cells"]["slack"]
    assert cell["coverage"] == status.COVERAGE_UNVERIFIED
    assert cell["legacy_meta_probed"] is True
    assert cell["legacy_meta"], "the record was read, so it belongs in the cell"


def test_a_run_backed_cell_reports_the_legacy_probe_as_not_applicable(
    paths: status.CollectionPaths,
) -> None:
    """False would claim something is unknown that is not."""
    write_manifest(
        paths, source="slack", run_id="20260901T000000Z-aaaaaa",
        started_at="2026-09-01T02:00:00+00:00",
    )
    grid = status.coverage(
        paths,
        start=status.parse_iso_date("2026-09-01"),
        end=status.parse_iso_date("2026-09-01"),
        sources=["slack"],
        now=NOW,
    )
    cell = grid["rows"][0]["cells"]["slack"]
    assert cell["runs"] == 1
    assert cell["legacy_meta_probed"] is None


def test_the_weekday_rollup_counts_the_dates_nobody_examined(
    paths: status.CollectionPaths,
) -> None:
    """A rollup must not let unexamined dates vanish into a total."""
    write_legacy_day(paths, day="2026-06-10", sources=("slack",))
    span = status.MAX_LEGACY_META_PROBE_DAYS + 30
    grid = status.coverage(
        paths,
        start=status.parse_iso_date("2026-06-10") - timedelta(days=span),
        end=status.parse_iso_date("2026-06-10"),
        sources=["slack"],
        group="weekday",
        now=NOW,
    )
    unexamined = sum(
        bucket["cells"]["slack"]["dates_unexamined"] for bucket in grid["weekday_rows"]
    )
    assert unexamined == 1
    counts = {
        state
        for bucket in grid["weekday_rows"]
        for state in bucket["cells"]["slack"]["coverage_counts"]
    }
    assert status.COVERAGE_UNEXAMINED in counts


def test_an_incomplete_legacy_inventory_reports_unknown_not_missing(
    paths: status.CollectionPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_legacy_day(paths, day="2026-06-13", sources=("slack",))
    monkeypatch.setattr(status, "LEGACY_INVENTORY_MAX_SECONDS", -1.0)
    status.clear_caches()
    grid = status.coverage(
        paths,
        start=status.parse_iso_date("2026-06-20"),
        end=status.parse_iso_date("2026-06-20"),
        sources=["slack"],
        now=NOW,
    )
    cell = grid["rows"][0]["cells"]["slack"]
    assert grid["legacy_inventory"]["complete"] is False
    assert cell["coverage"] == "unknown"
    assert cell["runs_known"] is False
    assert any("absence is not evidence" in note for note in cell["notes"])


def test_a_stale_run_alone_makes_a_date_unknown_rather_than_collected(
    paths: status.CollectionPaths,
) -> None:
    write_raw_run(
        paths, run_id="20260830T000000Z-444444", day="2026/08/30",
        mtime=NOW - timedelta(days=3),
    )
    grid = status.coverage(
        paths,
        start=status.parse_iso_date("2026-08-30"),
        end=status.parse_iso_date("2026-08-30"),
        sources=["slack"],
        now=NOW,
    )
    cell = grid["rows"][0]["cells"]["slack"]
    assert cell["coverage"] == "unknown"
    assert cell["states"] == ["stale"]
    assert any("no manifest" in note for note in cell["notes"])


def test_a_malformed_manifest_makes_a_date_unknown(paths: status.CollectionPaths) -> None:
    write_manifest(paths, run_id="20260828T120000Z-333333", raw="{broken")
    grid = status.coverage(
        paths,
        start=status.parse_iso_date("2026-08-28"),
        end=status.parse_iso_date("2026-08-28"),
        sources=["slack"],
        now=NOW,
    )
    cell = grid["rows"][0]["cells"]["slack"]
    assert cell["coverage"] == "unknown"
    assert any("quarantined" in note for note in cell["notes"])


def test_weekday_grouping_rolls_dates_up_by_kst_weekday(
    paths: status.CollectionPaths,
) -> None:
    write_manifest(
        paths,
        run_id="20260901T020000Z-abcdef",
        started_at="2026-09-01T02:00:00+00:00",
        finished_at="2026-09-01T02:10:00+00:00",
        requested_window={"since": "2026-09-01T02:00:00+00:00"},
    )
    grid = status.coverage(
        paths,
        start=status.parse_iso_date("2026-08-31"),
        end=status.parse_iso_date("2026-09-06"),
        sources=["slack"],
        group="weekday",
        now=NOW,
    )
    assert grid["group"] == "weekday"
    rows = {row["weekday"]: row for row in grid["weekday_rows"]}
    assert list(rows) == ["월", "화", "수", "목", "금", "토", "일"]
    tuesday = rows["화"]["cells"]["slack"]
    assert tuesday["coverage_counts"] == {"collected": 1}
    assert tuesday["runs"] == 1
    assert tuesday["rule_versions"] == {f"{ACTIVE_RULE_VERSION}·declared": 1}
    monday = rows["월"]["cells"]["slack"]
    assert monday["coverage_counts"] == {"not_collected": 1}
    assert monday["dates_not_collected"] == 1


def test_the_coverage_range_is_capped(
    paths: status.CollectionPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(status, "MAX_COVERAGE_DAYS", 3)
    grid = status.coverage(
        paths,
        start=status.parse_iso_date("2026-01-01"),
        end=status.parse_iso_date("2026-09-01"),
        sources=["slack"],
        now=NOW,
    )
    assert grid["range_truncated"] is True
    assert len(grid["rows"]) == 3
    assert grid["start"] == "2026-08-30" and grid["end"] == "2026-09-01"


# ------------------------------------------------------ progress snapshots


def test_a_live_snapshot_reports_a_run_that_has_written_no_manifest(
    paths: status.CollectionPaths,
) -> None:
    write_snapshot(paths, updated_at=(NOW - timedelta(minutes=2)).isoformat())
    run = find_run(status.build_run_index(paths, now=NOW), "20260902T020000Z-cccccc")
    assert run["state"] == "running"
    assert run["source"] == "notion"
    assert run["raw_file_count"] == 42
    assert run["raw_bytes"] == 4242
    assert run["progress"]["liveness"]["state"] == "running"
    assert run["progress"]["liveness"]["process_alive"] is True


def test_a_snapshot_whose_process_is_gone_is_stale_immediately(
    paths: status.CollectionPaths,
) -> None:
    """A crashed capture must not look alive until its heartbeat times out."""
    dead = 2
    while dead < 200_000:
        try:
            os.kill(dead, 0)
        except ProcessLookupError:
            break
        except PermissionError:
            pass
        dead += 1
    write_snapshot(paths, pid=dead, updated_at=(NOW - timedelta(seconds=30)).isoformat())
    run = find_run(status.build_run_index(paths, now=NOW), "20260902T020000Z-cccccc")
    assert run["state"] == "stale"
    assert "process that wrote this snapshot is gone" in run["state_reason"]


def test_a_snapshot_that_stopped_advancing_is_stale(paths: status.CollectionPaths) -> None:
    write_snapshot(paths, pid=None, host="another-host", updated_at="2026-09-01T00:00:00+00:00")
    run = find_run(status.build_run_index(paths, now=NOW), "20260902T020000Z-cccccc")
    assert run["state"] == "stale"
    assert "no progress for" in run["state_reason"]


def test_a_finished_snapshot_never_overrides_the_manifest(
    paths: status.CollectionPaths,
) -> None:
    write_manifest(
        paths, source="notion", run_id="20260902T020000Z-cccccc", status="success_with_skips",
        capture_profile="live-notion-api/v1",
    )
    write_snapshot(
        paths,
        phase="finished",
        status="success_with_skips",
        ledger={"records_written": 812, "schema_errors": 0, "output_path": "ledger/notion/x.jsonl"},
    )
    run = find_run(status.build_run_index(paths, now=NOW), "20260902T020000Z-cccccc")
    assert run["state"] == "success_with_skips"
    assert run["ledger_records"] == 812
    assert run["ledger_schema_errors"] == 0
    view = status.run_view(paths, run)
    assert view["ledger_schema_errors_known"] is True


def test_ledger_schema_errors_are_unknown_when_nothing_recorded_them(
    paths: status.CollectionPaths,
) -> None:
    write_manifest(paths, run_id="20260901T100000Z-aaaa11")
    run = find_run(status.build_run_index(paths, now=NOW), "20260901T100000Z-aaaa11")
    view = status.run_view(paths, run)
    assert view["ledger_schema_errors"] is None
    assert view["ledger_schema_errors_known"] is False


def test_ledger_records_are_counted_from_the_run_jsonl(
    paths: status.CollectionPaths,
) -> None:
    write_manifest(paths, run_id="20260901T110000Z-aaaa22")
    target = paths.ledger_root / "ledger" / "slack" / "live-20260901T110000Z-aaaa22.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("".join(f'{{"n": {index}}}\n' for index in range(7)), encoding="utf-8")
    run = find_run(status.build_run_index(paths, now=NOW), "20260901T110000Z-aaaa22")
    view = status.run_view(paths, run)
    assert view["ledger_records"] == 7
    assert view["ledger_path"] == "staging/ledger/ledger/slack/live-20260901T110000Z-aaaa22.jsonl"


# --------------------------------------------------------- path handling


@pytest.mark.parametrize(
    "name",
    ["..", ".", "../etc", "a/b", "", ".hidden", "-dash", "/absolute", "x" * 121, "sp ace"],
)
def test_unsafe_directory_names_are_never_joined(
    paths: status.CollectionPaths, name: str
) -> None:
    assert status.safe_child(paths.archive_root, name, root=paths.archive_root) is None


def test_within_rejects_a_path_outside_its_root(paths: status.CollectionPaths) -> None:
    assert status.within(paths.archive_root / "raw" / "slack", paths.archive_root)
    assert status.within(paths.archive_root, paths.archive_root)
    assert not status.within(Path("/etc/passwd"), paths.archive_root)
    assert not status.within(paths.archive_root / ".." / "elsewhere", paths.archive_root)


def test_an_unsafely_named_environment_directory_is_ignored(
    paths: status.CollectionPaths,
) -> None:
    write_manifest(paths, environment="production", run_id="20260901T120000Z-aaaa33")
    hostile = paths.manifest_root / "slack" / ".."
    hostile.mkdir(parents=True, exist_ok=True)
    weird = paths.manifest_root / "slack" / "-injected"
    weird.mkdir(parents=True, exist_ok=True)
    (weird / "20260901T130000Z-aaaa44.json").write_text("{}", encoding="utf-8")
    index = status.build_run_index(paths, now=NOW)
    assert {key[1] for key in index.runs} == {"production"}


def test_a_symlinked_environment_directory_is_not_followed(
    paths: status.CollectionPaths, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    (outside / "slack" / "production").mkdir(parents=True)
    (outside / "slack" / "production" / "20260901T140000Z-aaaa55.json").write_text(
        "{}", encoding="utf-8"
    )
    (paths.manifest_root / "slack").mkdir(parents=True, exist_ok=True)
    os.symlink(outside / "slack" / "production", paths.manifest_root / "slack" / "escape")
    index = status.build_run_index(paths, now=NOW)
    assert index.runs == {}
    assert index.environments.get("slack", set()) == set()


def test_the_reader_only_looks_at_sources_the_registry_declares(
    paths: status.CollectionPaths,
) -> None:
    """A directory for a source with no rule is not a run the dashboard reports.

    `slurm` rather than a literal count: the point is that the reader follows
    the registry, so this test must keep meaning the same thing as sources are
    added. It was written against `github`, which stopped being unknown the
    moment V4 declared it.
    """
    # A name that cannot become a source, rather than one that has not yet.
    # This test was written against `github`, then `slurm`, and both were
    # declared within the hour -- each time it quietly stopped testing anything.
    unknown = "not-a-collected-source"
    assert unknown not in status.COLLECTOR_SOURCES
    directory = paths.manifest_root / unknown / "production"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "20260901T150000Z-aaaa66.json").write_text("{}", encoding="utf-8")
    index = status.build_run_index(paths, now=NOW)
    assert index.runs == {}


def test_checkpoint_and_link_queue_files_are_not_mistaken_for_runs(
    paths: status.CollectionPaths,
) -> None:
    directory = paths.manifest_root / "slack" / "production"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "checkpoint.json").write_text('{"run_id": "x"}', encoding="utf-8")
    (directory / "link-queue.json").write_text("{}", encoding="utf-8")
    (directory / "checkpoints").mkdir(exist_ok=True)
    write_manifest(paths, run_id="20260901T160000Z-aaaa77")
    index = status.build_run_index(paths, now=NOW)
    assert [key[2] for key in index.runs] == ["20260901T160000Z-aaaa77"]


def test_list_runs_filters_and_orders_by_last_activity(paths: status.CollectionPaths) -> None:
    write_manifest(paths, run_id="20260901T170000Z-aaaa88", environment="production")
    write_manifest(
        paths,
        run_id="20260901T180000Z-aaaa99",
        environment="test",
        started_at="2026-09-01T18:00:00+00:00",
        finished_at="2026-09-01T18:01:00+00:00",
    )
    write_manifest(paths, source="notion", run_id="20260901T190000Z-aaab00",
                   capture_profile="live-notion-api/v1")
    listed = status.list_runs(paths, source="slack", limit=10, now=NOW)
    assert [item["run_id"] for item in listed["items"]] == [
        "20260901T180000Z-aaaa99",
        "20260901T170000Z-aaaa88",
    ]
    scoped = status.list_runs(paths, source="slack", environment="test", limit=10, now=NOW)
    assert [item["run_id"] for item in scoped["items"]] == ["20260901T180000Z-aaaa99"]
    assert listed["environments"]["slack"] == ["production", "test"]


# ------------------------------- coverage verdict: evidence grades (defect fix)


def test_a_legacy_only_date_is_unverified_not_collected(
    paths: status.CollectionPaths,
) -> None:
    """A V0 directory proves a dump exists, not that it was complete."""
    write_legacy_day(paths, day="2026-06-14", sources=("slack",))
    grid = status.coverage(
        paths, start=status.parse_iso_date("2026-06-14"),
        end=status.parse_iso_date("2026-06-14"), sources=["slack"], now=NOW,
    )
    cell = grid["rows"][0]["cells"]["slack"]
    assert cell["coverage"] == "unverified"
    assert cell["completeness"] == "unknown"
    assert cell["evidence_class"] == "directory_only"
    # `unknown` means evidence exists but could not be parsed; the two must not merge.
    assert cell["coverage"] != status.COVERAGE_UNKNOWN
    assert any("run identity" in note for note in cell["notes"])


def test_legacy_truncation_warnings_still_make_a_date_partial(
    paths: status.CollectionPaths,
) -> None:
    write_legacy_day(
        paths, day="2026-06-15", sources=("slack",),
        meta={"truncation_warnings": [{"where": "channel_history"}]},
    )
    grid = status.coverage(
        paths, start=status.parse_iso_date("2026-06-15"),
        end=status.parse_iso_date("2026-06-15"), sources=["slack"], now=NOW,
    )
    cell = grid["rows"][0]["cells"]["slack"]
    assert cell["coverage"] == "partial"
    assert cell["completeness"] == "incomplete"


def _one_day(paths: status.CollectionPaths, day: str = "2026-09-01") -> dict[str, Any]:
    grid = status.coverage(
        paths, start=status.parse_iso_date(day), end=status.parse_iso_date(day),
        sources=["slack"], now=NOW,
    )
    return grid["rows"][0]["cells"]["slack"]


def test_a_run_that_only_succeeded_is_collected(paths: status.CollectionPaths) -> None:
    # finished_at is past 2026-09-01 24:00 KST (15:00Z) so the time axis is
    # complete and this test measures observation quality alone.
    write_manifest(paths, run_id="20260901T010000Z-c0f001", status="success",
                   finished_at="2026-09-01T16:00:00+00:00")
    cell = _one_day(paths)
    assert cell["coverage"] == "collected"
    assert cell["completeness"] == "complete"
    assert cell["evidence_class"] == "manifest"


def test_a_run_with_named_skips_is_collected_with_skips_not_partial(
    paths: status.CollectionPaths,
) -> None:
    """Naming what it could not reach is honest reporting, not a defect."""
    write_manifest(
        paths, run_id="20260901T010100Z-c0f002", status="success_with_skips",
        skips=[{"kind": "channel_not_found"}, {"kind": "channel_not_found"},
               {"kind": "is_archived"}],
        # Past 24:00 KST, so the time axis is complete and only quality is under test.
        finished_at="2026-09-01T16:00:00+00:00",
    )
    cell = _one_day(paths)
    assert cell["coverage"] == "collected_with_skips"
    assert cell["completeness"] == "complete_with_known_gaps"
    assert cell["evidence_class"] == "manifest"
    joined = " ".join(cell["notes"])
    assert "3건" in joined and "channel_not_found 2" in joined


def test_skips_plus_truncation_is_still_partial(paths: status.CollectionPaths) -> None:
    write_manifest(
        paths, run_id="20260901T010200Z-c0f003", status="success_with_skips",
        skips=[{"kind": "channel_not_found"}], truncated=True,
        truncation=[{"reason": "max_messages"}],
    )
    cell = _one_day(paths)
    assert cell["coverage"] == "partial"
    assert cell["completeness"] == "incomplete"


def test_a_degraded_run_is_a_stronger_signal_than_skips(
    paths: status.CollectionPaths,
) -> None:
    write_manifest(paths, run_id="20260901T010300Z-c0f004", status="degraded")
    cell = _one_day(paths)
    assert cell["coverage"] == "partial"
    assert cell["completeness"] == "incomplete"


def test_a_date_with_both_v1_and_v0_keeps_the_v1_verdict_and_reports_mixed(
    paths: status.CollectionPaths,
) -> None:
    write_manifest(paths, run_id="20260901T010400Z-c0f005", status="success")
    write_legacy_day(paths, day="2026-09-01", sources=("slack",))
    cell = _one_day(paths)
    assert cell["coverage"] == "collected"
    assert cell["evidence_class"] == "mixed"
    versions = {(entry["version"], entry["attribution"]) for entry in cell["rule_versions"]}
    assert (ACTIVE_RULE_VERSION, "declared") in versions and ("V0", "legacy") in versions
    assert cell["runs"] == 1


@pytest.mark.parametrize(
    "kind,expected",
    [("running", "running"), ("failed", "failed"), ("malformed", "unknown")],
)
def test_the_other_verdicts_are_unchanged(
    paths: status.CollectionPaths, kind: str, expected: str
) -> None:
    """Regression: only the skips and legacy verdicts moved."""
    if kind == "running":
        write_raw_run(paths, run_id="20260902T020500Z-c0f006", day="2026/09/02",
                      mtime=NOW - timedelta(minutes=1))
        day = "2026-09-02"
    elif kind == "failed":
        write_manifest(paths, run_id="20260901T010600Z-c0f007", status="failed")
        day = "2026-09-01"
    else:
        write_manifest(paths, run_id="20260901T010700Z-c0f008", raw="{broken")
        day = "2026-09-01"
    assert _one_day(paths, day=day)["coverage"] == expected


def test_a_stale_run_alone_is_still_unknown(paths: status.CollectionPaths) -> None:
    write_raw_run(paths, run_id="20260901T010800Z-c0f009", day="2026/09/01",
                  mtime=NOW - timedelta(days=3))
    cell = _one_day(paths)
    assert cell["coverage"] == "unknown"
    # A crashed run wrote no manifest, so the only evidence is the directory
    # it left behind. Calling that `manifest` would be the same over-claim
    # this module exists to prevent.
    assert cell["evidence_class"] == "directory_only"


def test_a_date_with_no_evidence_reports_no_evidence_class(
    paths: status.CollectionPaths,
) -> None:
    cell = _one_day(paths, day="2026-07-20")
    assert cell["coverage"] == "not_collected"
    assert cell["evidence_class"] is None


def test_a_running_run_is_directory_evidence_not_manifest_evidence(
    paths: status.CollectionPaths,
) -> None:
    """A run in flight has written no manifest; the cell must say so."""
    write_raw_run(paths, run_id="20260902T020600Z-c0f010", day="2026/09/02",
                  mtime=NOW - timedelta(minutes=1))
    cell = _one_day(paths, day="2026-09-02")
    assert cell["coverage"] == "running"
    assert cell["evidence_class"] == "directory_only"


def test_a_finished_and_an_unfinished_run_on_one_date_is_mixed_evidence(
    paths: status.CollectionPaths,
) -> None:
    write_manifest(paths, run_id="20260901T010900Z-c0f011", status="success")
    write_raw_run(paths, run_id="20260901T011000Z-c0f012", day="2026/09/01",
                  mtime=NOW - timedelta(days=3))
    cell = _one_day(paths)
    assert cell["evidence_class"] == "mixed"


def test_degraded_wins_over_skips_on_the_same_date(paths: status.CollectionPaths) -> None:
    """The precedence DEFECT 2 turns on, with both states actually present."""
    write_manifest(paths, run_id="20260901T011100Z-c0f013", status="success_with_skips",
                   skips=[{"kind": "channel_not_found"}])
    write_manifest(paths, run_id="20260901T011200Z-c0f014", status="degraded")
    cell = _one_day(paths)
    assert cell["coverage"] == "partial"
    assert cell["completeness"] == "incomplete"


def test_a_capped_skip_breakdown_says_it_is_partial(
    paths: status.CollectionPaths,
) -> None:
    """A truncated per-kind list must not read as the whole story."""
    write_manifest(
        paths, run_id="20260901T011300Z-c0f015", status="success_with_skips",
        skips=[{"kind": f"kind_{index:02d}"} for index in range(12)],
    )
    cell = _one_day(paths)
    assert cell["coverage"] == "collected_with_skips"
    joined = " ".join(cell["notes"])
    assert "12건" in joined
    assert "외" in joined and "종" in joined, joined


def test_the_skip_breakdown_survives_hostile_manifest_entries(
    paths: status.CollectionPaths,
) -> None:
    write_manifest(
        paths, run_id="20260901T011400Z-c0f016", status="success_with_skips",
        skips=["a string", 12, None, {"no_kind": 1}, {"kind": ""}],
    )
    cell = _one_day(paths)
    assert cell["coverage"] == "collected_with_skips"
    assert "5건" in " ".join(cell["notes"])


# ------------------------------------- time coverage: a date is not a window

def _cell(paths: status.CollectionPaths, day: str, *, now: datetime = NOW) -> dict[str, Any]:
    grid = status.coverage(
        paths, start=status.parse_iso_date(day), end=status.parse_iso_date(day),
        sources=["slack"], now=now,
    )
    return grid["rows"][0]["cells"]["slack"]


def test_today_can_never_be_complete_however_clean_the_run(
    paths: status.CollectionPaths,
) -> None:
    """The hours that have not happened yet cannot have been collected."""
    write_manifest(
        paths, run_id="20260902T020000Z-t1m001", status="success",
        started_at="2026-09-02T02:00:00+00:00", finished_at="2026-09-02T02:30:00+00:00",
        requested_window={"since": "2026-09-01T00:00:00+00:00"},
    )
    cell = _cell(paths, "2026-09-02")
    assert cell["time_coverage"] == "in_progress"
    assert cell["completeness"] == "in_progress"
    assert cell["completeness"] != "complete"
    assert cell["observed_through"].startswith("2026-09-02T02:30")
    assert any("아직 끝나지 않았" in note for note in cell["notes"])


def test_a_finished_date_observed_past_its_end_is_complete(
    paths: status.CollectionPaths,
) -> None:
    # 2026-09-01 24:00 KST == 2026-09-01T15:00Z; this run ends after it.
    write_manifest(
        paths, run_id="20260901T160000Z-t1m002", status="success",
        started_at="2026-09-01T16:00:00+00:00", finished_at="2026-09-01T16:10:00+00:00",
        requested_window={"since": "2026-08-31T00:00:00+00:00"},
    )
    cell = _cell(paths, "2026-09-01")
    assert cell["time_coverage"] == "complete"
    assert cell["coverage"] == "collected"
    assert cell["completeness"] == "complete"


def test_a_finished_date_whose_tail_nobody_watched_is_partial_and_says_when(
    paths: status.CollectionPaths,
) -> None:
    # Ends 2026-09-01T13:00Z == 22:00 KST, before that date's 24:00 KST.
    write_manifest(
        paths, run_id="20260901T130000Z-t1m003", status="success",
        started_at="2026-09-01T12:00:00+00:00", finished_at="2026-09-01T13:00:00+00:00",
        requested_window={"since": "2026-08-31T00:00:00+00:00"},
    )
    cell = _cell(paths, "2026-09-01")
    assert cell["time_coverage"] == "partial"
    assert cell["completeness"] == "incomplete"
    assert "22:00" in " ".join(cell["notes"])


def test_skips_and_an_unfinished_day_combine(paths: status.CollectionPaths) -> None:
    write_manifest(
        paths, run_id="20260902T020100Z-t1m004", status="success_with_skips",
        skips=[{"kind": "channel_not_found"}],
        started_at="2026-09-02T02:00:00+00:00", finished_at="2026-09-02T02:01:00+00:00",
        requested_window={"since": "2026-09-01T00:00:00+00:00"},
    )
    cell = _cell(paths, "2026-09-02")
    assert cell["coverage"] == "collected_with_skips"
    assert cell["time_coverage"] == "in_progress"
    assert cell["completeness"] == "in_progress"


def test_a_legacy_only_date_has_no_time_axis(paths: status.CollectionPaths) -> None:
    """V0 has no observation window; inventing one would be a false claim."""
    write_legacy_day(paths, day="2026-06-20", sources=("slack",))
    cell = _cell(paths, "2026-06-20")
    assert cell["coverage"] == "unverified"
    assert cell["time_coverage"] is None
    assert cell["observed_through"] is None


def test_a_date_with_no_evidence_reports_no_time_axis(
    paths: status.CollectionPaths,
) -> None:
    cell = _cell(paths, "2026-07-21")
    assert cell["coverage"] == "not_collected"
    assert cell["time_coverage"] is None


def test_the_utc_to_kst_boundary_is_respected_for_the_day_end(
    paths: status.CollectionPaths,
) -> None:
    """2026-09-01 24:00 KST is 2026-09-01T15:00Z, not midnight UTC."""
    write_manifest(
        paths, run_id="20260901T145900Z-t1m005", status="success",
        started_at="2026-09-01T14:00:00+00:00", finished_at="2026-09-01T14:59:00+00:00",
        requested_window={"since": "2026-08-31T00:00:00+00:00"},
    )
    # 14:59Z is 23:59 KST — one minute short of the day's end.
    assert _cell(paths, "2026-09-01")["time_coverage"] == "partial"

    write_manifest(
        paths, run_id="20260901T150100Z-t1m006", status="success",
        started_at="2026-09-01T15:00:00+00:00", finished_at="2026-09-01T15:01:00+00:00",
        requested_window={"since": "2026-08-31T00:00:00+00:00"},
    )
    assert _cell(paths, "2026-09-01")["time_coverage"] == "complete"


def test_quality_verdicts_are_not_disturbed_by_the_time_axis(
    paths: status.CollectionPaths,
) -> None:
    """Regression: a degraded run stays partial regardless of the clock."""
    write_manifest(
        paths, run_id="20260901T160100Z-t1m007", status="degraded",
        started_at="2026-09-01T16:00:00+00:00", finished_at="2026-09-01T16:10:00+00:00",
    )
    cell = _cell(paths, "2026-09-01")
    assert cell["coverage"] == "partial"
    assert cell["completeness"] == "incomplete"
    assert cell["time_coverage"] == "complete"


# ------------------------------------------------- archives beside the live one


def _backfill_manifest(root: Path, *, source: str, run_id: str, **overrides: Any) -> Path:
    """A manifest in a backfill archive, written the way a backfill writes one."""
    payload: dict[str, Any] = {
        "schema_version": 2,
        "source": source,
        "environment": "production",
        "run_id": run_id,
        "status": "success",
        "capture_profile": "live-slack-web-api/v1",
        "capture_density": "day_slice",
        "dry_run": False,
        "started_at": "2026-08-05T00:00:00+00:00",
        "finished_at": "2026-08-05T00:30:00+00:00",
        "requested_window": {"since": "2026-08-01T00:00:00+00:00"},
        "files": [],
        **active_rule_stamp(),
    }
    payload.update(overrides)
    directory = root / "manifests" / source / "production"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{run_id}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def test_a_backfill_archive_beside_the_live_one_is_read_too(
    paths: status.CollectionPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """August lives in its own root, and a view that reads one root calls it missing."""
    write_manifest(paths, run_id="20260901T000000Z-aaaaaa")
    _backfill_manifest(
        paths.archive_root / "backfill-2026-08", source="slack", run_id="20260805T000000Z-bbbbbb"
    )
    status.clear_caches()
    rescanned = status.paths_from_environment()

    index = status.build_run_index(rescanned, include_active=False)
    assert {run["run_id"] for run in index.runs.values()} == {
        "20260901T000000Z-aaaaaa",
        "20260805T000000Z-bbbbbb",
    }


def test_the_view_names_every_archive_it_read(
    paths: status.CollectionPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """So "nothing here" can be told apart from "we never looked here"."""
    _backfill_manifest(
        paths.archive_root / "backfill-2026-08", source="slack", run_id="20260805T000000Z-bbbbbb"
    )
    status.clear_caches()
    rescanned = status.paths_from_environment()

    payload = status.overview(rescanned)
    assert payload["roots"]["archive_roots"] == [
        str(rescanned.archive_root),
        str(rescanned.archive_root / "backfill-2026-08"),
    ]


def test_a_directory_without_manifests_is_not_taken_for_an_archive(
    paths: status.CollectionPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Discovery has to be bounded, or any stray directory becomes evidence."""
    (paths.archive_root / "backfill-nothing-here").mkdir(parents=True, exist_ok=True)
    (paths.archive_root / "not-a-backfill" / "manifests").mkdir(parents=True, exist_ok=True)
    status.clear_caches()
    rescanned = status.paths_from_environment()

    assert rescanned.extra_archive_roots == ()


def test_the_archive_list_can_be_set_and_can_be_set_to_nothing(
    paths: status.CollectionPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """"Read only the live root" has to stay sayable, so an empty setting wins."""
    _backfill_manifest(
        paths.archive_root / "backfill-2026-08", source="slack", run_id="20260805T000000Z-bbbbbb"
    )
    monkeypatch.setenv("BACKFILL_ARCHIVE_ROOTS", "")
    status.clear_caches()
    assert status.paths_from_environment().extra_archive_roots == ()

    elsewhere = paths.archive_root.parent / "elsewhere"
    monkeypatch.setenv("BACKFILL_ARCHIVE_ROOTS", str(elsewhere))
    status.clear_caches()
    assert status.paths_from_environment().extra_archive_roots == (elsewhere,)


def test_a_run_says_which_archive_it_came_from(
    paths: status.CollectionPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    backfill = paths.archive_root / "backfill-2026-08"
    _backfill_manifest(backfill, source="slack", run_id="20260805T000000Z-bbbbbb")
    status.clear_caches()
    rescanned = status.paths_from_environment()

    index = status.build_run_index(rescanned, include_active=False)
    roots = {run["run_id"]: run.get("archive_root") for run in index.runs.values()}
    assert roots["20260805T000000Z-bbbbbb"] == str(backfill)


def test_a_stamped_rule_that_does_not_name_the_source_is_said_so_not_filled_in(
    paths: status.CollectionPaths,
) -> None:
    """The August GitHub runs stamped V2, and V2 does not name GitHub.

    The manifest is honest and must not be rewritten, so the view has to be able
    to report the gap rather than failing on it or quietly substituting the
    current rule.
    """
    stamped = status.classify_rule(
        {"collection_rule_version": "V2", "collection_rule_digest": None},
        manifest_relative_path="manifests/github/production/x.json",
        source="github",
        started_at=None,
        run_id="20260902T140337Z-0aa0aa98d8ce",
    )
    assert stamped["attribution"] == "declared"
    assert stamped["version"] == "V2"
    assert stamped["declares_source"] is False

    slack = status.classify_rule(
        {"collection_rule_version": "V2", "collection_rule_digest": None},
        manifest_relative_path="manifests/slack/production/x.json",
        source="slack",
        started_at=None,
        run_id="20260902T140337Z-0aa0aa98d8ce",
    )
    assert slack["declares_source"] is True


def _day_run(
    paths: status.CollectionPaths,
    *,
    run_id: str,
    at: str,
    manifest_status: str = "success",
    since: str = "2026-08-29T15:00:00+00:00",
    until: str = "2026-08-30T15:00:00+00:00",
    truncated: bool = False,
) -> None:
    """One run over the KST date 2026-08-30, or over part of it."""
    write_manifest(
        paths,
        source="notion",
        run_id=run_id,
        status=manifest_status,
        started_at=at,
        finished_at=at,
        requested_window={"since_effective": since, "until": until, "mode": "date_slice"},
        truncated=truncated,
        truncation=[{"kind": "budget", "count": 1}] if truncated else [],
    )


def _cell_for_0830(paths: status.CollectionPaths) -> dict[str, Any]:
    grid = status.coverage(
        paths,
        start=status.parse_iso_date("2026-08-30"),
        end=status.parse_iso_date("2026-08-30"),
        sources=["notion"],
        now=NOW,
    )
    return grid["rows"][0]["cells"]["notion"]


def test_a_whole_day_reread_clears_an_earlier_failure(
    paths: status.CollectionPaths,
) -> None:
    """Repairing a gap has to be visible on the screen that reported it.

    Every run touching a date used to vote forever, so a capture that failed
    at 01:00 held the date at `partial` even after a clean run at 05:00 read
    the whole day. The dashboard could show a gap and could never show it
    closed, which made it useless for the only thing it was used for.
    """
    _day_run(paths, run_id="20260830T010000Z-f00001", at="2026-08-30T01:00:00+00:00",
             manifest_status="failed")
    _day_run(paths, run_id="20260830T050000Z-f00002", at="2026-08-30T05:00:00+00:00")

    cell = _cell_for_0830(paths)
    assert cell["coverage"] == "collected"
    assert cell["completeness"] == "complete"
    # The history is not erased: the date still had two runs, and the cell
    # says how many of them the re-read answered.
    assert cell["runs"] == 2
    assert cell["runs_superseded"] == 1
    assert any("판정에서 제외" in note for note in cell["notes"])


def test_a_partial_reread_clears_nothing(paths: status.CollectionPaths) -> None:
    """A run that re-read two hours cannot speak for the other twenty-two."""
    _day_run(paths, run_id="20260830T010000Z-f00003", at="2026-08-30T01:00:00+00:00",
             manifest_status="failed")
    _day_run(
        paths,
        run_id="20260830T050000Z-f00004",
        at="2026-08-30T05:00:00+00:00",
        since="2026-08-30T13:00:00+00:00",
    )

    cell = _cell_for_0830(paths)
    assert cell["coverage"] == "partial"
    assert cell["runs_superseded"] == 0


def test_a_truncated_reread_clears_nothing(paths: status.CollectionPaths) -> None:
    """A truncated run is precisely one that knows it stopped early.

    Slack fails loudly and Notion runs out of budget quietly. The two must not
    end the same way: a successful-but-truncated re-read may not turn a date
    green, however recent it is.
    """
    _day_run(paths, run_id="20260830T010000Z-f00005", at="2026-08-30T01:00:00+00:00",
             manifest_status="failed")
    _day_run(paths, run_id="20260830T050000Z-f00006", at="2026-08-30T05:00:00+00:00",
             truncated=True)

    cell = _cell_for_0830(paths)
    assert cell["coverage"] == "partial"
    assert cell["completeness"] == "incomplete"
    assert cell["runs_superseded"] == 0


def test_a_failure_after_the_reread_is_not_cleared_by_it(
    paths: status.CollectionPaths,
) -> None:
    """Supersession runs forwards only."""
    _day_run(paths, run_id="20260830T010000Z-f00007", at="2026-08-30T01:00:00+00:00")
    _day_run(paths, run_id="20260830T050000Z-f00008", at="2026-08-30T05:00:00+00:00",
             manifest_status="failed")

    cell = _cell_for_0830(paths)
    assert cell["coverage"] == "partial"
    assert cell["runs_superseded"] == 0


def test_two_runs_at_the_same_instant_order_by_run_id(
    paths: status.CollectionPaths,
) -> None:
    """A verdict that changes with directory listing order is not a verdict."""
    _day_run(paths, run_id="20260830T050000Z-aaaaaa", at="2026-08-30T05:00:00+00:00",
             manifest_status="failed")
    _day_run(paths, run_id="20260830T050000Z-bbbbbb", at="2026-08-30T05:00:00+00:00")

    cell = _cell_for_0830(paths)
    assert cell["coverage"] == "collected"
    assert cell["runs_superseded"] == 1
    assert cell["last_run_id"] == "20260830T050000Z-bbbbbb"


def test_a_dry_run_does_not_collect_a_date(paths: status.CollectionPaths) -> None:
    """A dry run reads the source and keeps nothing.

    No checkpoint moves and no ledger row lands, so the date is exactly as
    uncollected afterwards as before. It used to paint the cell `collected`,
    which is the one direction this module must never err in.
    """
    write_manifest(
        paths,
        source="notion",
        run_id="20260830T050000Z-d00001",
        started_at="2026-08-30T05:00:00+00:00",
        finished_at="2026-08-30T05:00:00+00:00",
        requested_window={
            "since_effective": "2026-08-29T15:00:00+00:00",
            "until": "2026-08-30T15:00:00+00:00",
            "mode": "date_slice",
        },
        dry_run=True,
    )

    cell = _cell_for_0830(paths)
    assert cell["coverage"] == "not_collected"
    assert cell["completeness"] == "incomplete"
    assert cell["runs"] == 1
    assert any("dry-run" in note for note in cell["notes"])


def test_a_dry_run_neither_clears_nor_condemns_a_real_run(
    paths: status.CollectionPaths,
) -> None:
    """Set aside, not folded in: it is evidence of neither collection nor failure."""
    _day_run(paths, run_id="20260830T010000Z-d00002", at="2026-08-30T01:00:00+00:00")
    write_manifest(
        paths,
        source="notion",
        run_id="20260830T050000Z-d00003",
        status="failed",
        started_at="2026-08-30T05:00:00+00:00",
        finished_at="2026-08-30T05:00:00+00:00",
        requested_window={
            "since_effective": "2026-08-29T15:00:00+00:00",
            "until": "2026-08-30T15:00:00+00:00",
            "mode": "date_slice",
        },
        dry_run=True,
    )

    cell = _cell_for_0830(paths)
    # The dry run failed, but it was never going to keep anything, so it does
    # not turn a genuinely collected date partial.
    assert cell["coverage"] == "collected"
    assert cell["runs"] == 2


def test_a_dry_run_cannot_supersede_a_failure(paths: status.CollectionPaths) -> None:
    """Superseding means 'this was read again'. A dry run did not keep it."""
    _day_run(paths, run_id="20260830T010000Z-d00004", at="2026-08-30T01:00:00+00:00",
             manifest_status="failed")
    write_manifest(
        paths,
        source="notion",
        run_id="20260830T050000Z-d00005",
        started_at="2026-08-30T05:00:00+00:00",
        finished_at="2026-08-30T05:00:00+00:00",
        requested_window={
            "since_effective": "2026-08-29T15:00:00+00:00",
            "until": "2026-08-30T15:00:00+00:00",
            "mode": "date_slice",
        },
        dry_run=True,
    )

    cell = _cell_for_0830(paths)
    assert cell["coverage"] == "failed"
    assert cell["runs_superseded"] == 0
