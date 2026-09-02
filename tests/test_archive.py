# hook-allow: synthetic-credentials
"""Raw archive guarantees: append-only, detailed manifests, safe checkpoints.

Every identifier here is invented. No collected data is committed to this
repository (docs/data-policy.md).
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from rlwrld_worklog.archive import RawArchive


def test_page_is_written_verbatim_and_hashed(tmp_path: Path) -> None:
    archive = RawArchive(tmp_path, "slack", "run-1", "test")
    body = {"ok": True, "messages": [{"ts": "1.000100", "text": "synthetic"}]}

    path = archive.write_page("history-C0TEST", body, endpoint="conversations.history", item_count=1)

    assert json.loads(gzip.decompress(path.read_bytes())) == body
    entry = archive.files[0]
    assert entry["endpoint"] == "conversations.history"
    assert entry["item_count"] == 1
    assert entry["sha256"] and len(entry["sha256"]) == 64
    assert archive.api_coverage["conversations.history"] == {"pages": 1, "items": 1}


def test_request_parameters_are_recorded_with_credentials_redacted(tmp_path: Path) -> None:
    archive = RawArchive(tmp_path, "slack", "run-1", "test")
    archive.write_page(
        "history-C0TEST",
        {"ok": True},
        endpoint="conversations.history",
        request={"channel": "C0TEST", "oldest": "1.0", "token": "xoxp-must-not-be-archived"},
    )
    request = archive.files[0]["request"]
    assert request["channel"] == "C0TEST"
    assert request["token"] == "<redacted>"
    assert "xoxp-must-not-be-archived" not in json.dumps(request)


def test_archive_refuses_to_overwrite_a_prior_observation(tmp_path: Path) -> None:
    archive = RawArchive(tmp_path, "slack", "run-1", "test")
    first = archive.write_page("history-C0TEST", {"ok": True})
    # Force the collision the sequence number normally prevents.
    archive._sequence = 0
    with pytest.raises(FileExistsError):
        archive.write_page("history-C0TEST", {"ok": True})
    assert first.exists()


def test_manifest_carries_profile_window_coverage_and_signals(tmp_path: Path) -> None:
    archive = RawArchive(
        tmp_path, "slack", "run-1", "test", capture_profile="live-slack-web-api/v1", capture_density="full"
    )
    archive.set_requested_window({"since": "2026-08-30T00:00:00+00:00", "mode": "incremental"})
    archive.set_checkpoint_in({"high_watermarks": {"C0TEST": "1.0"}})
    archive.write_page("users", {"members": []}, endpoint="users.list", item_count=0)
    archive.note_skip("conversation_inaccessible", channel_id="C0GONE", error="channel_not_found")
    archive.note_error("normalize_failed", object_id="X")
    archive.note_truncation("max_messages", limit=10)
    archive.note_rate_limit(3)
    archive.note_coverage("slack.message_deletion_not_exposed")

    manifest = json.loads(archive.finish({"status": "success_with_skips"}).read_text())

    assert manifest["schema_version"] == 2
    assert manifest["capture_profile"] == "live-slack-web-api/v1"
    assert manifest["capture_density"] == "full"
    assert manifest["started_at"] and manifest["finished_at"]
    assert manifest["requested_window"]["mode"] == "incremental"
    assert manifest["checkpoint_in"]["high_watermarks"] == {"C0TEST": "1.0"}
    assert manifest["checkpoint_advanced"] is False
    assert manifest["api_coverage"]["users.list"]["pages"] == 1
    assert manifest["rate_limit_hits"] == 3
    assert manifest["truncated"] is True
    assert manifest["skips"][0]["channel_id"] == "C0GONE"
    assert manifest["errors"][0]["kind"] == "normalize_failed"
    assert manifest["coverage_notes"] == ["slack.message_deletion_not_exposed"]
    assert manifest["files"][0]["sha256"]
    assert manifest["status"] == "success_with_skips"


def test_second_manifest_for_a_run_does_not_replace_the_first(tmp_path: Path) -> None:
    archive = RawArchive(tmp_path, "slack", "run-1", "test")
    first = archive.finish({"status": "success"})
    second = archive.finish({"status": "failed"})
    assert first != second
    assert json.loads(first.read_text())["status"] == "success"
    assert json.loads(second.read_text())["status"] == "failed"


def test_checkpoint_keeps_the_previous_position_as_history(tmp_path: Path) -> None:
    first = RawArchive(tmp_path, "slack", "run-1", "test")
    first.write_checkpoint({"run_id": "run-1", "high_watermarks": {"C0TEST": "1.0"}})
    second = RawArchive(tmp_path, "slack", "run-2", "test")
    second.write_checkpoint({"run_id": "run-2", "high_watermarks": {"C0TEST": "2.0"}})

    current = json.loads((tmp_path / "manifests/slack/test/checkpoint.json").read_text())
    history = json.loads((tmp_path / "manifests/slack/test/checkpoints/run-1.json").read_text())
    assert current["high_watermarks"]["C0TEST"] == "2.0"
    assert history["high_watermarks"]["C0TEST"] == "1.0"


def test_dry_run_archive_refuses_to_advance_a_checkpoint(tmp_path: Path) -> None:
    archive = RawArchive(tmp_path, "slack", "run-1", "test", dry_run=True)
    with pytest.raises(RuntimeError):
        archive.write_checkpoint({"run_id": "run-1"})
    assert not (tmp_path / "manifests/slack/test/checkpoint.json").exists()
    assert json.loads(archive.finish({"status": "success"}).read_text())["dry_run"] is True


def test_read_checkpoint_tolerates_absent_and_corrupt_files(tmp_path: Path) -> None:
    archive = RawArchive(tmp_path, "slack", "run-1", "test")
    assert archive.read_checkpoint() == {}
    archive.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    archive.checkpoint_path.write_text("{not json", encoding="utf-8")
    assert archive.read_checkpoint() == {}


# ------------------------------------- collection rule stamp and progress


def test_every_manifest_names_the_collection_rule_it_was_captured_under(
    tmp_path: Path,
) -> None:
    from rlwrld_worklog.collection_rules import (
        ACTIVE_RULE_VERSION,
        RULE_REGISTRY_SCHEMA_VERSION,
        active_rule,
    )

    archive = RawArchive(tmp_path, "slack", "run-1", "test")
    manifest = json.loads(archive.finish({"status": "success"}).read_text())

    assert manifest["collection_rule_version"] == ACTIVE_RULE_VERSION
    assert manifest["collection_rule_digest"] == active_rule().digest
    assert manifest["collection_rule_schema_version"] == RULE_REGISTRY_SCHEMA_VERSION
    # Additive only: the manifest schema and every field a reader already
    # depends on are unchanged.
    assert manifest["schema_version"] == 2


@pytest.mark.parametrize(
    "details",
    [
        {"status": "success"},
        {"status": "failed", "error_type": "SlackApiError", "error": "boom"},
        {"status": "success", "dry_run": True},
    ],
)
def test_dry_run_smoke_and_failure_manifests_carry_the_same_stamp(
    tmp_path: Path, details: dict[str, object]
) -> None:
    from rlwrld_worklog.collection_rules import active_rule_stamp

    for density, dry_run in (("full", False), ("dry-run", True), ("smoke", True)):
        archive = RawArchive(
            tmp_path, "notion", f"run-{density}", "test", capture_density=density, dry_run=dry_run
        )
        manifest = json.loads(archive.finish(dict(details)).read_text())
        for key, value in active_rule_stamp().items():
            assert manifest[key] == value


def test_a_collector_detail_cannot_drop_or_rewrite_the_stamp(tmp_path: Path) -> None:
    from rlwrld_worklog.collection_rules import ACTIVE_RULE_VERSION

    archive = RawArchive(tmp_path, "slack", "run-1", "test")
    manifest = json.loads(
        archive.finish(
            {"status": "success", "collection_rule_version": "V0", "collection_rule_digest": "x"}
        ).read_text()
    )
    assert manifest["collection_rule_version"] == ACTIVE_RULE_VERSION
    assert manifest["collection_rule_digest"].startswith("sha256:")


def test_no_progress_snapshot_is_written_without_a_configured_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A capture run from a bare shell must not create files in $HOME."""
    monkeypatch.delenv("APP_CONFIG_ROOT", raising=False)
    monkeypatch.delenv("COLLECTION_PROGRESS_ROOT", raising=False)
    archive = RawArchive(tmp_path, "slack", "run-1", "test")
    assert archive.progress.enabled is False
    assert archive.progress.path is None
    archive.write_page("history", {"ok": True})
    archive.finish({"status": "success"})


