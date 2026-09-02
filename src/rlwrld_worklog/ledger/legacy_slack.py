"""Legacy Slack daily_raw -> standard v1 ledger records.

Raw message stores only. Attribution buckets (`sent`, `mentioned`,
`replied_to_me`, `reacted_to_me`) are never converted: they duplicate the same
message once per person and mix observation with inference (principle 3).

Identity is (workspace_id, channel_id, ts) per principle 6.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from .common import (
    LegacyPath,
    MetaSignalResolver,
    SlackWorkspaceResolver,
    classify_path,
    content_hash,
    file_sha256,
    iso_or_none,
    observation_window,
    read_json,
    slack_ts_to_iso,
    visibility_routing,
)
from .schema import CAPTURE_PROFILES, CONVERTER_VERSION, LEDGER_SCHEMA_VERSION, LedgerRecord, ledger_id_for

# Message-bearing containers. `personal` and `attribution` are attribution
# buckets and are excluded by name, not by shape, so a future rename cannot
# silently pull them in.
RAW_CONTAINERS = {"common", "dm", "private"}
EXCLUDED_CONTAINERS = {"attribution", "personal"}
EXCLUDED_FILENAMES = {"meta.json", "all_users.json", "reactions_supplement.json"}

# Slack subtypes that mark a deletion tombstone rather than a message.
TOMBSTONE_SUBTYPES = {"tombstone", "message_deleted"}


@dataclass
class SlackConvertStats:
    files_seen: int = 0
    files_skipped_partial: int = 0
    files_skipped_container: int = 0
    files_unreadable: int = 0
    records_seen: int = 0
    records_converted: int = 0
    records_dropped_no_ts: int = 0
    records_dropped_no_channel: int = 0
    records_deduplicated: int = 0
    workspace_unknown: int = 0
    updated_at_unknown: int = 0
    deleted_state_unknown: int = 0
    supplemented: int = 0
    completeness_recorded: int = 0
    completeness_not_recorded: int = 0
    completeness_unknown: int = 0
    routing_anomalies: int = 0
    unreadable_files: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        value = {
            key: getattr(self, key)
            for key in self.__dataclass_fields__
            if key != "unreadable_files"
        }
        value["unreadable_files"] = self.unreadable_files[:50]
        return value


def _scope(info: LegacyPath, payload: dict[str, Any], channel_id: str) -> dict[str, Any]:
    return {
        "kind": "channel",
        "channel_id": channel_id,
        # is_private is only ever set at file level; slim_message never carried
        # it, which is what let private content route into shared/.
        "is_private": payload.get("is_private") if isinstance(payload.get("is_private"), bool) else None,
        "container": info.container,
    }


def _routing_anomaly(info: LegacyPath) -> str | None:
    """A private-shaped container sitting under the company-wide root."""
    if info.root == "shared" and info.container in {"dm", "private"}:
        return "private_container_under_shared_root"
    return None


def _supplement(message: dict[str, Any], file_payload: dict[str, Any]) -> dict[str, Any]:
    is_supplement = message.get("_supplemented")
    runs = file_payload.get("_supplemented_runs")
    # Search-supplemented records carry username/edited but no permalink;
    # primary records are the reverse. One file holds both shapes.
    variant = "search_supplement" if is_supplement else "conversations_history"
    return {
        "is_supplement": bool(is_supplement) if is_supplement is not None else None,
        "supplement_kind": "search.messages" if is_supplement else None,
        "schema_variant": variant,
        # Reaction supplements are written to a separate file and never merged
        # back into the message, so a consumer must join them.
        "merged_into_primary": False if is_supplement else None,
        "file_supplement_runs": len(runs) if isinstance(runs, list) else None,
    }


def _deleted_state(message: dict[str, Any]) -> dict[str, Any]:
    subtype = message.get("subtype")
    if isinstance(subtype, str) and subtype in TOMBSTONE_SUBTYPES:
        return {"is_deleted": True, "kind": "tombstone_subtype", "status": "observed"}
    if message.get("deleted") is True:
        return {"is_deleted": True, "kind": "deleted_flag", "status": "observed"}
    # Legacy never tracked deletions, so "no tombstone" is not evidence the
    # message still exists.
    return {"is_deleted": None, "kind": None, "status": "unknown"}


def _relations(message: dict[str, Any], channel_id: str, workspace_id: str) -> dict[str, Any]:
    thread_ts = message.get("thread_ts")
    files = message.get("files")
    attachments = []
    if isinstance(files, list):
        for item in files:
            if isinstance(item, dict):
                # Binaries are excluded; URL/name/type metadata is kept.
                # Legacy never captured `size` or `mimetype`.
                attachments.append(
                    {
                        "name": item.get("name"),
                        "filetype": item.get("filetype"),
                        "url": item.get("url"),
                        "size": item.get("size"),
                        "mimetype": item.get("mimetype"),
                    }
                )
    return {
        "workspace_id": workspace_id,
        "channel_id": channel_id,
        "thread_id": thread_ts if isinstance(thread_ts, str) else None,
        "is_thread_reply": bool(thread_ts) and thread_ts != message.get("ts"),
        "parent_ts": thread_ts if isinstance(thread_ts, str) and thread_ts != message.get("ts") else None,
        "reply_count": message.get("reply_count"),
        "author_user_id": message.get("user"),
        "reactions": message.get("reactions") if isinstance(message.get("reactions"), list) else [],
        "attachments": attachments,
        # Mentions are not extracted here. The legacy pipeline derived them
        # from a single regex over `text`; re-deriving belongs to the service
        # layer, where the method and its limits can be recorded.
        "mentions_extracted": False,
    }


def _labels(message: dict[str, Any], file_payload: dict[str, Any]) -> dict[str, Any]:
    """Names as observed at capture time. The live API only returns current
    names, so these are the only record of a rename."""
    return {
        "channel_name": message.get("channel_name") or file_payload.get("channel_name"),
        "username": message.get("username"),
        "observed_at": iso_or_none(file_payload.get("collected_at")),
    }


def iter_slack_records(
    legacy_root: Path,
    *,
    stats: SlackConvertStats,
    meta_resolver: MetaSignalResolver,
    workspace_resolver: SlackWorkspaceResolver,
    roots: tuple[str, ...] = ("shared", "personal"),
) -> Iterator[LedgerRecord]:
    seen_ledger_ids: set[str] = set()
    for root in roots:
        base = legacy_root / root
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.json")):
            if not path.is_file():
                continue
            info = classify_path(path, legacy_root)
            if info.source != "slack":
                continue
            if info.is_partial:
                stats.files_skipped_partial += 1
                continue
            if path.name in EXCLUDED_FILENAMES:
                continue
            if info.container in EXCLUDED_CONTAINERS:
                stats.files_skipped_container += 1
                continue
            if info.layout == "thread_store":
                container_ok = True
            else:
                container_ok = info.container in RAW_CONTAINERS
            if not container_ok:
                continue
            stats.files_seen += 1
            yield from _convert_file(
                path,
                info,
                stats=stats,
                meta_resolver=meta_resolver,
                workspace_resolver=workspace_resolver,
                seen_ledger_ids=seen_ledger_ids,
                legacy_root=legacy_root,
            )


def _convert_file(
    path: Path,
    info: LegacyPath,
    *,
    stats: SlackConvertStats,
    meta_resolver: MetaSignalResolver,
    workspace_resolver: SlackWorkspaceResolver,
    seen_ledger_ids: set[str],
    legacy_root: Path,
) -> Iterator[LedgerRecord]:
    payload, error = read_json(path)
    if error or not isinstance(payload, dict):
        stats.files_unreadable += 1
        stats.unreadable_files.append(f"{path.relative_to(legacy_root)}:{error or 'not_an_object'}")
        return
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return

    is_thread_store = info.layout == "thread_store"
    profile = CAPTURE_PROFILES["slack_thread_store" if is_thread_store else "slack_message"]
    # thread_store has no capture date. main_ts_kst_date is the date of the
    # thread's parent message, not the observation window, so using it here
    # would fabricate coverage months before the collector existed.
    day = None if is_thread_store else info.date
    window = observation_window(payload.get("date_range"), day if isinstance(day, str) else None)
    workspace_id, workspace_status = workspace_resolver.get(window["start"])
    if workspace_status == "unknown":
        stats.workspace_unknown += 1
    completeness = dict(meta_resolver.get(info.root, info.date, "slack"))
    completeness["lossy_fields"] = {}
    if completeness["status"] == "recorded":
        stats.completeness_recorded += 1
    elif completeness["status"] == "not_recorded":
        stats.completeness_not_recorded += 1
    else:
        stats.completeness_unknown += 1

    routing = visibility_routing(payload.get("_meta"), root=info.root, container=info.container or "")
    routing["patched_at"] = iso_or_none(payload.get("_meta_patched_at"))
    anomaly = _routing_anomaly(info)
    routing["routing_anomaly"] = anomaly
    if anomaly:
        stats.routing_anomalies += 1

    file_sha = file_sha256(str(path))
    relative = str(path.relative_to(legacy_root))
    declared = payload.get("message_count")
    default_channel = payload.get("channel_id")
    collected_at = iso_or_none(payload.get("collected_at")) or iso_or_none(
        (payload.get("_meta") or {}).get("collected_at") if isinstance(payload.get("_meta"), dict) else None
    )

    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        stats.records_seen += 1
        timestamp = message.get("ts")
        if not isinstance(timestamp, str) or not timestamp:
            stats.records_dropped_no_ts += 1
            continue
        channel_id = message.get("channel_id") or default_channel
        if not isinstance(channel_id, str) or not channel_id:
            stats.records_dropped_no_channel += 1
            continue

        raw_payload = dict(message)
        record_hash = content_hash(raw_payload)
        source_entity_id = f"{workspace_id}:{channel_id}:{timestamp}"
        ledger_id = ledger_id_for(
            source="slack",
            entity_type="message",
            tenant_id=workspace_id,
            scope_key=channel_id,
            source_entity_id=source_entity_id,
            window_start=window["start"],
            content_hash=record_hash,
        )
        if ledger_id in seen_ledger_ids:
            stats.records_deduplicated += 1
            continue
        seen_ledger_ids.add(ledger_id)

        edited = message.get("edited")
        updated_at = None
        if isinstance(edited, dict):
            updated_at = slack_ts_to_iso(edited.get("ts"))
        # `edited` is only ever present on search-supplemented records, and is
        # null on most of those. Absence means "unknown", not "never edited".
        updated_status = "observed" if updated_at else "unknown"
        if updated_status == "unknown":
            stats.updated_at_unknown += 1

        deleted_state = _deleted_state(message)
        if deleted_state["status"] == "unknown":
            stats.deleted_state_unknown += 1

        supplement = _supplement(message, payload)
        if supplement["is_supplement"]:
            stats.supplemented += 1

        stats.records_converted += 1
        yield LedgerRecord(
            ledger_id=ledger_id,
            schema_version=LEDGER_SCHEMA_VERSION,
            capture_profile=profile,
            source="slack",
            tenant={"workspace_id": workspace_id, "status": workspace_status},
            scope=_scope(info, payload, channel_id),
            entity_type="message",
            source_entity_id=source_entity_id,
            source_entity_key={
                "workspace_id": workspace_id,
                "channel_id": channel_id,
                "ts": timestamp,
            },
            source_revision_id=edited.get("ts") if isinstance(edited, dict) else None,
            source_created_at=slack_ts_to_iso(timestamp),
            source_updated_at=updated_at,
            source_updated_at_status=updated_status,
            collected_at=collected_at,
            deleted_state=deleted_state,
            raw_payload=raw_payload,
            content_hash=record_hash,
            relations=_relations(message, channel_id, workspace_id),
            provenance={
                "source_file": relative,
                "source_file_sha256": file_sha,
                "source_file_kind": info.container or info.layout,
                "record_pointer": f"/messages/{index}",
                "legacy_layout_version": info.layout,
                "converter_version": CONVERTER_VERSION,
                "api_endpoint": "search.messages" if supplement["is_supplement"] else "conversations.history",
                "cursor": None,
                "collector_run_id": None,
            },
            coverage={
                "observation_role": "historical_observation",
                "record_count_in_file": len(messages),
                "declared_count_in_file": declared if isinstance(declared, int) else None,
                "count_matches_declared": (len(messages) == declared) if isinstance(declared, int) else None,
                "permission_gap": None,
                "errors": [],
            },
            observation_window=window,
            capture_completeness=completeness,
            supplement_provenance=supplement,
            visibility_routing=routing,
            denormalized_label_snapshot=_labels(message, payload),
        )
