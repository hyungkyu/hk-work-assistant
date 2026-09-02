"""Shared helpers for legacy -> standard ledger conversion.

Every function here is read-only with respect to legacy inputs. Nothing in
this module writes to, moves, or deletes a source file.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterator

KST = timezone(timedelta(hours=9))
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Abandoned rsync transfers. Principle 2: excluded unconditionally, by path,
# because some of them parse successfully and would not be caught by an
# error filter alone.
PARTIAL_TRANSFER_MARKER = ".rsync-partial"

# Notion stores unsupported property types as a type label instead of a value.
TYPE_LABEL_RE = re.compile(r"^<[a-z_]+>$")


def is_partial_transfer(path: Path) -> bool:
    return PARTIAL_TRANSFER_MARKER in path.parts


def canonical_json(value: Any) -> str:
    """Stable JSON used for hashing. Sorted keys, no insignificant whitespace."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def text_hash(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


@lru_cache(maxsize=4096)
def file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def read_json(path: Path) -> tuple[Any, str | None]:
    """(value, error). Read-only; never raises for malformed input."""
    try:
        raw = path.read_bytes()
    except OSError as error:
        return None, f"read_error:{type(error).__name__}"
    if not raw.strip():
        return None, "empty_file"
    try:
        return json.loads(raw.decode("utf-8")), None
    except UnicodeDecodeError:
        return None, "unicode_decode_error"
    except json.JSONDecodeError as error:
        return None, f"json_decode_error:line{error.lineno}"