def test_a_run_publishes_an_atomic_progress_snapshot_outside_the_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rlwrld_worklog.collection_rules import ACTIVE_RULE_VERSION

    config_root = tmp_path / "config"
    monkeypatch.setenv("APP_CONFIG_ROOT", str(config_root))
    archive_root = tmp_path / "archive"

    archive = RawArchive(archive_root, "notion", "20260902T000000Z-abcdef", "production")
    snapshot_path = (
        config_root
        / "collection-status"
        / "progress"
        / "notion"
        / "production"
        / "20260902T000000Z-abcdef.json"
    )
    assert snapshot_path.is_file()
    opened = json.loads(snapshot_path.read_text())
    assert opened["phase"] == "capture"
    assert opened["status"] == "running"
    assert opened["files_written"] == 0
    assert opened["raw_run_dir"].startswith("raw/notion/production/")
    assert opened["collection_rule_version"] == ACTIVE_RULE_VERSION
    assert opened["pid"] and opened["host"]

    archive.write_page("page", {"secret": "NEVER-IN-A-SNAPSHOT"})
    archive.finish({"status": "success"})
    finished = json.loads(snapshot_path.read_text())
    assert finished["phase"] == "finished"
    assert finished["status"] == "success"
    assert finished["files_written"] == 1
    assert finished["bytes_written"] == archive.bytes_archived > 0
    assert finished["manifest_path"].endswith("20260902T000000Z-abcdef.json")
    assert "NEVER-IN-A-SNAPSHOT" not in snapshot_path.read_text()

    # The snapshot is derived: nothing about it lives inside the raw archive.
    assert not list(archive_root.rglob("*.progress*"))
    assert snapshot_path.stat().st_mode & 0o777 == 0o600


