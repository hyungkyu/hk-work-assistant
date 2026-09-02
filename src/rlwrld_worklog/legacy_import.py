from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import TimelineEvent
from .normalizers import normalize_calendar, normalize_notion, normalize_slack


@dataclass
class LegacyImportStats:
    files_seen: int = 0
    files_invalid: int = 0
    records_seen: int = 0
    records_normalized: int = 0
    records_skipped: int = 0


def _json_files(root: Path) -> Iterator[Path]:
    yield from sorted(path for path in root.rglob("*.json") if path.is_file())


def _walk_dicts(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            yield from _walk_dicts(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk_dicts(nested)


def iter_legacy_slack(root: Path, stats: LegacyImportStats) -> Iterator[TimelineEvent]:
    for path in sorted(root.glob("*/slack/**/*.json")):
        if path.name in {"meta.json", "all_users.json"}:
            continue
        stats.files_seen += 1
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            stats.files_invalid += 1
            continue
        default_channel = value.get("channel_id") if isinstance(value, dict) else None
        seen_in_file: set[tuple[str, str]] = set()
        for item in _walk_dicts(value):
            timestamp = item.get("ts")
            if not timestamp or ("text" not in item and not item.get("deleted")):
                continue
            channel = item.get("channel") or item.get("channel_id") or default_channel
            if isinstance(channel, dict):
                channel = channel.get("id")
            if not channel:
                continue
            key = (str(channel), str(timestamp))
            if key in seen_in_file:
                continue
            seen_in_file.add(key)
            stats.records_seen += 1
            record = dict(item)
            record["channel"] = str(channel)
            try:
                event = normalize_slack(record, self_user_id="")
            except (KeyError, TypeError, ValueError):
                stats.records_skipped += 1
                continue
            stats.records_normalized += 1
            yield event


def iter_legacy_calendar(root: Path, stats: LegacyImportStats) -> Iterator[TimelineEvent]:
    for path in sorted(root.glob("*/gcal/common/all_events.json")):
        stats.files_seen += 1
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            stats.files_invalid += 1
            continue
        for item in value.get("events", []):
            if not isinstance(item, dict) or not item.get("id"):
                continue
            stats.records_seen += 1
            record = dict(item)
            organizer = record.get("organizer") or {}
            creator = record.get("creator") or {}
            record["calendar_id"] = (
                record.get("calendar_id")
                or organizer.get("email")
                or creator.get("email")
                or "legacy-unknown-calendar"
            )
            try:
                event = normalize_calendar(record)
            except (KeyError, TypeError, ValueError):
                stats.records_skipped += 1
                continue
            stats.records_normalized += 1
            yield event


def iter_legacy_notion(root: Path, stats: LegacyImportStats) -> Iterator[TimelineEvent]:
    for path in sorted(root.glob("*/notion/**/*.json")):
        if path.name in {"meta.json", "all_users.json"}:
            continue
        stats.files_seen += 1
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            stats.files_invalid += 1
            continue
        seen_in_file: set[tuple[str, str]] = set()
        for item in _walk_dicts(value):
            if not item.get("id") or not item.get("last_edited_time"):
                continue
            url = str(item.get("url") or "")
            if "notion.so" not in url and "notion.site" not in url and "properties" not in item:
                continue
            key = (str(item["id"]), str(item["last_edited_time"]))
            if key in seen_in_file:
                continue
            seen_in_file.add(key)
            stats.records_seen += 1
            try:
                event = normalize_notion(item, legacy_text=item.get("_blocks_text") or "")
            except (KeyError, TypeError, ValueError):
                stats.records_skipped += 1
                continue
            stats.records_normalized += 1
            yield event


def iter_legacy_events(
    daily_raw_root: Path,
    source: str,
    stats: LegacyImportStats,
) -> Iterable[TimelineEvent]:
    if source == "slack":
        return iter_legacy_slack(daily_raw_root, stats)
    if source == "google-calendar":
        return iter_legacy_calendar(daily_raw_root, stats)
    if source == "notion":
        return iter_legacy_notion(daily_raw_root, stats)
    raise ValueError(f"unsupported legacy source: {source}")
