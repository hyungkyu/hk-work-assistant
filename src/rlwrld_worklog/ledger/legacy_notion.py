"""Legacy Notion daily_raw -> standard v1 ledger records.

Emits three entity types:

  page     from the raw source stores (`notion/common`, `notion/restricted`,
           and the pre-2026-04 layout that wrote straight into `date/common/`)
  block    from `pages[]._blocks[]`, which holds real Notion block objects and
           is the only near-lossless part of the legacy Notion capture
  comment  see `salvage_comments` below

`_blocks_text` is emitted separately as an ExtractedText artifact: it is a
collector-derived flattening that cannot be re-fetched for pages whose block
originals were never stored, so it must not be silently folded into
raw_payload (principle 4).

Attribution buckets (authored_pages, block_edits, mentioned_in,
mentioned_others, assignments) are never converted (principle 3).

`salvage_comments`: Notion comment objects exist ONLY inside attribution
files. They are genuine source objects, not derived attribution, so the
converter can lift them out under their own capture_profile with
`source_file_kind="attribution"` in provenance. This is off by default
because it reads an attribution file; enable it only with Codex's approval.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from .common import (
    LegacyPath,
    MetaSignalResolver,
    classify_path,
    content_hash,
    count_type_labels,
    file_sha256,
    iso_or_none,
    observation_window,
    read_json,
    text_hash,
    visibility_routing,
)
from .schema import (
    CAPTURE_PROFILES,
    CONVERTER_VERSION,
    LEDGER_SCHEMA_VERSION,
    ExtractedText,
    LedgerRecord,
    ledger_id_for,
)

RAW_CONTAINERS = {"common", "restricted"}
EXCLUDED_CONTAINERS = {"attribution", "personal"}
EXCLUDED_FILENAMES = {"meta.json", "all_users.json"}

# Notion has no tenant id anywhere in the legacy capture.
NOTION_WORKSPACE = "unknown"


@dataclass
class NotionConvertStats:
    files_seen: int = 0
    files_skipped_partial: int = 0
    files_skipped_container: int = 0
    files_unreadable: int = 0
    pages_seen: int = 0
    pages_converted: int = 0
    pages_dropped_no_id: int = 0
    pages_deduplicated: int = 0
    blocks_converted: int = 0
    comments_seen: int = 0
    comments_converted: int = 0
    comments_deduplicated: int = 0
    extracted_text_artifacts: int = 0
    extracted_text_chars: int = 0
    pages_with_raw_blocks: int = 0
    pages_without_raw_blocks: int = 0
    deleted_state_unknown: int = 0
    property_type_labels: int = 0
    legacy_layout_pages: int = 0
    completeness_recorded: int = 0
    completeness_not_recorded: int = 0
    completeness_unknown: int = 0
    unreadable_files: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        value = {
            key: getattr(self, key)
            for key in self.__dataclass_fields__
            if key != "unreadable_files"
        }
        value["unreadable_files"] = self.unreadable_files[:50]
        return value


def _page_relations(page: dict[str, Any], file_payload: dict[str, Any]) -> dict[str, Any]:
    blocks = page.get("_blocks")
    return {
        "notion_source_key": file_payload.get("source_key"),
        "notion_source_id": file_payload.get("source_id"),
        "created_by_user_id": page.get("created_by"),
        "last_edited_by_user_id": page.get("last_edited_by"),
        # Legacy stored these as bare UUID strings; the Notion API returns
        # {object, id} objects. Consumers must not assume the API shape.
        "user_id_shape": "uuid_string",
        "mentioned_user_ids": page.get("_mentioned_user_ids") or [],
        "block_author_ids": page.get("_block_author_ids") or [],
        "has_raw_blocks": isinstance(blocks, list) and bool(blocks),
        "raw_block_count": len(blocks) if isinstance(blocks, list) else 0,
        # Relation properties were reduced to the literal "<relation>", so
        # page-to-page relations cannot be recovered from legacy at all.
        "relation_properties_recoverable": False,
        "parent_page_id": None,
        "parent_status": "unknown",
    }


def _page_deleted_state() -> dict[str, Any]:
    # `archived` / `in_trash` were never captured.
    return {"is_deleted": None, "kind": None, "status": "unknown"}


def _flatten_blocks(blocks: Any, parent_id: str | None = None) -> Iterator[tuple[dict[str, Any], str | None, str]]:
    """Yield (block, parent_block_id, pointer_suffix) depth-first.

    The legacy collector capped recursion at depth 3 and nested children under
    `_children`, so this walks that shape rather than the API's `has_children`.
    """
    if not isinstance(blocks, list):
        return
    for index, block in enumerate(blocks):
        if not isinstance(block, dict):
            continue
        pointer = f"/{index}"
        yield block, parent_id, pointer
        for child, _, child_pointer in _flatten_blocks(block.get("_children"), block.get("id")):
            yield child, block.get("id"), f"{pointer}/_children{child_pointer}"


def iter_notion_records(
    legacy_root: Path,
    *,
    stats: NotionConvertStats,
    meta_resolver: MetaSignalResolver,
    roots: tuple[str, ...] = ("shared", "personal"),
    salvage_comments: bool = False,
) -> Iterator[LedgerRecord | ExtractedText]:
    seen_ledger_ids: set[str] = set()
    for root in roots:
        base = legacy_root / root
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.json")):
            if not path.is_file():
                continue
            info = classify_path(path, legacy_root)
            if info.source != "notion":
                continue
            if info.is_partial:
                stats.files_skipped_partial += 1
                continue
            if path.name in EXCLUDED_FILENAMES:
                continue
            if info.container in EXCLUDED_CONTAINERS:
                stats.files_skipped_container += 1
                if salvage_comments and info.container == "attribution" and path.name == "comments.json":
                    yield from _convert_comments(
                        path, info, stats=stats, seen_ledger_ids=seen_ledger_ids, legacy_root=legacy_root
                    )
                continue
            if info.container not in RAW_CONTAINERS:
                continue
            stats.files_seen += 1
            yield from _convert_pages_file(
                path,
                info,
                stats=stats,
                meta_resolver=meta_resolver,
                seen_ledger_ids=seen_ledger_ids,
                legacy_root=legacy_root,
            )


def _convert_pages_file(
    path: Path,
    info: LegacyPath,
    *,
    stats: NotionConvertStats,
    meta_resolver: MetaSignalResolver,
    seen_ledger_ids: set[str],
    legacy_root: Path,
) -> Iterator[LedgerRecord | ExtractedText]:
    payload, error = read_json(path)
    if error or not isinstance(payload, dict):
        stats.files_unreadable += 1
        stats.unreadable_files.append(f"{path.relative_to(legacy_root)}:{error or 'not_an_object'}")
        return
    pages = payload.get("pages")
    if not isinstance(pages, list):
        return
    if info.layout == "legacy_no_source_dir":
        stats.legacy_layout_pages += len(pages)

    window = observation_window(payload.get("date_range"), info.date)
    completeness_base = dict(meta_resolver.get(info.root, info.date, "notion"))
    if completeness_base["status"] == "recorded":
        stats.completeness_recorded += 1
    elif completeness_base["status"] == "not_recorded":
        stats.completeness_not_recorded += 1
    else:
        stats.completeness_unknown += 1
    routing = visibility_routing(payload.get("_meta"), root=info.root, container=info.container or "")
    routing["routing_anomaly"] = None
    file_sha = file_sha256(str(path))
    relative = str(path.relative_to(legacy_root))
    declared = payload.get("page_count")
    collected_at = iso_or_none(payload.get("collected_at")) or iso_or_none(
        (payload.get("_meta") or {}).get("collected_at") if isinstance(payload.get("_meta"), dict) else None
    )
    scope_base = {
        "kind": "notion_source",
        "notion_source_key": payload.get("source_key"),
        "notion_source_id": payload.get("source_id"),
        "notion_source_type": payload.get("source_type"),
    }

    for index, page in enumerate(pages):
        if not isinstance(page, dict):
            continue
        stats.pages_seen += 1
        page_id = page.get("id")
        if not isinstance(page_id, str) or not page_id:
            stats.pages_dropped_no_id += 1
            continue

        raw_payload = dict(page)
        record_hash = content_hash(raw_payload)
        ledger_id = ledger_id_for(
            source="notion",
            entity_type="page",
            tenant_id=NOTION_WORKSPACE,
            # Empty on purpose: a page's identity is its page id. The source
            # query it came from is recorded in scope and provenance.
            scope_key="",
            source_entity_id=page_id,
            window_start=window["start"],
            content_hash=record_hash,
        )
        if ledger_id in seen_ledger_ids:
            stats.pages_deduplicated += 1
            continue
        seen_ledger_ids.add(ledger_id)

        labels = count_type_labels(page.get("properties"))
        stats.property_type_labels += sum(labels.values())
        completeness = dict(completeness_base)
        completeness["lossy_fields"] = labels

        blocks = page.get("_blocks")
        # Blocks carry no page properties, so they must not inherit the
        # page's lossy property labels.
        block_completeness = dict(completeness_base)
        block_completeness["lossy_fields"] = {}
        has_blocks = isinstance(blocks, list) and bool(blocks)
        if has_blocks:
            stats.pages_with_raw_blocks += 1
        else:
            stats.pages_without_raw_blocks += 1

        deleted_state = _page_deleted_state()
        stats.deleted_state_unknown += 1
        stats.pages_converted += 1

        yield LedgerRecord(
            ledger_id=ledger_id,
            schema_version=LEDGER_SCHEMA_VERSION,
            capture_profile=CAPTURE_PROFILES["notion_page"],
            source="notion",
            tenant={"workspace_id": NOTION_WORKSPACE, "status": "unknown"},
            scope=dict(scope_base),
            entity_type="page",
            source_entity_id=page_id,
            source_entity_key={"page_id": page_id},
            source_revision_id=page.get("last_edited_time"),
            source_created_at=iso_or_none(page.get("created_time")),
            source_updated_at=iso_or_none(page.get("last_edited_time")),
            source_updated_at_status="observed" if page.get("last_edited_time") else "unknown",
            collected_at=collected_at,
            deleted_state=deleted_state,
            raw_payload=raw_payload,
            content_hash=record_hash,
            relations=_page_relations(page, payload),
            provenance={
                "source_file": relative,
                "source_file_sha256": file_sha,
                "source_file_kind": info.container or "common",
                "record_pointer": f"/pages/{index}",
                "legacy_layout_version": info.layout,
                "converter_version": CONVERTER_VERSION,
                "api_endpoint": "databases/query" if payload.get("source_type") == "database" else "blocks/children",
                "cursor": None,
                "collector_run_id": None,
            },
            coverage={
                "observation_role": "historical_observation",
                "record_count_in_file": len(pages),
                "declared_count_in_file": declared if isinstance(declared, int) else None,
                "count_matches_declared": (len(pages) == declared) if isinstance(declared, int) else None,
                "permission_gap": None,
                "errors": [],
            },
            observation_window=window,
            capture_completeness=completeness,
            supplement_provenance={
                "is_supplement": None,
                "supplement_kind": None,
                "schema_variant": "phase1_source_dump",
                "merged_into_primary": None,
                "file_supplement_runs": None,
            },
            visibility_routing=routing,
            denormalized_label_snapshot={
                "page_title": page.get("title"),
                "notion_source_name": payload.get("source_name"),
                "observed_at": collected_at,
            },
        )

        # Raw Notion block objects, promoted to first-class records.
        for block, parent_block_id, pointer in _flatten_blocks(blocks):
            block_id = block.get("id")
            if not isinstance(block_id, str) or not block_id:
                continue
            block_payload = dict(block)
            block_hash = content_hash(block_payload)
            block_ledger_id = ledger_id_for(
                source="notion",
                entity_type="block",
                tenant_id=NOTION_WORKSPACE,
                scope_key=page_id,
                source_entity_id=block_id,
                window_start=window["start"],
                content_hash=block_hash,
            )
            if block_ledger_id in seen_ledger_ids:
                continue
            seen_ledger_ids.add(block_ledger_id)
            stats.blocks_converted += 1
            yield LedgerRecord(
                ledger_id=block_ledger_id,
                schema_version=LEDGER_SCHEMA_VERSION,
                capture_profile=CAPTURE_PROFILES["notion_block"],
                source="notion",
                tenant={"workspace_id": NOTION_WORKSPACE, "status": "unknown"},
                scope={**scope_base, "kind": "notion_page", "parent_page_id": page_id},
                entity_type="block",
                source_entity_id=block_id,
                source_entity_key={"block_id": block_id, "page_id": page_id},
                source_revision_id=block.get("last_edited_time"),
                source_created_at=iso_or_none(block.get("created_time")),
                source_updated_at=iso_or_none(block.get("last_edited_time")),
                source_updated_at_status="observed" if block.get("last_edited_time") else "unknown",
                collected_at=collected_at,
                deleted_state={"is_deleted": None, "kind": None, "status": "unknown"},
                raw_payload=block_payload,
                content_hash=block_hash,
                relations={
                    "page_id": page_id,
                    "parent_block_id": parent_block_id,
                    "block_type": block.get("type"),
                    "created_by_user_id": (block.get("created_by") or {}).get("id")
                    if isinstance(block.get("created_by"), dict)
                    else None,
                    "last_edited_by_user_id": (block.get("last_edited_by") or {}).get("id")
                    if isinstance(block.get("last_edited_by"), dict)
                    else None,
                    "user_id_shape": "object",
                    # Depth was capped at 3 by the collector, so a block with
                    # no children here may still have had them.
                    "children_complete": False,
                },
                provenance={
                    "source_file": relative,
                    "source_file_sha256": file_sha,
                    "source_file_kind": info.container or "common",
                    "record_pointer": f"/pages/{index}/_blocks{pointer}",
                    "legacy_layout_version": info.layout,
                    "converter_version": CONVERTER_VERSION,
                    "api_endpoint": "blocks/children",
                    "cursor": None,
                    "collector_run_id": None,
                },
                coverage={
                    "observation_role": "historical_observation",
                    "record_count_in_file": None,
                    "declared_count_in_file": None,
                    "count_matches_declared": None,
                    "permission_gap": {"reason": "block_recursion_depth_capped_at_3"},
                    "errors": [],
                },
                observation_window=window,
                capture_completeness=block_completeness,
                supplement_provenance={
                    "is_supplement": None,
                    "supplement_kind": None,
                    "schema_variant": "phase1_source_dump",
                    "merged_into_primary": None,
                    "file_supplement_runs": None,
                },
                visibility_routing=routing,
                denormalized_label_snapshot={"observed_at": collected_at},
            )

        # `_blocks_text` is preserved verbatim as its own artifact.
        blocks_text = page.get("_blocks_text")
        if isinstance(blocks_text, str) and blocks_text:
            stats.extracted_text_artifacts += 1
            stats.extracted_text_chars += len(blocks_text)
            yield ExtractedText(
                artifact_id=ledger_id_for(
                    source="notion",
                    entity_type="extracted_text",
                    tenant_id=NOTION_WORKSPACE,
                    scope_key=page_id,
                    # Extracted text belongs to one immutable page
                    # observation, not merely to the logical page.  The
                    # legacy tree can contain the same page/text/window in
                    # two source layouts while the enclosing page payloads
                    # (and therefore their ledger ids) differ.  Keying only
                    # on page + text collapsed those two artifacts in
                    # PostgreSQL and left one ledger observation pointing at
                    # no extracted-text row.
                    source_entity_id=ledger_id,
                    window_start=window["start"],
                    content_hash=text_hash(blocks_text),
                ),
                schema_version=LEDGER_SCHEMA_VERSION,
                ledger_id=ledger_id,
                source="notion",
                kind="notion_blocks_text",
                text=blocks_text,
                text_sha256=text_hash(blocks_text),
                char_length=len(blocks_text),
                byte_length=len(blocks_text.encode("utf-8")),
                extractor="legacy-notion-collector/_blocks_text",
                source_ref={"page_id": page_id, "has_raw_blocks": has_blocks},
                provenance={
                    "source_file": relative,
                    "source_file_sha256": file_sha,
                    "record_pointer": f"/pages/{index}/_blocks_text",
                    "legacy_layout_version": info.layout,
                    "converter_version": CONVERTER_VERSION,
                },
            )


def _convert_comments(
    path: Path,
    info: LegacyPath,
    *,
    stats: NotionConvertStats,
    seen_ledger_ids: set[str],
    legacy_root: Path,
) -> Iterator[LedgerRecord]:
    """Lift Notion comment objects out of an attribution file.

    Only the comment objects themselves are read. The surrounding attribution
    counters and per-person buckets are ignored.
    """
    payload, error = read_json(path)
    if error or not isinstance(payload, dict):
        stats.files_unreadable += 1
        stats.unreadable_files.append(f"{path.relative_to(legacy_root)}:{error or 'not_an_object'}")
        return
    comments = payload.get("comments")
    if not isinstance(comments, list):
        return
    window = observation_window(payload.get("date_range"), info.date)
    routing = visibility_routing(payload.get("_meta"), root=info.root, container=info.container or "")
    routing["routing_anomaly"] = None
    file_sha = file_sha256(str(path))
    relative = str(path.relative_to(legacy_root))
    collected_at = iso_or_none(payload.get("collected_at"))

    for index, comment in enumerate(comments):
        if not isinstance(comment, dict):
            continue
        stats.comments_seen += 1
        comment_id = comment.get("id")
        if not isinstance(comment_id, str) or not comment_id:
            continue
        raw_payload = dict(comment)
        record_hash = content_hash(raw_payload)
        ledger_id = ledger_id_for(
            source="notion",
            entity_type="comment",
            tenant_id=NOTION_WORKSPACE,
            scope_key=str(comment.get("page_id") or ""),
            source_entity_id=comment_id,
            window_start=window["start"],
            content_hash=record_hash,
        )
        if ledger_id in seen_ledger_ids:
            stats.comments_deduplicated += 1
            continue
        seen_ledger_ids.add(ledger_id)
        stats.comments_converted += 1
        yield LedgerRecord(
            ledger_id=ledger_id,
            schema_version=LEDGER_SCHEMA_VERSION,
            capture_profile=CAPTURE_PROFILES["notion_comment"],
            source="notion",
            tenant={"workspace_id": NOTION_WORKSPACE, "status": "unknown"},
            scope={"kind": "notion_page", "parent_page_id": comment.get("page_id")},
            entity_type="comment",
            source_entity_id=comment_id,
            source_entity_key={"comment_id": comment_id},
            source_revision_id=comment.get("last_edited_time"),
            source_created_at=iso_or_none(comment.get("created_time")),
            source_updated_at=iso_or_none(comment.get("last_edited_time")),
            source_updated_at_status="observed" if comment.get("last_edited_time") else "unknown",
            collected_at=collected_at,
            deleted_state={"is_deleted": None, "kind": None, "status": "unknown"},
            raw_payload=raw_payload,
            content_hash=record_hash,
            relations={
                "page_id": comment.get("page_id"),
                "discussion_id": comment.get("discussion_id"),
                "parent_type": comment.get("parent_type"),
                "parent_id": comment.get("parent_id"),
                "created_by_user_id": comment.get("created_by"),
                "user_id_shape": "uuid_string",
            },
            provenance={
                "source_file": relative,
                "source_file_sha256": file_sha,
                # Recorded explicitly so this salvage path stays auditable and
                # reversible with a single provenance filter.
                "source_file_kind": "attribution",
                "record_pointer": f"/comments/{index}",
                "legacy_layout_version": info.layout,
                "converter_version": CONVERTER_VERSION,
                "api_endpoint": "v1/comments",
                "cursor": None,
                "collector_run_id": None,
            },
            coverage={
                "observation_role": "historical_observation",
                "record_count_in_file": len(comments),
                "declared_count_in_file": payload.get("count") if isinstance(payload.get("count"), int) else None,
                "count_matches_declared": None,
                "permission_gap": {"reason": "comments_only_exist_inside_attribution_files"},
                "errors": [],
            },
            observation_window=window,
            capture_completeness={
                "status": "unknown",
                "truncated": None,
                "truncation_events": None,
                "rate_limit_hits": None,
                "channels_scanned": None,
                "channels_empty": None,
                "channels_active": None,
                "legacy_status_field": None,
                "legacy_status_is_trustworthy": False,
                "lossy_fields": {},
                "notes": "salvaged_from_attribution_file",
            },
            supplement_provenance={
                "is_supplement": None,
                "supplement_kind": None,
                "schema_variant": "attribution_comment_bucket",
                "merged_into_primary": None,
                "file_supplement_runs": None,
            },
            visibility_routing=routing,
            denormalized_label_snapshot={
                "page_title": comment.get("page_title"),
                "observed_at": collected_at,
            },
        )