def test_a_failed_progress_write_never_fails_the_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APP_CONFIG_ROOT", str(tmp_path / "config"))
    archive = RawArchive(tmp_path / "archive", "slack", "run-1", "test")
    assert archive.progress.enabled is True

    def explode(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("rlwrld_worklog.collection_progress._atomic_write_json", explode)
    archive.write_page("history", {"ok": True})
    manifest = json.loads(archive.finish({"status": "success"}).read_text())
    assert manifest["status"] == "success"
    assert archive.progress.enabled is False


def test_the_ledger_stage_records_its_counts_on_the_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_root = tmp_path / "config"
    monkeypatch.setenv("APP_CONFIG_ROOT", str(config_root))
    archive = RawArchive(tmp_path / "archive", "slack", "20260902T010000Z-abcdef", "test")
    archive.finish({"status": "success"})
    archive.progress.note_ledger({"records_written": 91, "schema_errors": 2, "output_path": "x"})
    snapshot = json.loads(
        (
            config_root
            / "collection-status"
            / "progress"
            / "slack"
            / "test"
            / "20260902T010000Z-abcdef.json"
        ).read_text()
    )
    assert snapshot["ledger"] == {
        "records_written": 91,
        "schema_errors": 2,
        "output_path": "x",
    }


def test_the_progress_snapshot_stays_out_of_the_archive_and_its_locks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rule 7: nothing about a live run is written into the raw archive.

    The daily runner's lock lives at ``<archive_root>/locks/``. A snapshot
    must not create, take or touch anything there, so the two cannot
    interfere.
    """
    import fcntl

    config_root = tmp_path / "config"
    archive_root = tmp_path / "archive"
    monkeypatch.setenv("APP_CONFIG_ROOT", str(config_root))

    lock_path = archive_root / "locks" / "daily-collect-test.lock"
    lock_path.parent.mkdir(parents=True)
    lock_path.touch()

    archive = RawArchive(archive_root, "slack", "20260902T020000Z-abcdef", "test")
    archive.write_page("history", {"ok": True})
    archive.finish({"status": "success"})

    assert sorted(entry.name for entry in archive_root.iterdir()) == [
        "locks",
        "manifests",
        "raw",
    ]
    assert list(lock_path.parent.iterdir()) == [lock_path]
    assert lock_path.read_bytes() == b""
    with open(lock_path, "a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    assert not any(config_root.rglob("*.lock"))
