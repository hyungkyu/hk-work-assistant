from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from conftest_ledger import CHANNEL, USER_A, WORKSPACE, build_slack_tree, slack_message, write_json  # noqa: E402

from rlwrld_worklog.ledger.common import MetaSignalResolver, SlackWorkspaceResolver  # noqa: E402
from rlwrld_worklog.ledger.legacy_slack import SlackConvertStats, iter_slack_records  # noqa: E402
from rlwrld_worklog.ledger.schema import validate_record  # noqa: E402


def convert(tmp_path: Path):
    root = build_slack_tree(tmp_path / "legacy")
    stats = SlackConvertStats()
    resolver = SlackWorkspaceResolver(root)
    resolver.prime()
    records = list(
        iter_slack_records(
            root,
            stats=stats,
            meta_resolver=MetaSignalResolver(root),
            workspace_resolver=resolver,
        )
    )
    return root, stats, records


def test_converts_only_raw_message_stores(tmp_path):
    _, stats, records = convert(tmp_path)
    # 3 shared common + 1 shared dm + 1 personal private
    assert len(records) == 5
    assert stats.records_converted == 5
    # attribution bucket was skipped by name, not by shape
    assert stats.files_skipped_container == 1
    assert all(record.entity_type == "message" for record in records)


def test_rsync_partial_is_excluded_even_though_it_parses(tmp_path):
    _, stats, records = convert(tmp_path)
    assert stats.files_skipped_partial == 1
    assert not any(".rsync-partial" in record.provenance["source_file"] for record in records)


def test_identity_is_workspace_channel_ts(tmp_path):
    _, _, records = convert(tmp_path)
    record = next(r for r in records if r.source_entity_key["ts"] == "1777000000.000100")
    assert record.source_entity_id == f"{WORKSPACE}:{CHANNEL}:1777000000.000100"
    assert record.source_entity_key == {
        "workspace_id": WORKSPACE,
        "channel_id": CHANNEL,
        "ts": "1777000000.000100",
    }
    assert record.tenant["workspace_id"] == WORKSPACE
    assert record.tenant["status"] == "resolved_from_all_users"


def test_every_record_validates_and_is_traceable(tmp_path):
    root, _, records = convert(tmp_path)
    for record in records:
        assert validate_record(record.to_dict()) == []
        provenance = record.provenance
        assert (root / provenance["source_file"]).is_file()
        assert provenance["source_file_sha256"].startswith("sha256:")
        assert provenance["record_pointer"].startswith("/messages/")


def test_unedited_message_reports_unknown_not_false(tmp_path):
    _, stats, records = convert(tmp_path)
    plain = next(r for r in records if r.source_entity_key["ts"] == "1777000000.000100")
    assert plain.source_updated_at is None
    assert plain.source_updated_at_status == "unknown"
    edited = next(r for r in records if r.source_entity_key["ts"] == "1777000200.000300")
    assert edited.source_updated_at_status == "observed"
    assert edited.source_revision_id == "1777000250.000000"


def test_deleted_state_is_unknown_when_legacy_never_tracked_it(tmp_path):
    _, _, records = convert(tmp_path)
    assert all(record.deleted_state["status"] == "unknown" for record in records)
    assert all(record.deleted_state["is_deleted"] is None for record in records)


def test_supplement_provenance_separates_the_two_schemas(tmp_path):
    _, stats, records = convert(tmp_path)
    supplemented = next(r for r in records if r.supplement_provenance["is_supplement"])
    assert supplemented.supplement_provenance["schema_variant"] == "search_supplement"
    assert supplemented.provenance["api_endpoint"] == "search.messages"
    assert "permalink" not in supplemented.raw_payload
    primary = next(r for r in records if r.source_entity_key["ts"] == "1777000000.000100")
    assert primary.supplement_provenance["schema_variant"] == "conversations_history"
    assert primary.supplement_provenance["file_supplement_runs"] == 1
    assert stats.supplemented == 1


def test_capture_completeness_uses_signals_not_status(tmp_path):
    _, _, records = convert(tmp_path)
    record = next(r for r in records if r.scope["channel_id"] == CHANNEL)
    completeness = record.capture_completeness
    assert completeness["status"] == "recorded"
    assert completeness["truncated"] is True
    assert completeness["truncation_events"] == 1
    assert completeness["rate_limit_hits"] == 7
    # meta.json says "ok" on the same day it lost data.
    assert completeness["legacy_status_field"] == "ok"
    assert completeness["legacy_status_is_trustworthy"] is False


def test_private_container_under_shared_root_is_flagged(tmp_path):
    _, stats, records = convert(tmp_path)
    leaked = next(r for r in records if r.scope["channel_id"] == "D0TESTDM01")
    assert leaked.visibility_routing["routing_anomaly"] == "private_container_under_shared_root"
    assert leaked.visibility_routing["storage_root"] == "shared"
    assert leaked.visibility_routing["patched_at"] is not None
    assert stats.routing_anomalies == 1


def test_labels_capture_the_name_observed_at_the_time(tmp_path):
    _, _, records = convert(tmp_path)
    record = next(r for r in records if r.source_entity_key["ts"] == "1777000000.000100")
    assert record.denormalized_label_snapshot["channel_name"] == "test-channel"


def test_observation_window_is_the_day_slice(tmp_path):
    _, _, records = convert(tmp_path)
    for record in records:
        assert record.observation_window["start"] == "2026-05-01"
        assert record.observation_window["granularity"] == "day"
        assert record.observation_window["tz"] == "+09:00"


def test_thread_relation_is_preserved(tmp_path):
    _, _, records = convert(tmp_path)
    reply = next(r for r in records if r.source_entity_key["ts"] == "1777000100.000200")
    assert reply.relations["thread_id"] == "1777000000.000100"
    assert reply.relations["is_thread_reply"] is True
    assert reply.relations["parent_ts"] == "1777000000.000100"
    # Mentions are deliberately not derived at ledger time.
    assert reply.relations["mentions_extracted"] is False


def test_identical_content_in_one_window_collapses(tmp_path):
    root, stats, records = convert(tmp_path)
    ids = [record.ledger_id for record in records]
    assert len(ids) == len(set(ids))


def test_thread_store_parent_date_is_not_claimed_as_observation_date(tmp_path):
    root = build_slack_tree(tmp_path / "legacy")
    write_json(
        root / "personal" / "thread_store" / CHANNEL / "1756093293.054529.json",
        {
            "main_ts_kst_date": "2025-08-25",
            "channel_id": CHANNEL,
            "messages": [slack_message("1756093293.054529")],
            "message_count": 1,
        },
    )
    stats = SlackConvertStats()
    resolver = SlackWorkspaceResolver(root)
    resolver.prime()
    records = list(
        iter_slack_records(
            root,
            stats=stats,
            meta_resolver=MetaSignalResolver(root),
            workspace_resolver=resolver,
        )
    )
    thread = next(record for record in records if record.provenance["legacy_layout_version"] == "thread_store")
    assert thread.observation_window == {
        "start": None,
        "end": None,
        "tz": "+09:00",
        "granularity": "unknown",
    }
