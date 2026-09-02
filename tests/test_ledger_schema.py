from __future__ import annotations

from rlwrld_worklog.ledger.schema import (
    LEDGER_SCHEMA_VERSION,
    ledger_id_for,
    ledger_json_schema,
    validate_record,
)


def _minimal_record() -> dict:
    return {
        "ledger_id": "00000000-0000-5000-8000-000000000000",
        "schema_version": LEDGER_SCHEMA_VERSION,
        "capture_profile": "legacy-slack-slim12/v1",
        "source": "slack",
        "tenant": {"workspace_id": "T1", "status": "resolved_from_all_users"},
        "scope": {"kind": "channel", "channel_id": "C1", "is_private": False, "container": "common"},
        "entity_type": "message",
        "source_entity_id": "T1:C1:1.2",
        "source_entity_key": {"workspace_id": "T1", "channel_id": "C1", "ts": "1.2"},
        "source_revision_id": None,
        "source_created_at": "2026-05-01T00:00:00+00:00",
        "source_updated_at": None,
        "source_updated_at_status": "unknown",
        "collected_at": None,
        "deleted_state": {"is_deleted": None, "kind": None, "status": "unknown"},
        "raw_payload": {"ts": "1.2"},
        "content_hash": "sha256:" + "0" * 64,
        "relations": {},
        "provenance": {
            "source_file": "shared/daily_raw/2026-05-01/slack/common/x.json",
            "source_file_sha256": "sha256:" + "1" * 64,
            "record_pointer": "/messages/0",
            "legacy_layout_version": "current",
            "converter_version": "legacy-converter/1.0.0",
        },
        "coverage": {"observation_role": "historical_observation"},
        "observation_window": {
            "start": "2026-05-01",
            "end": "2026-05-01",
            "tz": "+09:00",
            "granularity": "day",
        },
        "capture_completeness": {"status": "unknown", "legacy_status_is_trustworthy": False},
        "supplement_provenance": {"schema_variant": "conversations_history"},
        "visibility_routing": {"storage_root": "shared", "meta_present": True},
        "denormalized_label_snapshot": {},
    }


def test_schema_shape():
    schema = ledger_json_schema()
    assert schema["properties"]["schema_version"]["const"] == LEDGER_SCHEMA_VERSION
    # The three service-derived fields must not be ledger fields.
    for forbidden in ("derived_attribution", "roster_identity_link", "computed_metrics"):
        assert forbidden not in schema["properties"]
    # extracted_text is a separate artifact, not a ledger column.
    assert "extracted_text" not in schema["properties"]
    # legacy_layout_version is parser provenance.
    assert "legacy_layout_version" in schema["properties"]["provenance"]["properties"]


def test_minimal_record_validates():
    assert validate_record(_minimal_record()) == []


def test_unknown_field_is_rejected():
    record = _minimal_record()
    record["derived_attribution"] = {"bucket": "sent"}
    errors = validate_record(record)
    assert errors, "service-derived fields must not be accepted into the ledger"


def test_bad_content_hash_is_rejected():
    record = _minimal_record()
    record["content_hash"] = "md5:abc"
    assert validate_record(record)


def test_provenance_is_required():
    record = _minimal_record()
    del record["provenance"]["source_file_sha256"]
    assert validate_record(record)


def test_ledger_id_is_deterministic_and_window_scoped():
    base = dict(
        source="slack",
        entity_type="message",
        tenant_id="T1",
        scope_key="C1",
        source_entity_id="T1:C1:1.2",
        content_hash="sha256:" + "0" * 64,
    )
    first = ledger_id_for(window_start="2026-05-01", **base)
    assert first == ledger_id_for(window_start="2026-05-01", **base)
    # Same object observed on another day is a separate observation.
    assert first != ledger_id_for(window_start="2026-05-02", **base)