def iso_or_none(value: Any) -> str | None:
    """Normalize a legacy timestamp string to ISO-8601 with an explicit offset.

    Legacy mixes tz-aware (`_meta.collected_at`) and tz-naive local
    (`collected_at`) values. Naive values are local KST by collector
    convention, so they are stamped +09:00 rather than silently read as UTC.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=KST)
    return parsed.isoformat()


def slack_ts_to_iso(value: Any) -> str | None:
    """Slack `ts` is an epoch 'seconds.microseconds' string."""
    if not isinstance(value, str) or "." not in value:
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
    except (ValueError, OSError, OverflowError):
        return None


def observation_window(value: Any, fallback_date: str | None) -> dict[str, Any]:
    """Build observation_window from a legacy `date_range` or a path date.

    Legacy files are day slices. Without this, a caller cannot tell whether
    two records of the same object should be summed or de-duplicated.
    """
    start = end = None
    if isinstance(value, dict):
        start = value.get("start") if DATE_RE.match(str(value.get("start", ""))) else None
        end = value.get("end") if DATE_RE.match(str(value.get("end", ""))) else None
    if not start and fallback_date and DATE_RE.match(fallback_date):
        start = end = fallback_date
    if not start:
        return {"start": None, "end": None, "tz": "+09:00", "granularity": "unknown"}
    return {
        "start": start,
        "end": end or start,
        "tz": "+09:00",
        "granularity": "day" if (end or start) == start else "range",
    }


def visibility_routing(meta: Any, *, root: str, container: str) -> dict[str, Any]:
    """Preserve the legacy visibility decision plus any post-hoc patch marker."""
    routing: dict[str, Any] = {
        "storage_root": root,
        "container": container,
        "visibility": None,
        "access_list": None,
        "collected_by": None,
        "source_schema_version": None,
        "patched_at": None,
        "meta_present": False,
    }
    if isinstance(meta, dict):
        routing["meta_present"] = True
        routing["visibility"] = meta.get("visibility")
        routing["access_list"] = meta.get("access_list")
        routing["collected_by"] = meta.get("collected_by")
        routing["source_schema_version"] = meta.get("source_schema_version")
    return routing


def count_type_labels(properties: Any) -> dict[str, int]:
    """Count Notion properties reduced to a type label instead of a value."""
    counts: dict[str, int] = {}
    if isinstance(properties, dict):
        for value in properties.values():
            if isinstance(value, str) and TYPE_LABEL_RE.match(value):
                counts[value] = counts.get(value, 0) + 1
    return counts


@dataclass(frozen=True, slots=True)
class LegacyPath:
    """Classification of a legacy daily_raw path."""

    path: Path
    root: str            # shared | personal | hk_private | other
    source: str | None   # slack | notion | gcal | ...
    container: str | None
    date: str | None
    layout: str          # current | legacy_no_source_dir | thread_store | other

    @property
    def is_partial(self) -> bool:
        return is_partial_transfer(self.path)


def classify_path(path: Path, legacy_root: Path) -> LegacyPath:
    try:
        parts = path.relative_to(legacy_root).parts
    except ValueError:
        parts = path.parts
    root = parts[0] if parts else "other"
    if len(parts) >= 2 and parts[1] == "thread_store":
        return LegacyPath(path, root, "slack", "thread_store", None, "thread_store")
    if len(parts) >= 3 and parts[1] == "daily_raw" and DATE_RE.match(parts[2]):
        day = parts[2]
        tail = parts[3:]
        if tail and tail[0] in {"slack", "notion", "gcal", "github", "slurm", "gdrive"}:
            container = tail[1] if len(tail) > 2 else None
            return LegacyPath(path, root, tail[0], container, day, "current")
        # 2025-05-16..2026-03-31: Notion written straight into date/common/
        container = tail[0] if len(tail) > 1 else None
        return LegacyPath(path, root, "notion", container, day, "legacy_no_source_dir")
    return LegacyPath(path, root, None, None, None, "other")


class MetaSignalResolver:
    """Reads meta.json completeness signals for a (root, date, source).

    `status` is ignored on purpose: it is hardcoded to "ok" in every legacy
    meta.json, including on days with truncation and rate-limit loss.
    """

    def __init__(self, legacy_root: Path) -> None:
        self._legacy_root = legacy_root
        self._cache: dict[tuple[str, str, str], dict[str, Any]] = {}

    def get(self, root: str, day: str | None, source: str) -> dict[str, Any]:
        if not day:
            return self._unknown("no_observation_date")
        key = (root, day, source)
        if key not in self._cache:
            self._cache[key] = self._load(root, day, source)
        return self._cache[key]

    def _load(self, root: str, day: str, source: str) -> dict[str, Any]:
        path = self._legacy_root / root / "daily_raw" / day / source / "meta.json"
        if not path.is_file():
            return self._unknown("meta_json_absent")
        value, error = read_json(path)
        if error or not isinstance(value, dict):
            return self._unknown(f"meta_json_unreadable:{error or 'not_an_object'}")
        warnings = value.get("truncation_warnings")
        warnings = warnings if isinstance(warnings, list) else []
        rate_limit = value.get("rate_limit_hits")
        rate_limit = rate_limit if isinstance(rate_limit, int) else None
        scan = value.get("channel_scan") if isinstance(value.get("channel_scan"), dict) else {}
        # Slack records truncation and rate-limit signals. Calendar and Notion
        # collectors have no such code, so absence there is not evidence of a
        # clean run.
        records_signals = bool(warnings) or rate_limit is not None
        return {
            "status": "recorded" if records_signals else "not_recorded",
            "truncated": bool(warnings),
            "truncation_events": len(warnings),
            "rate_limit_hits": rate_limit,
            "channels_scanned": len(scan.get("scanned_channels", []) or []),
            "channels_empty": len(scan.get("empty_channels", []) or []),
            "channels_active": len(scan.get("active_channels", []) or []),
            "legacy_status_field": value.get("status"),
            "legacy_status_is_trustworthy": False,
            "notes": None,
        }

    @staticmethod
    def _unknown(reason: str) -> dict[str, Any]:
        return {
            "status": "unknown",
            "truncated": None,
            "truncation_events": None,
            "rate_limit_hits": None,
            "channels_scanned": None,
            "channels_empty": None,
            "channels_active": None,
            "legacy_status_field": None,
            "legacy_status_is_trustworthy": False,
            "notes": reason,
        }


class SlackWorkspaceResolver:
    """Resolves the Slack workspace id for a given observation date.

    Messages carry no `team` field. `all_users.json` keeps the raw
    `users.list` response, whose `team_id` is present on every user record,
    so the workspace is recovered by joining on the same date. When that file
    is missing the workspace is reported as unknown rather than guessed
    (principle 5's rule, applied to Slack).
    """

    def __init__(self, legacy_root: Path) -> None:
        self._legacy_root = legacy_root
        self._cache: dict[str, tuple[str | None, str]] = {}
        self._fallback: str | None = None

    def prime(self) -> str | None:
        """Scan every all_users.json once; if exactly one workspace exists,
        remember it so dates without the file can still be resolved."""
        found: set[str] = set()
        for root in ("shared", "personal"):
            base = self._legacy_root / root / "daily_raw"
            if not base.is_dir():
                continue
            for day_dir in sorted(base.iterdir()):
                if not DATE_RE.match(day_dir.name):
                    continue
                workspace, _ = self._read(day_dir / "slack" / "all_users.json")
                if workspace:
                    found.add(workspace)
        self._fallback = next(iter(found)) if len(found) == 1 else None
        return self._fallback

    def get(self, day: str | None) -> tuple[str, str]:
        """Returns (workspace_id, status)."""
        if day and day in self._cache:
            workspace, status = self._cache[day]
            if workspace:
                return workspace, status
        if day:
            for root in ("shared", "personal"):
                path = self._legacy_root / root / "daily_raw" / day / "slack" / "all_users.json"
                workspace, status = self._read(path)
                if workspace:
                    self._cache[day] = (workspace, status)
                    return workspace, status
            self._cache[day] = (None, "unknown")
        if self._fallback:
            return self._fallback, "resolved_single_workspace_fallback"
        return "unknown", "unknown"

    @staticmethod
    def _read(path: Path) -> tuple[str | None, str]:
        if not path.is_file():
            return None, "unknown"
        value, error = read_json(path)
        if error or not isinstance(value, dict):
            return None, "unknown"
        for user in value.get("users") or []:
            if isinstance(user, dict) and isinstance(user.get("team_id"), str):
                return user["team_id"], "resolved_from_all_users"
        return None, "unknown"


def iter_json_files(root: Path) -> Iterator[Path]:
    """Sorted JSON files under root, with partial transfers excluded."""
    if not root.is_dir():
        return
    for path in sorted(root.rglob("*.json")):
        if path.is_file() and not is_partial_transfer(path):
            yield path


def parse_date(value: str) -> date:
    return date.fromisoformat(value)
