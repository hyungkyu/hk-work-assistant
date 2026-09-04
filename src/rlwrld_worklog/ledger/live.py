"""Convert one immutable official-API capture run into standard v1 ledger rows.

The raw gzip responses remain authoritative. This module is deliberately a
separate, repeatable projection: changing service needs never requires calling
Slack, Notion or Google again for a run that is already archived, and running
it twice over the same manifest produces byte-identical output.

Two kinds of record come out of a run:

  * activity records (message, page, block, comment, event), which the service
    loader projects onto the timeline;
  * dimension records (user, usergroup, conversation, calendar, data_source),
    which describe the containers and actors those activities point at. They
    exist in the ledger so the service database is rebuildable from ledger
    data alone, and they are not projected onto the timeline.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .common import content_hash, slack_ts_to_iso
from .schema import (
    CAPTURE_PROFILES,
    LEDGER_SCHEMA_VERSION,
    LedgerRecord,
    ledger_id_for,
    validate_record,
)

LIVE_CONVERTER_VERSION = "live-api-converter/1.2.0"

# Per-record profiles, taken from the registry so the collector and the
# converter cannot drift apart on what a record's provenance is called.
MIRROR_COMMIT_PROFILE = CAPTURE_PROFILES["github_commit"]
REST_COMMIT_PROFILE = CAPTURE_PROFILES["github_rest"]
REST_PROFILE = CAPTURE_PROFILES["github_rest"]
SLURM_PROFILE = CAPTURE_PROFILES["slurm_job"]
LIVE_SOURCES = ("slack", "notion", "google_calendar", "github", "slurm")


@dataclass
class LiveConvertResult:
    source: str
    run_id: str
    output_path: str | None = None
    records_written: int = 0
    schema_errors: int = 0
    schema_error_samples: list[str] = field(default_factory=list)
    by_entity_type: dict[str, int] = field(default_factory=dict)
    unhandled_kinds: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "run_id": self.run_id,
            "output_path": self.output_path,
            "records_written": self.records_written,
            "schema_errors": self.schema_errors,
            "schema_error_samples": self.schema_error_samples[:20],
            "by_entity_type": dict(sorted(self.by_entity_type.items())),
            "unhandled_kinds": dict(sorted(self.unhandled_kinds.items())),
        }


def _load_manifest(path: Path, source: str) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("manifest is not an object")
    manifest_source = str(value.get("source", ""))
    if _canonical_source(manifest_source) != source:
        raise ValueError(f"manifest source is not {source}")
    if value.get("status") not in {"success", "success_with_skips"}:
        raise ValueError(
            "only successful capture runs can be converted; "
            f"{path.name} has status {value.get('status')!r}"
        )
    if not isinstance(value.get("run_id"), str) or not value["run_id"]:
        raise ValueError("manifest has no run_id")
    return value


def _canonical_source(value: str) -> str:
    """The archive names Calendar `google-calendar`; the ledger says `google_calendar`."""
    return value.replace("-", "_")


_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


def _read_archived(archive_root: Path, item: dict[str, Any]) -> dict[str, Any]:
    """Read one archived response, verifying it against the manifest hash first.

    The manifest is an index, not the authority: only the bytes on disk are.
    So the digest is recomputed over the compressed file and compared before
    anything is decompressed or parsed. A missing, malformed or mismatched
    hash raises, which aborts the whole conversion before any ledger file is
    written -- a silently corrupted or edited raw page must never become a
    ledger record, and must never replace a ledger file built from good bytes.
    """
    relative = Path(str(item["path"]))
    path = (archive_root / relative).resolve()
    if not path.is_relative_to(archive_root.resolve()):
        raise ValueError(f"archive path escapes root: {relative}")

    expected = item.get("sha256")
    if not isinstance(expected, str) or not _SHA256_HEX.match(expected):
        raise ValueError(f"manifest has no usable sha256 for {relative}")
    raw = path.read_bytes()
    actual = hashlib.sha256(raw).hexdigest()
    if actual != expected:
        raise ValueError(
            f"archived response does not match the manifest hash: {relative} "
            f"(expected {expected[:12]}..., found {actual[:12]}...)"
        )

    value = json.loads(gzip.decompress(raw))
    if not isinstance(value, dict):
        raise ValueError(f"archived API response is not an object: {relative}")
    return value


def _window(manifest: dict[str, Any]) -> dict[str, Any]:
    finished = datetime.fromisoformat(str(manifest["finished_at"]).replace("Z", "+00:00"))
    day = finished.astimezone(timezone.utc).date().isoformat()
    return {"start": day, "end": day, "tz": "UTC", "granularity": "day"}


def _epoch_to_iso(value: Any, *, unit: str = "s") -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value <= 0:
        return None
    seconds = float(value) / (1000.0 if unit == "ms" else 1.0)
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()
    except (ValueError, OSError, OverflowError):
        return None


def _provenance(item: dict[str, Any], *, kind: str, run_id: str, endpoint: str) -> dict[str, Any]:
    return {
        "source_file": str(item["path"]),
        "source_file_sha256": f"sha256:{item['sha256']}",
        "source_file_kind": kind,
        "record_pointer": "",
        "legacy_layout_version": "live_api_v1",
        "converter_version": LIVE_CONVERTER_VERSION,
        "api_endpoint": item.get("endpoint") or endpoint,
        "cursor": None,
        "collector_run_id": run_id,
    }


def _completeness(
    manifest: dict[str, Any],
    *,
    notes: str | None = None,
    lossy_fields: dict[str, Any] | None = None,
) -> dict[str, Any]:
    truncation = manifest.get("truncation")
    return {
        "status": "recorded",
        "truncated": bool(manifest.get("truncated", False)),
        "truncation_events": len(truncation) if isinstance(truncation, list) else (1 if manifest.get("truncated") else 0),
        "rate_limit_hits": int(manifest.get("rate_limit_hits") or 0),
        "channels_scanned": manifest.get("channels_collected"),
        "channels_empty": None,
        "channels_active": None,
        "legacy_status_field": None,
        "legacy_status_is_trustworthy": True,
        "lossy_fields": dict(lossy_fields or {}),
        "notes": notes,
    }


def _coverage(count: int, manifest: dict[str, Any]) -> dict[str, Any]:
    skips = manifest.get("skips")
    permission_gap = None
    if isinstance(skips, list) and skips:
        kinds: dict[str, int] = {}
        for skip in skips:
            if isinstance(skip, dict):
                kinds[str(skip.get("kind", "unknown"))] = kinds.get(str(skip.get("kind", "unknown")), 0) + 1
        permission_gap = {"run_skips": len(skips), "by_kind": dict(sorted(kinds.items()))}
    errors = manifest.get("errors")
    return {
        "observation_role": "current_head",
        "record_count_in_file": count,
        "declared_count_in_file": count,
        "count_matches_declared": True,
        "permission_gap": permission_gap,
        "errors": [str(error.get("kind")) for error in errors if isinstance(error, dict)]
        if isinstance(errors, list)
        else [],
    }


def _visibility(
    *,
    container: str,
    visibility: str,
    collected_by: str | None,
    schema_version: str,
) -> dict[str, Any]:
    return {
        "storage_root": "local_raw_archive",
        "container": container,
        "visibility": visibility,
        "access_list": None,
        "collected_by": collected_by,
        "source_schema_version": schema_version,
        "patched_at": None,
        "meta_present": True,
        "routing_anomaly": None,
    }


def _supplement(*, is_supplement: bool, kind: str | None, schema_variant: str) -> dict[str, Any]:
    return {
        "is_supplement": is_supplement,
        "supplement_kind": kind,
        "schema_variant": schema_variant,
        "merged_into_primary": False if is_supplement else None,
        "file_supplement_runs": None,
    }


# ----------------------------------------------------------------- slack


_SLACK_SEARCH_KINDS = {
    "direct-mentions",
    "direct-messages-to-self",
    "messages-from-self",
    "broadcast-channel",
    "broadcast-here",
    "broadcast-everyone",
}


def _slack_records(
    archive_root: Path, manifest: dict[str, Any], result: LiveConvertResult
) -> Iterable[LedgerRecord]:
    run_id = str(manifest["run_id"])
    team_id = str(manifest["team_id"])
    window = _window(manifest)
    collected_at = str(manifest["finished_at"])
    self_user_id = manifest.get("self_user_id")
    channels: dict[str, dict[str, Any]] = {}
    loaded: list[tuple[dict[str, Any], str, dict[str, Any]]] = []
    for item in manifest.get("files", []):
        kind = str(item.get("kind", ""))
        body = _read_archived(archive_root, item)
        loaded.append((item, kind, body))
        if kind == "conversations":
            for channel in body.get("channels", []):
                if isinstance(channel, dict) and channel.get("id"):
                    channels[str(channel["id"])] = channel

    seen: set[str] = set()

    def emit(record: LedgerRecord) -> LedgerRecord | None:
        if record.ledger_id in seen:
            return None
        seen.add(record.ledger_id)
        return record

    for item, kind, body in loaded:
        supplement = False
        if kind.startswith("history-"):
            endpoint = "conversations.history"
            channel_id = kind.removeprefix("history-")
            messages = body.get("messages", [])
            pointer_base = "/messages"
        elif kind.startswith("replies-"):
            endpoint = "conversations.replies"
            # replies-<channel>-<thread ts>; Slack channel ids never contain '-'.
            channel_id = kind.split("-", 2)[1]
            messages = body.get("messages", [])
            pointer_base = "/messages"
        elif kind in _SLACK_SEARCH_KINDS or kind.startswith("usergroup-"):
            endpoint = "search.messages"
            messages = (body.get("messages") or {}).get("matches", [])
            pointer_base = "/messages/matches"
            channel_id = ""
            supplement = True
        elif kind == "users":
            yield from _slack_dimension_records(
                item,
                body.get("members") or [],
                manifest=manifest,
                window=window,
                entity_type="user",
                container_key="members",
                endpoint="users.list",
                profile="live-slack-users/v1",
                emit=emit,
            )
            continue
        elif kind == "usergroups":
            yield from _slack_dimension_records(
                item,
                body.get("usergroups") or [],
                manifest=manifest,
                window=window,
                entity_type="usergroup",
                container_key="usergroups",
                endpoint="usergroups.list",
                profile="live-slack-usergroups/v1",
                emit=emit,
            )
            continue
        elif kind == "conversations":
            yield from _slack_dimension_records(
                item,
                body.get("channels") or [],
                manifest=manifest,
                window=window,
                entity_type="conversation",
                container_key="channels",
                endpoint="conversations.list",
                profile="live-slack-conversations/v1",
                emit=emit,
            )
            continue
        else:
            if kind:
                result.unhandled_kinds[kind] = result.unhandled_kinds.get(kind, 0) + 1
            continue
        if not isinstance(messages, list):
            continue
        for index, original in enumerate(messages):
            if not isinstance(original, dict):
                continue
            message = dict(original)
            match_channel = message.get("channel")
            current_channel = channel_id
            if isinstance(match_channel, dict):
                current_channel = str(match_channel.get("id") or current_channel)
            elif isinstance(match_channel, str):
                current_channel = match_channel
            timestamp = message.get("ts") or message.get("deleted_ts")
            if not current_channel or not isinstance(timestamp, str) or not timestamp:
                continue
            source_entity_id = f"{team_id}:{current_channel}:{timestamp}"
            record_hash = content_hash(message)
            ledger_id = ledger_id_for(
                source="slack",
                entity_type="message",
                tenant_id=team_id,
                scope_key=current_channel,
                source_entity_id=source_entity_id,
                window_start=window["start"],
                content_hash=record_hash,
            )
            if ledger_id in seen:
                continue
            seen.add(ledger_id)
            edited = message.get("edited") if isinstance(message.get("edited"), dict) else {}
            updated_at = slack_ts_to_iso(edited.get("ts"))
            subtype = message.get("subtype")
            deleted = bool(message.get("deleted")) or subtype in {"message_deleted", "tombstone"}
            channel = channels.get(current_channel, {})
            container = (
                "im" if channel.get("is_im") else "mpim" if channel.get("is_mpim")
                else "private" if channel.get("is_private") else "public"
            )
            files = message.get("files") if isinstance(message.get("files"), list) else []
            provenance = _provenance(item, kind=kind, run_id=run_id, endpoint=endpoint)
            provenance["record_pointer"] = f"{pointer_base}/{index}"
            thread_ts = message.get("thread_ts")
            yield LedgerRecord(
                ledger_id=ledger_id,
                schema_version=LEDGER_SCHEMA_VERSION,
                capture_profile="live-slack-search/v1" if supplement else "live-slack-web-api/v1",
                source="slack",
                tenant={"workspace_id": team_id, "status": "observed"},
                scope={
                    "kind": "slack_conversation",
                    "channel_id": current_channel,
                    "is_private": bool(
                        channel.get("is_private") or channel.get("is_im") or channel.get("is_mpim")
                    ),
                    "container": container,
                },
                entity_type="message",
                source_entity_id=source_entity_id,
                source_entity_key={
                    "workspace_id": team_id,
                    "channel_id": current_channel,
                    "ts": timestamp,
                },
                source_revision_id=edited.get("ts"),
                source_created_at=slack_ts_to_iso(timestamp),
                source_updated_at=updated_at,
                source_updated_at_status="observed" if updated_at else "unknown",
                collected_at=collected_at,
                # The message was present in the API response at capture
                # time, so its state is observed. What the Web API cannot give
                # is a later deletion: see the slack_collector coverage notes.
                deleted_state={
                    "is_deleted": deleted,
                    "kind": "api_tombstone" if deleted else None,
                    "status": "observed",
                },
                raw_payload=message,
                content_hash=record_hash,
                relations={
                    "workspace_id": team_id,
                    "channel_id": current_channel,
                    "thread_id": thread_ts,
                    "is_thread_reply": bool(thread_ts and thread_ts != timestamp),
                    "parent_ts": thread_ts if thread_ts != timestamp else None,
                    "reply_count": message.get("reply_count"),
                    "latest_reply": message.get("latest_reply"),
                    "author_user_id": message.get("user") or message.get("bot_id"),
                    "reactions": message.get("reactions") if isinstance(message.get("reactions"), list) else [],
                    "attachments": files,
                    "mentions_extracted": False,
                },
                provenance=provenance,
                coverage=_coverage(len(messages), manifest),
                observation_window=window,
                capture_completeness=_completeness(
                    manifest,
                    notes="search result supplement" if supplement else "official Web API response",
                    lossy_fields={"files": "metadata_and_links_only"} if files else None,
                ),
                supplement_provenance=_supplement(
                    is_supplement=supplement,
                    kind="search.messages" if supplement else None,
                    schema_variant="official_web_api_v1",
                ),
                visibility_routing=_visibility(
                    container=container,
                    visibility="restricted" if container != "public" else "public",
                    collected_by=self_user_id,
                    schema_version="slack-web-api",
                ),
                denormalized_label_snapshot={
                    "channel_name": channel.get("name"),
                    "username": message.get("username"),
                    "observed_at": collected_at,
                },
            )


def _slack_dimension_records(
    item: dict[str, Any],
    objects: list[Any],
    *,
    manifest: dict[str, Any],
    window: dict[str, Any],
    entity_type: str,
    container_key: str,
    endpoint: str,
    profile: str,
    emit,
) -> Iterable[LedgerRecord]:
    """Slack users, usergroups and conversations: the dimension side of a run."""
    run_id = str(manifest["run_id"])
    team_id = str(manifest["team_id"])
    collected_at = str(manifest["finished_at"])
    for index, original in enumerate(objects):
        if not isinstance(original, dict) or not original.get("id"):
            continue
        obj = dict(original)
        object_id = str(obj["id"])
        record_hash = content_hash(obj)
        ledger_id = ledger_id_for(
            source="slack",
            entity_type=entity_type,
            tenant_id=team_id,
            scope_key="",
            source_entity_id=f"{team_id}:{object_id}",
            window_start=window["start"],
            content_hash=record_hash,
        )
        if entity_type == "user":
            created = _epoch_to_iso(obj.get("created"))
            updated = _epoch_to_iso(obj.get("updated"))
            deleted = bool(obj.get("deleted"))
            deleted_kind = "user_deactivated" if deleted else None
            container = "workspace_directory"
        elif entity_type == "usergroup":
            created = _epoch_to_iso(obj.get("date_create"))
            updated = _epoch_to_iso(obj.get("date_update"))
            deleted = bool(obj.get("date_delete")) or obj.get("deleted_by") is not None
            deleted_kind = "usergroup_disabled" if deleted else None
            container = "workspace_directory"
        else:
            created = _epoch_to_iso(obj.get("created"))
            # conversations.list reports `updated` in epoch milliseconds.
            updated = _epoch_to_iso(obj.get("updated"), unit="ms")
            deleted = bool(obj.get("is_archived"))
            deleted_kind = "channel_archived" if deleted else None
            container = (
                "im" if obj.get("is_im") else "mpim" if obj.get("is_mpim")
                else "private" if obj.get("is_private") else "public"
            )
        provenance = _provenance(item, kind=str(item.get("kind", "")), run_id=run_id, endpoint=endpoint)
        provenance["record_pointer"] = f"/{container_key}/{index}"
        record = LedgerRecord(
            ledger_id=ledger_id,
            schema_version=LEDGER_SCHEMA_VERSION,
            capture_profile=profile,
            source="slack",
            tenant={"workspace_id": team_id, "status": "observed"},
            scope={
                "kind": f"slack_{entity_type}",
                "channel_id": object_id if entity_type == "conversation" else None,
                "is_private": bool(obj.get("is_private") or obj.get("is_im") or obj.get("is_mpim"))
                if entity_type == "conversation"
                else None,
                "container": container,
            },
            entity_type=entity_type,
            source_entity_id=f"{team_id}:{object_id}",
            source_entity_key={"workspace_id": team_id, "id": object_id},
            source_revision_id=str(obj.get("updated") or obj.get("date_update") or "") or None,
            source_created_at=created or updated,
            source_updated_at=updated,
            source_updated_at_status="observed" if updated else "unknown",
            collected_at=collected_at,
            deleted_state={"is_deleted": deleted, "kind": deleted_kind, "status": "observed"},
            raw_payload=obj,
            content_hash=record_hash,
            relations={
                "workspace_id": team_id,
                "is_bot": obj.get("is_bot"),
                "is_admin": obj.get("is_admin"),
                "member_user_ids": obj.get("users") if isinstance(obj.get("users"), list) else None,
                "creator_user_id": obj.get("creator"),
                "num_members": obj.get("num_members") or obj.get("user_count"),
            },
            provenance=provenance,
            coverage=_coverage(len(objects), manifest),
            observation_window=window,
            capture_completeness=_completeness(manifest, notes="official Web API directory response"),
            supplement_provenance=_supplement(
                is_supplement=False, kind=None, schema_variant="official_web_api_v1"
            ),
            visibility_routing=_visibility(
                container=container,
                visibility="restricted" if container not in {"public", "workspace_directory"} else "workspace",
                collected_by=manifest.get("self_user_id"),
                schema_version="slack-web-api",
            ),
            denormalized_label_snapshot={
                "name": obj.get("name") or obj.get("handle"),
                "real_name": (obj.get("profile") or {}).get("real_name")
                if isinstance(obj.get("profile"), dict)
                else None,
                "observed_at": collected_at,
            },
        )
        emitted = emit(record)
        if emitted is not None:
            yield emitted


# ---------------------------------------------------------------- notion


def _notion_records(
    archive_root: Path, manifest: dict[str, Any], result: LiveConvertResult
) -> Iterable[LedgerRecord]:
    run_id = str(manifest["run_id"])
    window = _window(manifest)
    collected_at = str(manifest["finished_at"])
    seen: set[str] = set()
    for item in manifest.get("files", []):
        kind = str(item.get("kind", ""))
        if kind.startswith("page-"):
            endpoint, entity_type, profile = "pages.retrieve", "page", "live-notion-page/v1"
            objects = [_read_archived(archive_root, item)]
            pointer_base = ""
        elif kind.startswith("data_source-") or kind.startswith("database-"):
            endpoint = "data_sources.retrieve" if kind.startswith("data_source-") else "databases.retrieve"
            entity_type, profile = "data_source", "live-notion-data-source/v1"
            objects = [_read_archived(archive_root, item)]
            pointer_base = ""
        elif kind.startswith("blocks-"):
            endpoint, entity_type, profile = "blocks.children.list", "block", "live-notion-block/v1"
            objects = _read_archived(archive_root, item).get("results", [])
            pointer_base = "/results"
        elif kind.startswith("comments-"):
            endpoint, entity_type, profile = "comments.list", "comment", "live-notion-comment/v1"
            objects = _read_archived(archive_root, item).get("results", [])
            pointer_base = "/results"
        elif kind == "users":
            endpoint, entity_type, profile = "users.list", "user", "live-notion-users/v1"
            objects = _read_archived(archive_root, item).get("results", [])
            pointer_base = "/results"
        elif kind in {"search"} or kind.startswith("property-"):
            # Discovery listings and paginated property pages add no object
            # that is not fetched individually; the raw page stays canonical.
            result.unhandled_kinds[kind.split("-")[0]] = (
                result.unhandled_kinds.get(kind.split("-")[0], 0) + 1
            )
            continue
        else:
            if kind:
                result.unhandled_kinds[kind] = result.unhandled_kinds.get(kind, 0) + 1
            continue
        if not isinstance(objects, list):
            continue
        for index, original in enumerate(objects):
            if not isinstance(original, dict) or not original.get("id"):
                continue
            obj = dict(original)
            object_id = str(obj["id"])
            record_hash = content_hash(obj)
            ledger_id = ledger_id_for(
                source="notion",
                entity_type=entity_type,
                tenant_id="unknown",
                scope_key="",
                source_entity_id=object_id,
                window_start=window["start"],
                content_hash=record_hash,
            )
            if ledger_id in seen:
                continue
            seen.add(ledger_id)
            parent = obj.get("parent") if isinstance(obj.get("parent"), dict) else {}
            parent_id = parent.get("page_id") or parent.get("block_id") or parent.get("data_source_id") or parent.get("database_id")
            created = obj.get("created_time")
            updated = obj.get("last_edited_time") or created
            archived = bool(obj.get("archived") or obj.get("in_trash"))
            provenance = _provenance(item, kind=kind, run_id=run_id, endpoint=endpoint)
            provenance["record_pointer"] = f"{pointer_base}/{index}" if pointer_base else ""
            yield LedgerRecord(
                ledger_id=ledger_id,
                schema_version=LEDGER_SCHEMA_VERSION,
                capture_profile=profile,
                source="notion",
                tenant={"workspace_id": "unknown", "status": "unknown"},
                scope={
                    "kind": "notion_object",
                    "parent_page_id": str(parent_id) if parent_id else None,
                    "notion_source_id": str(parent.get("data_source_id") or parent.get("database_id") or "")
                    or None,
                    "notion_source_type": str(parent.get("type") or "") or None,
                },
                entity_type=entity_type,
                source_entity_id=object_id,
                source_entity_key={"object_id": object_id},
                source_revision_id=str(updated) if updated else None,
                source_created_at=str(created) if created else str(updated) if updated else None,
                source_updated_at=str(updated) if updated else None,
                source_updated_at_status="observed" if updated else "unknown",
                collected_at=collected_at,
                deleted_state={
                    "is_deleted": archived,
                    "kind": "archived_or_in_trash" if archived else None,
                    "status": "observed",
                },
                raw_payload=obj,
                content_hash=record_hash,
                relations={
                    "parent_id": parent_id,
                    "parent": parent,
                    "object_type": obj.get("type") or obj.get("object"),
                    "discussion_id": obj.get("discussion_id"),
                    "page_id": str(parent.get("page_id")) if parent.get("page_id") else None,
                    "created_by_user_id": (obj.get("created_by") or {}).get("id")
                    if isinstance(obj.get("created_by"), dict)
                    else None,
                    "last_edited_by_user_id": (obj.get("last_edited_by") or {}).get("id")
                    if isinstance(obj.get("last_edited_by"), dict)
                    else None,
                    "has_children": obj.get("has_children"),
                },
                provenance=provenance,
                coverage=_coverage(len(objects), manifest),
                observation_window=window,
                capture_completeness=_completeness(manifest, notes="official Notion API response"),
                supplement_provenance=_supplement(
                    is_supplement=False, kind=None, schema_variant="official_notion_api_v1"
                ),
                visibility_routing=_visibility(
                    container="integration_visible",
                    visibility="integration_visible",
                    collected_by=None,
                    schema_version="notion-api",
                ),
                denormalized_label_snapshot={
                    "name": obj.get("name"),
                    "url": obj.get("url"),
                    "observed_at": collected_at,
                },
            )


# ------------------------------------------------------- google calendar


def _calendar_tenant(calendars: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """The authenticated account is the tenant; its primary calendar names it."""
    for calendar_id, entry in calendars.items():
        if entry.get("primary"):
            return {"workspace_id": calendar_id, "status": "observed"}
    return {"workspace_id": "unknown", "status": "unknown"}


def _calendar_records(
    archive_root: Path, manifest: dict[str, Any], result: LiveConvertResult
) -> Iterable[LedgerRecord]:
    run_id = str(manifest["run_id"])
    window = _window(manifest)
    collected_at = str(manifest["finished_at"])
    calendars: dict[str, dict[str, Any]] = {}
    loaded: list[tuple[dict[str, Any], str, dict[str, Any]]] = []
    for item in manifest.get("files", []):
        kind = str(item.get("kind", ""))
        body = _read_archived(archive_root, item)
        loaded.append((item, kind, body))
        if kind == "calendar-list":
            for entry in body.get("items") or []:
                if isinstance(entry, dict) and entry.get("id"):
                    calendars[str(entry["id"])] = entry
    tenant = _calendar_tenant(calendars)
    seen: set[str] = set()

    for item, kind, body in loaded:
        if kind == "calendar-list":
            objects = body.get("items") or []
            entity_type, endpoint = "calendar", "calendarList.list"
            profile = "live-google-calendar-list/v1"
        elif kind.startswith("events-"):
            objects = body.get("items") or []
            entity_type, endpoint = "event", "events.list"
            profile = "live-google-calendar-events/v1"
        else:
            if kind:
                result.unhandled_kinds[kind] = result.unhandled_kinds.get(kind, 0) + 1
            continue
        if not isinstance(objects, list):
            continue
        calendar_id = kind.removeprefix("events-") if entity_type == "event" else ""
        for index, original in enumerate(objects):
            if not isinstance(original, dict) or not original.get("id"):
                continue
            obj = dict(original)
            object_id = str(obj["id"])
            record_hash = content_hash(obj)
            if entity_type == "calendar":
                scope_key = ""
                source_entity_id = object_id
                deleted = bool(obj.get("deleted"))
                deleted_kind = "calendar_removed" if deleted else None
                created = None
                updated = None
                updated_status = "unknown"
                container = "calendar_list"
                relations = {
                    "access_role": obj.get("accessRole"),
                    "primary": obj.get("primary"),
                    "selected": obj.get("selected"),
                    "time_zone": obj.get("timeZone"),
                    "reminders": obj.get("defaultReminders"),
                    "notification_settings": obj.get("notificationSettings"),
                }
                labels = {
                    "summary": obj.get("summaryOverride") or obj.get("summary"),
                    "time_zone": obj.get("timeZone"),
                    "observed_at": collected_at,
                }
                scope = {
                    "kind": "google_calendar",
                    "calendar_id": object_id,
                    "calendar_id_status": "observed",
                }
            else:
                scope_key = calendar_id
                source_entity_id = f"{calendar_id}:{object_id}"
                deleted = obj.get("status") == "cancelled"
                deleted_kind = "event_cancelled" if deleted else None
                created = obj.get("created")
                updated = obj.get("updated") or created
                updated_status = "observed" if updated else "unknown"
                calendar_entry = calendars.get(calendar_id, {})
                container = str(calendar_entry.get("accessRole") or "unknown")
                organizer = obj.get("organizer") if isinstance(obj.get("organizer"), dict) else {}
                creator = obj.get("creator") if isinstance(obj.get("creator"), dict) else {}
                relations = {
                    "calendar_id": calendar_id,
                    "recurring_event_id": obj.get("recurringEventId"),
                    "original_start_time": obj.get("originalStartTime"),
                    "recurrence": obj.get("recurrence"),
                    "is_recurrence_master": bool(obj.get("recurrence")),
                    "is_recurrence_instance": bool(obj.get("recurringEventId")),
                    "organizer_email": organizer.get("email"),
                    "creator_email": creator.get("email"),
                    "attendees": obj.get("attendees") if isinstance(obj.get("attendees"), list) else [],
                    "attendee_responses": [
                        {
                            "email": attendee.get("email"),
                            "responseStatus": attendee.get("responseStatus"),
                            "optional": attendee.get("optional"),
                            "organizer": attendee.get("organizer"),
                            "self": attendee.get("self"),
                        }
                        for attendee in (obj.get("attendees") or [])
                        if isinstance(attendee, dict)
                    ],
                    "conference_data": obj.get("conferenceData"),
                    "reminders": obj.get("reminders"),
                    "attachments": obj.get("attachments")
                    if isinstance(obj.get("attachments"), list)
                    else [],
                    "status": obj.get("status"),
                    "transparency": obj.get("transparency"),
                    "visibility": obj.get("visibility"),
                }
                labels = {
                    "summary": obj.get("summary"),
                    "calendar_summary": calendar_entry.get("summaryOverride")
                    or calendar_entry.get("summary"),
                    "observed_at": collected_at,
                }
                scope = {
                    "kind": "google_calendar_event",
                    "calendar_id": calendar_id,
                    "calendar_id_status": "observed" if calendar_id else "unknown",
                    "container": container,
                }
            ledger_id = ledger_id_for(
                source="google_calendar",
                entity_type=entity_type,
                tenant_id=str(tenant["workspace_id"]),
                scope_key=scope_key,
                source_entity_id=source_entity_id,
                window_start=window["start"],
                content_hash=record_hash,
            )
            if ledger_id in seen:
                continue
            seen.add(ledger_id)
            provenance = _provenance(item, kind=kind, run_id=run_id, endpoint=endpoint)
            provenance["record_pointer"] = f"/items/{index}"
            yield LedgerRecord(
                ledger_id=ledger_id,
                schema_version=LEDGER_SCHEMA_VERSION,
                capture_profile=profile,
                source="google_calendar",
                tenant=dict(tenant),
                scope=scope,
                entity_type=entity_type,
                source_entity_id=source_entity_id,
                source_entity_key={"calendar_id": calendar_id or object_id, "event_id": object_id}
                if entity_type == "event"
                else {"calendar_id": object_id},
                source_revision_id=str(obj.get("etag")) if obj.get("etag") else None,
                source_created_at=str(created) if created else str(updated) if updated else None,
                source_updated_at=str(updated) if updated else None,
                source_updated_at_status=updated_status,
                collected_at=collected_at,
                deleted_state={"is_deleted": deleted, "kind": deleted_kind, "status": "observed"},
                raw_payload=obj,
                content_hash=record_hash,
                relations=relations,
                provenance=provenance,
                coverage=_coverage(len(objects), manifest),
                observation_window=window,
                capture_completeness=_completeness(
                    manifest,
                    notes="official Google Calendar API response",
                    lossy_fields={"attachments": "metadata_and_links_only"}
                    if obj.get("attachments")
                    else None,
                ),
                supplement_provenance=_supplement(
                    is_supplement=False, kind=None, schema_variant="official_google_calendar_v3"
                ),
                visibility_routing=_visibility(
                    container=container,
                    visibility=str(obj.get("visibility") or "default"),
                    collected_by=str(tenant["workspace_id"]),
                    schema_version="google-calendar-v3",
                ),
                denormalized_label_snapshot=labels,
            )


# ----------------------------------------------------------------- github


# The collector files one page per repository per object kind, and a run holds
# only the kinds it was asked for: the commit backfill and the REST backfill
# are separate runs over the same window. Both shapes are handled here, so a
# reader does not have to know which run produced which page -- mistaking one
# run for the whole capture is what led to "the new collector does not fetch
# pull requests" when the pull requests were simply in another run.
_GITHUB_ACTIVITY_KINDS = {
    "pull_request": ("pull_request", "updated_at"),
    "review": ("review", "submitted_at"),
    "review_comment": ("review_comment", "created_at"),
    "issue_comment": ("issue_comment", "created_at"),
    "issue": ("issue", "updated_at"),
}

_GITHUB_KST = timezone(timedelta(hours=9))


def _github_day(value: Any) -> str | None:
    """The KST day a timestamp belongs to, or None.

    The collector keys every count on KST days, so the ledger has to agree:
    an observation window taken from the run's finish date would file a whole
    month of commits under the night the backfill happened to run, and the
    coverage-by-day view would then disagree with the manifest it came from.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("z", "Z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(_GITHUB_KST).date().isoformat()


def _github_window(day: str | None, manifest: dict[str, Any]) -> dict[str, Any]:
    if day is None:
        return _window(manifest)
    return {"start": day, "end": day, "tz": "Asia/Seoul", "granularity": "day"}


def _github_commit_times(commit: dict[str, Any]) -> tuple[str | None, str | None]:
    """(authored_at, committed_at) from either shape.

    A mirror record carries them at the top level; a REST commit nests them
    under `commit.author.date` and `commit.committer.date`. One accessor keeps
    the two runs' records on the same day for the same commit.
    """
    authored = commit.get("authored_at")
    committed = commit.get("committed_at")
    if authored or committed:
        return (str(authored) if authored else None, str(committed) if committed else None)
    inner = commit.get("commit")
    if isinstance(inner, dict):
        author = inner.get("author") if isinstance(inner.get("author"), dict) else {}
        committer = inner.get("committer") if isinstance(inner.get("committer"), dict) else {}
        return (
            str(author.get("date")) if author.get("date") else None,
            str(committer.get("date")) if committer.get("date") else None,
        )
    return (None, None)


def _github_login(value: Any) -> str | None:
    if isinstance(value, dict):
        login = value.get("login")
        return str(login) if login else None
    return None


def _github_records(
    archive_root: Path, manifest: dict[str, Any], result: LiveConvertResult
) -> Iterable[LedgerRecord]:
    run_id = str(manifest["run_id"])
    collected_at = str(manifest["finished_at"])
    organization = str(manifest.get("organization") or "unknown")
    tenant = {"workspace_id": organization, "status": "observed"}
    repositories: dict[str, dict[str, Any]] = {}
    loaded: list[tuple[dict[str, Any], str, dict[str, Any]]] = []

    for item in manifest.get("files", []):
        kind = str(item.get("kind", ""))
        body = _read_archived(archive_root, item)
        loaded.append((item, kind, body))
        if kind.startswith("repositories"):
            for entry in body.get("items") or []:
                if isinstance(entry, dict) and entry.get("name"):
                    repositories[str(entry["name"])] = entry

    seen: set[str] = set()
    for item, kind, body in loaded:
        if kind.startswith("repositories"):
            objects = body.get("items") or []
            entity_type = "repository"
            profile = "live-github-repository-list/v1"
            repository = ""
        elif kind.startswith("commits-rest-"):
            objects = body.get("commits") or []
            entity_type = "commit"
            profile = REST_COMMIT_PROFILE
            repository = kind.removeprefix("commits-rest-")
        elif kind.startswith("commits-"):
            objects = body.get("commits") or []
            entity_type = "commit"
            profile = MIRROR_COMMIT_PROFILE
            repository = kind.removeprefix("commits-")
        else:
            matched = None
            for prefix, (candidate, _) in _GITHUB_ACTIVITY_KINDS.items():
                if kind.startswith(f"{prefix}-"):
                    matched = (prefix, candidate)
                    break
            if matched is None:
                if kind:
                    result.unhandled_kinds[kind] = result.unhandled_kinds.get(kind, 0) + 1
                continue
            prefix, entity_type = matched
            objects = body.get("items") or []
            profile = REST_PROFILE
            repository = kind.removeprefix(f"{prefix}-")
        if not isinstance(objects, list):
            continue
        repository = str(body.get("repository") or repository)

        for index, original in enumerate(objects):
            if not isinstance(original, dict):
                continue
            obj = dict(original)
            record_hash = content_hash(obj)
            entry = repositories.get(repository, {})

            if entity_type == "repository":
                name = str(obj.get("name") or "")
                if not name:
                    continue
                scope_key = ""
                source_entity_id = f"{organization}/{name}"
                source_entity_key = {"organization": organization, "repository": name}
                created = obj.get("created_at")
                updated = obj.get("updated_at") or obj.get("pushed_at")
                day = _github_day(updated) or _github_day(created)
                scope = {"kind": "github_organization", "organization": organization}
                relations = {
                    "archived": obj.get("archived"),
                    "visibility": obj.get("visibility"),
                    "default_branch": obj.get("default_branch"),
                    "pushed_at": obj.get("pushed_at"),
                    "fork": obj.get("fork"),
                    "listed_from": obj.get("listed_from"),
                }
                labels = {"name": name, "observed_at": collected_at}
                container = str(obj.get("visibility") or "unknown")
                revision = None
            elif entity_type == "commit":
                sha = str(obj.get("sha") or "")
                if not sha:
                    continue
                authored, committed = _github_commit_times(obj)
                scope_key = repository
                source_entity_id = f"{repository}:{sha}"
                source_entity_key = {"repository": repository, "sha": sha}
                created = authored or committed
                updated = committed or authored
                day = _github_day(updated)
                scope = {
                    "kind": "github_repository",
                    "organization": organization,
                    "repository": repository,
                    "read_from": str(body.get("source") or "unknown"),
                }
                parents = obj.get("parents")
                relations = {
                    "repository": repository,
                    "parents": parents,
                    "parent_count": obj.get("parent_count")
                    if obj.get("parent_count") is not None
                    else (len(parents) if isinstance(parents, list) else None),
                    "is_merge": obj.get("is_merge"),
                    "author_email": obj.get("author_email"),
                    "committer_email": obj.get("committer_email"),
                    "files_changed": obj.get("files_changed"),
                    "insertions": obj.get("insertions"),
                    "deletions": obj.get("deletions"),
                    "diffstat_status": obj.get("diffstat_status"),
                }
                labels = {
                    "repository": repository,
                    "subject": obj.get("subject")
                    or (obj.get("commit") or {}).get("message", "").splitlines()[0]
                    if isinstance(obj.get("commit"), dict)
                    else obj.get("subject"),
                    "observed_at": collected_at,
                }
                container = str(entry.get("visibility") or "unknown")
                revision = sha
            else:
                _, date_field = _GITHUB_ACTIVITY_KINDS[
                    entity_type if entity_type in _GITHUB_ACTIVITY_KINDS else "pull_request"
                ]
                identifier = obj.get("number") if entity_type in {"pull_request", "issue"} else obj.get("id")
                if identifier is None:
                    continue
                scope_key = repository
                source_entity_id = f"{repository}:{entity_type}:{identifier}"
                source_entity_key = {"repository": repository, entity_type: identifier}
                created = obj.get("created_at") or obj.get("submitted_at")
                updated = obj.get(date_field) or obj.get("updated_at") or created
                day = _github_day(updated)
                scope = {
                    "kind": "github_repository",
                    "organization": organization,
                    "repository": repository,
                }
                labels = {
                    "repository": repository,
                    "title": obj.get("title"),
                    "observed_at": collected_at,
                }
                relations = {
                    "repository": repository,
                    "author": _github_login(obj.get("user")),
                    "state": obj.get("state"),
                    "merged_at": obj.get("merged_at"),
                    "closed_at": obj.get("closed_at"),
                    "labels": [
                        label.get("name")
                        for label in (obj.get("labels") or [])
                        if isinstance(label, dict)
                    ]
                    if isinstance(obj.get("labels"), list)
                    else None,
                    "pull_request_url": obj.get("pull_request_url"),
                    "issue_url": obj.get("issue_url"),
                    "path": obj.get("path"),
                }
                container = str(entry.get("visibility") or "unknown")
                revision = str(obj.get("node_id")) if obj.get("node_id") else None

            window = _github_window(day, manifest)
            ledger_id = ledger_id_for(
                source="github",
                entity_type=entity_type,
                tenant_id=organization,
                scope_key=scope_key,
                source_entity_id=source_entity_id,
                window_start=window["start"],
                content_hash=record_hash,
            )
            if ledger_id in seen:
                # The same commit arrives twice when a renamed repository is
                # mirrored under both names. One commit, one row; the raw
                # pages keep both observations.
                continue
            seen.add(ledger_id)
            provenance = _provenance(item, kind=kind, run_id=run_id, endpoint=str(item.get("endpoint") or ""))
            provenance["record_pointer"] = f"/commits/{index}" if entity_type == "commit" else f"/items/{index}"
            yield LedgerRecord(
                ledger_id=ledger_id,
                schema_version=LEDGER_SCHEMA_VERSION,
                capture_profile=profile,
                source="github",
                tenant=dict(tenant),
                scope=scope,
                entity_type=entity_type,
                source_entity_id=source_entity_id,
                source_entity_key=source_entity_key,
                source_revision_id=revision,
                source_created_at=str(created) if created else None,
                source_updated_at=str(updated) if updated else None,
                source_updated_at_status="observed" if updated else "unknown",
                collected_at=collected_at,
                # These listings never report a deletion: a deleted commit or
                # pull request simply stops appearing. Absence is not an
                # observation of deletion, so it is not recorded as one.
                deleted_state={"is_deleted": False, "kind": None, "status": "unknown"},
                raw_payload=obj,
                content_hash=record_hash,
                relations=relations,
                provenance=provenance,
                coverage=_coverage(len(objects), manifest),
                observation_window=window,
                capture_completeness=_completeness(
                    manifest,
                    notes="commit read from a local bare mirror"
                    if profile == MIRROR_COMMIT_PROFILE
                    else "official GitHub REST response",
                    lossy_fields={"diffstat": str(obj.get("diffstat_status"))}
                    if obj.get("diffstat_status") and obj.get("diffstat_status") != "recorded"
                    else None,
                ),
                supplement_provenance=_supplement(
                    is_supplement=False, kind=None, schema_variant="official_github_v3"
                ),
                visibility_routing=_visibility(
                    container=container,
                    visibility=str(entry.get("visibility") or obj.get("visibility") or "unknown"),
                    collected_by=organization,
                    schema_version="github-v3",
                ),
                denormalized_label_snapshot=labels,
            )


# ------------------------------------------------------------------ slurm


def _slurm_records(
    archive_root: Path, manifest: dict[str, Any], result: LiveConvertResult
) -> Iterable[LedgerRecord]:
    """One record per finished parent job, on the KST day it ended.

    Step rows (`.batch`, `.extern`) are archived but not converted. They are
    sub-resources of a job rather than activities, and giving them an
    entity_type needs a third category alongside activity and dimension --
    that is a schema decision, not a conversion detail. They stay in the raw
    archive with all 117 columns, so nothing is lost by waiting; the count of
    skipped step rows is reported so the gap is visible rather than implied.
    """
    run_id = str(manifest["run_id"])
    collected_at = str(manifest["finished_at"])
    seen: set[str] = set()
    step_rows_skipped = 0

    for item in manifest.get("files", []):
        kind = str(item.get("kind", ""))
        if not kind.startswith("jobs-"):
            if kind:
                result.unhandled_kinds[kind] = result.unhandled_kinds.get(kind, 0) + 1
            continue
        body = _read_archived(archive_root, item)
        columns = body.get("columns")
        rows = body.get("rows")
        if not isinstance(columns, list) or not isinstance(rows, list):
            continue
        cloud = str(body.get("cloud") or "unknown")
        day = str(body.get("day") or "")
        index_of = {str(name): position for position, name in enumerate(columns)}
        for position in ("JobID", "State", "End", "Cluster"):
            if position not in index_of:
                raise ValueError(f"archived slurm page lacks the {position} column: {item['path']}")
        window = {
            "start": day,
            "end": day,
            "tz": str(body.get("timezone") or "Asia/Seoul"),
            "granularity": "day",
        }
        tenant = {"workspace_id": cloud, "status": "observed"}

        for row_index, row in enumerate(rows):
            if not isinstance(row, list) or len(row) != len(columns):
                continue
            values = {name: row[offset] for name, offset in index_of.items()}
            job_id = str(values.get("JobID") or "")
            if not job_id:
                continue
            if "." in job_id:
                step_rows_skipped += 1
                continue
            obj = {str(name): row[offset] for offset, name in enumerate(columns)}
            record_hash = content_hash(obj)
            # The cluster name is used exactly as sacct reported it. The
            # collector's CLUSTER_MAP exists to name output folders, and
            # importing it here would both couple the ledger to the collector
            # and put a derived value in a row that is supposed to hold what
            # the source said. A consumer that wants the folder name can map
            # it; a row that has already been renamed cannot be un-renamed.
            cluster = str(values.get("Cluster") or "unknown")
            source_entity_id = f"{cloud}:{job_id}"
            ledger_id = ledger_id_for(
                source="slurm",
                entity_type="job",
                tenant_id=cloud,
                scope_key=cluster,
                source_entity_id=source_entity_id,
                window_start=day,
                content_hash=record_hash,
            )
            if ledger_id in seen:
                continue
            seen.add(ledger_id)
            state = str(values.get("State") or "")
            ended = str(values.get("End") or "") or None
            submitted = str(values.get("Submit") or "") or None
            started = str(values.get("Start") or "") or None
            provenance = _provenance(
                item, kind=kind, run_id=run_id, endpoint=str(item.get("endpoint") or "")
            )
            provenance["record_pointer"] = f"/rows/{row_index}"
            yield LedgerRecord(
                ledger_id=ledger_id,
                schema_version=LEDGER_SCHEMA_VERSION,
                capture_profile=SLURM_PROFILE,
                source="slurm",
                tenant=dict(tenant),
                scope={
                    "kind": "slurm_cluster",
                    "cloud": cloud,
                    "cluster": cluster,
                    "cluster_naming": "as_reported_by_sacct",
                },
                entity_type="job",
                source_entity_id=source_entity_id,
                source_entity_key={"cloud": cloud, "job_id": job_id, "cluster": cluster},
                source_revision_id=None,
                # sacct reports no Submit for the naver cluster, so the
                # created time is genuinely absent there rather than unknown
                # by omission. End is what every cluster reports.
                source_created_at=submitted or started or ended,
                source_updated_at=ended,
                source_updated_at_status="observed" if ended else "unknown",
                collected_at=collected_at,
                deleted_state={"is_deleted": False, "kind": None, "status": "unknown"},
                raw_payload=obj,
                content_hash=record_hash,
                relations={
                    "cluster": cluster,
                    "state": state,
                    "user": values.get("User"),
                    "account": values.get("Account"),
                    "partition": values.get("Partition"),
                    "alloc_tres": values.get("AllocTRES"),
                    "elapsed": values.get("Elapsed"),
                    "exit_code": values.get("ExitCode"),
                    "node_list": values.get("NodeList"),
                    "submit": submitted,
                    "start": started,
                    "end": ended,
                },
                provenance=provenance,
                coverage=_coverage(len(rows), manifest),
                observation_window=window,
                capture_completeness=_completeness(
                    manifest,
                    notes="sacct export, all 117 columns preserved; step rows archived but not converted",
                    lossy_fields={"submit_time": "absent for this cluster"} if not submitted else None,
                ),
                supplement_provenance=_supplement(
                    is_supplement=False, kind=None, schema_variant="sacct_117_column_export"
                ),
                visibility_routing=_visibility(
                    container=cluster,
                    visibility="internal",
                    collected_by=cloud,
                    schema_version="sacct-117",
                ),
                denormalized_label_snapshot={
                    "job_name": values.get("JobName"),
                    "cluster": cluster,
                    "state": state,
                    "observed_at": collected_at,
                },
            )
    if step_rows_skipped:
        result.unhandled_kinds["slurm_step_rows_not_converted"] = step_rows_skipped


# ------------------------------------------------------------------ api


_STREAMS = {
    "slack": _slack_records,
    "notion": _notion_records,
    "google_calendar": _calendar_records,
    "github": _github_records,
    "slurm": _slurm_records,
}


def convert_live_run(
    *,
    archive_root: Path,
    manifest_path: Path,
    out_root: Path,
    source: str,
    dry_run: bool = False,
) -> LiveConvertResult:
    source = _canonical_source(source)
    if source not in _STREAMS:
        raise ValueError(f"live conversion supports {', '.join(LIVE_SOURCES)}")
    manifest = _load_manifest(manifest_path, source)
    result = LiveConvertResult(source=source, run_id=str(manifest["run_id"]))
    rows: list[str] = []
    counts: dict[str, int] = {}
    for record in _STREAMS[source](archive_root, manifest, result):
        payload = record.to_dict()
        errors = validate_record(payload)
        if errors:
            result.schema_errors += 1
            if len(result.schema_error_samples) < 20:
                result.schema_error_samples.append(f"{record.ledger_id}: {errors[0]}")
            continue
        rows.append(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        result.records_written += 1
        counts[record.entity_type] = counts.get(record.entity_type, 0) + 1
    result.by_entity_type = counts
    if not dry_run:
        target = out_root / "ledger" / source / f"live-{result.run_id}.jsonl"
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".jsonl.tmp")
        temporary.write_text("\n".join(sorted(rows)) + ("\n" if rows else ""), encoding="utf-8")
        os.replace(temporary, target)
        result.output_path = str(target)
    return result
