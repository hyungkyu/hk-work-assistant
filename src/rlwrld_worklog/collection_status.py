"""Read-only derived view of collection runs, for the 수집 현황 backoffice page.

Everything this module reads -- raw run directories, run manifests,
checkpoints, ledger JSONL, legacy daily_raw directories -- is external
evidence and immutable. Nothing here writes, moves or deletes any of it. The
whole view is derived and can be thrown away and rebuilt at any time.

Four rules the reader follows:

  * **No path comes from a caller.** Sources, environments and run ids are
    matched against the names the filesystem actually offers; a name that is
    not a single safe path segment is dropped before it is ever joined. Every
    resolved path is re-checked to be inside ``RAW_ARCHIVE_ROOT`` or
    ``APP_CONFIG_ROOT``.
  * **No raw content is exposed.** Counts, kinds, endpoint names, timestamps,
    local identifiers and paths only. A skip or an error is reported by its
    ``kind`` and its count, never by its details.
  * **Absent evidence is never collected.** A date with nothing behind it is
    ``not_collected``; a date whose evidence cannot be read is ``unknown``.
    Neither is ever rounded up to "collected".
  * **Expensive work is bounded and cached.** A completed run is summarized
    from its manifest, never by walking its raw directory. Only a run with no
    manifest is walked, with a hard entry cap and a short TTL cache, and the
    legacy inventory is a directory-name index -- never a content rescan.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import date as date_type, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .collection_progress import (
    DEFAULT_STALE_AFTER_SECONDS,
    is_safe_name,
    iter_snapshots,
    progress_root,
    snapshot_liveness,
)
from .collection_rules import (
    ACTIVE_RULE_VERSION,
    COLLECTOR_SOURCES,
    COLLECTOR_TO_SOURCE,
    SOURCE_TO_COLLECTOR,
    active_rule,
    digest_is_recognised,
    rule_for_version,
    stamp_from_manifest,
)

KST = timezone(timedelta(hours=9))

# Files that live beside the run manifests but are not run manifests.
NON_MANIFEST_NAMES = frozenset({"checkpoint.json", "link-queue.json"})

# A run manifest is <run_id>.json, and a repeat finish writes <run_id>.<n>.json.
_MANIFEST_NAME = re.compile(r"^(?P<run_id>[A-Za-z0-9][A-Za-z0-9._-]{0,118})\.json$")
_MANIFEST_REVISION = re.compile(r"^(?P<run_id>.+)\.(?P<revision>\d+)$")
# The run id this collector generation mints: UTC compact timestamp + hex.
_V1_RUN_ID = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{6,}$")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

MANIFEST_STATE = {
    "success": "success",
    "success_with_skips": "success_with_skips",
    "degraded": "degraded",
    "failed": "failed",
}

# Bounds. Every one of these exists so a single HTTP request cannot turn into
# an unbounded walk of a 300 GB archive.
MAX_RAW_ENTRIES_SCANNED = 250_000
MAX_ACTIVE_DAY_DIRS = 14
MAX_COVERAGE_DAYS = 400
MAX_COVERAGE_DAYS_PER_RUN = 400
MAX_LEDGER_COUNT_BYTES = 512 * 1024 * 1024
# meta.json probing is a per-date file read, so it runs only for a window a
# person would actually look at.
MAX_LEGACY_META_PROBE_DAYS = 62
LEGACY_INVENTORY_MAX_SECONDS = 5.0

WEEKDAY_LABELS = ("월", "화", "수", "목", "금", "토", "일")


# ----------------------------------------------------------------- caching


class _TtlCache:
    """Tiny LRU with a time-to-live. Safe for concurrent request threads."""

    def __init__(self, *, ttl_seconds: float, max_entries: int) -> None:
        self._ttl = ttl_seconds
        self._max = max_entries
        self._lock = threading.Lock()
        self._entries: OrderedDict[Any, tuple[float, Any]] = OrderedDict()

    def get_or_set(self, key: Any, factory: Callable[[], Any], *, now: float | None = None) -> Any:
        moment = time.monotonic() if now is None else now
        with self._lock:
            hit = self._entries.get(key)
            if hit is not None and moment - hit[0] < self._ttl:
                self._entries.move_to_end(key)
                return hit[1]
        value = factory()
        with self._lock:
            self._entries[key] = (moment, value)
            self._entries.move_to_end(key)
            while len(self._entries) > self._max:
                self._entries.popitem(last=False)
        return value

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


# A manifest summary is keyed by (path, mtime_ns, size), so it is only ever
# recomputed when the file itself changed; the archive never rewrites one.
_MANIFEST_CACHE = _TtlCache(ttl_seconds=3_600, max_entries=512)
# A live run directory changes constantly, so this is a short throttle rather
# than a real cache: it stops one page refresh from re-walking 100k files.
_RAW_SCAN_CACHE = _TtlCache(ttl_seconds=15, max_entries=256)
_LEDGER_CACHE = _TtlCache(ttl_seconds=3_600, max_entries=512)
_LEGACY_INVENTORY_CACHE = _TtlCache(ttl_seconds=300, max_entries=8)
_LEGACY_META_CACHE = _TtlCache(ttl_seconds=300, max_entries=2_048)


_ALL_CACHES = {
    "manifest": _MANIFEST_CACHE,
    "raw_scan": _RAW_SCAN_CACHE,
    "ledger": _LEDGER_CACHE,
    "legacy_inventory": _LEGACY_INVENTORY_CACHE,
    "legacy_meta": _LEGACY_META_CACHE,
}

# Which caches a screen depends on, so a refresh can drop just those. The TTLs
# themselves are deliberately left alone: they are set by what a read costs,
# and the honest answer to "is this stale?" is to say so, not to poll harder.
CACHE_GROUPS = {
    "overview": ("manifest", "raw_scan", "ledger"),
    "runs": ("manifest", "raw_scan", "ledger"),
    "coverage": ("manifest", "raw_scan", "legacy_inventory", "legacy_meta"),
}


def clear_caches(names: Iterable[str] | None = None) -> list[str]:
    """Drop every cache, or just the named ones. Returns what was dropped."""
    wanted = list(_ALL_CACHES) if names is None else [n for n in names if n in _ALL_CACHES]
    for name in wanted:
        _ALL_CACHES[name].clear()
    return wanted


# ------------------------------------------------------------------- paths


@dataclass(frozen=True)
class CollectionPaths:
    archive_root: Path
    ledger_root: Path
    config_root: Path
    legacy_root: Path

    @property
    def raw_root(self) -> Path:
        return self.archive_root / "raw"

    @property
    def manifest_root(self) -> Path:
        return self.archive_root / "manifests"

    def relative(self, path: Path) -> str:
        """A local identifier: relative to the archive root where possible."""
        for root in (self.archive_root, self.config_root):
            try:
                return str(path.relative_to(root))
            except ValueError:
                continue
        return path.name


def paths_from_environment() -> CollectionPaths:
    archive_root = Path(os.environ.get("RAW_ARCHIVE_ROOT", "/data/rlwrld-worklog"))
    ledger_root = Path(os.environ.get("LEDGER_ROOT") or archive_root / "staging" / "ledger")
    configured_config = os.environ.get("APP_CONFIG_ROOT")
    config_root = (
        Path(configured_config) if configured_config else Path.home() / ".config/hk-work-assistant"
    )
    legacy_root = Path(os.environ.get("LEGACY_ROOT") or archive_root / "legacy" / "claude" / "weekly")
    return CollectionPaths(
        archive_root=archive_root,
        ledger_root=ledger_root,
        config_root=config_root,
        legacy_root=legacy_root,
    )


def within(path: Path, root: Path) -> bool:
    """True when ``path`` is ``root`` or below it, without resolving symlinks.

    Used as the last check before any read: a reader must not leave
    RAW_ARCHIVE_ROOT or APP_CONFIG_ROOT even if a directory name on disk is
    hostile.
    """
    try:
        Path(os.path.normpath(path)).relative_to(Path(os.path.normpath(root)))
    except ValueError:
        return False
    return True


def safe_child(parent: Path, name: str, *, root: Path) -> Path | None:
    """``parent/name`` when ``name`` is one safe segment inside ``root``."""
    if not is_safe_name(name):
        return None
    child = parent / name
    return child if within(child, root) else None


def _dir_names(path: Path) -> list[str]:
    try:
        return sorted(
            entry.name
            for entry in os.scandir(path)
            if entry.is_dir(follow_symlinks=False) and is_safe_name(entry.name)
        )
    except OSError:
        return []


def _file_names(path: Path) -> list[str]:
    try:
        return sorted(
            entry.name
            for entry in os.scandir(path)
            if entry.is_file(follow_symlinks=False) and is_safe_name(entry.name)
        )
    except OSError:
        return []


# -------------------------------------------------------------- timestamps


def parse_instant(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def kst_date(value: datetime | None) -> str | None:
    return value.astimezone(KST).date().isoformat() if value is not None else None


def kst_day_bounds(day: date_type) -> tuple[datetime, datetime]:
    """[00:00, 24:00) of one KST calendar day, as UTC-comparable instants."""
    start = datetime(day.year, day.month, day.day, tzinfo=KST)
    return start, start + timedelta(days=1)


def weekday_label(day: date_type) -> str:
    return WEEKDAY_LABELS[day.weekday()]


def parse_iso_date(value: str) -> date_type | None:
    if not _ISO_DATE.match(value or ""):
        return None
    try:
        return date_type.fromisoformat(value)
    except ValueError:
        return None


# ---------------------------------------------------- rule classification


def classify_rule(
    manifest: Mapping[str, Any] | None,
    *,
    manifest_relative_path: str | None,
    source: str,
    started_at: datetime | None,
    run_id: str,
) -> dict[str, Any]:
    """Which collection rule a run was captured under, and how we know.

    ``attribution`` separates a recorded fact from a reconstruction:

      declared  the manifest carries collection_rule_version itself
      inferred  the manifest predates the stamp, but its format, its time and
                its location identify it as a run of the current collector
      legacy    the evidence is a legacy daily_raw directory, which is V0
      unknown   the evidence does not identify a rule; nothing is assumed
    """
    stamp = stamp_from_manifest(manifest or {})
    if stamp is not None:
        known = rule_for_version(stamp["version"])
        return {
            "version": stamp["version"],
            "attribution": "declared",
            "digest": stamp["digest"],
            "schema_version": stamp["schema_version"],
            "known_version": known is not None,
            "digest_matches_registry": (
                # A manifest written before the digest definition changed
                # carries the earlier value. That is not a mismatch: the rule
                # content is the same, so the earlier digest still identifies
                # it. Reporting it as a mismatch would flag real runs as
                # tampered with.
                None
                if known is None or stamp["digest"] is None
                else digest_is_recognised(stamp["version"], stamp["digest"])
            ),
            "evidence": ["manifest.collection_rule_version"],
        }

    evidence: list[str] = []
    if manifest is None:
        # A run with no manifest at all: judged by where it lives and what it
        # is named, which is all the evidence there is.
        if source in COLLECTOR_SOURCES and _V1_RUN_ID.match(run_id):
            evidence.extend(["raw_run_directory_layout", "run_id_format"])
    else:
        if manifest.get("capture_profile") in active_rule().capture_profiles:
            evidence.append("manifest.capture_profile")
        if manifest_relative_path and manifest_relative_path.startswith(f"manifests/{source}/"):
            evidence.append("manifest_location")
        if _V1_RUN_ID.match(run_id):
            evidence.append("run_id_format")
        if isinstance(manifest.get("schema_version"), int):
            evidence.append("manifest.schema_version")

    effective_start = parse_iso_date(active_rule().effective.start or "")
    if started_at is not None and effective_start is not None:
        observed = started_at.astimezone(KST).date()
        if observed >= effective_start:
            evidence.append("started_at_within_active_period")

    strong = {"manifest.capture_profile", "raw_run_directory_layout"}
    supporting = {"manifest_location", "run_id_format", "started_at_within_active_period"}
    confident = bool(strong & set(evidence)) or len(supporting & set(evidence)) >= 2
    if confident:
        return {
            "version": ACTIVE_RULE_VERSION,
            "attribution": "inferred",
            "digest": None,
            "schema_version": None,
            "known_version": True,
            "digest_matches_registry": None,
            "evidence": sorted(set(evidence)),
        }
    return {
        "version": None,
        "attribution": "unknown",
        "digest": None,
        "schema_version": None,
        "known_version": False,
        "digest_matches_registry": None,
        "evidence": sorted(set(evidence)),
    }


# --------------------------------------------------------------- manifests


def _kind_counts(entries: Any, *, limit: int = 8) -> tuple[int, list[dict[str, Any]]]:
    """Totals and the top kinds only. Never the entries' own details."""
    if not isinstance(entries, list):
        return 0, []
    counts: dict[str, int] = {}
    for entry in entries:
        kind: Any = None
        if isinstance(entry, Mapping):
            # Skips and errors carry `kind`; truncation entries carry `reason`.
            kind = entry.get("kind") or entry.get("reason")
        name = kind if isinstance(kind, str) and kind else "unspecified"
        counts[name] = counts.get(name, 0) + 1
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ranked = ordered[:limit]
    rows = [{"kind": kind, "count": count} for kind, count in ranked]
    if len(ordered) > len(ranked):
        # Mark the cap so a caller aggregating several runs cannot present a
        # capped breakdown as the whole story.
        rows.append({"kind": "__truncated__", "count": len(ordered) - len(ranked)})
    return len(entries), rows


def _window(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """What a run actually observed, not when it ran.

    A bounded run declares `requested_window.until`, and that is where its
    observation stops -- taking `end` from `finished_at` instead would say a
    date slice observed everything from its window up to the wall clock. The
    coverage grid attributes a run to every date its window touches, so that
    error made one 8/19 slice count toward 8/20 through today, letting a run
    that never looked at a date decide that date's verdict. An unbounded
    incremental run has no `until` and does end when it finished.
    """
    requested = manifest.get("requested_window")
    since = None
    until = None
    if isinstance(requested, Mapping):
        since = requested.get("since_effective") or requested.get("since")
        until = requested.get("until")
    start = parse_instant(since) or parse_instant(manifest.get("started_at"))
    end = (
        parse_instant(until)
        or parse_instant(manifest.get("finished_at"))
        or parse_instant(manifest.get("started_at"))
    )
    return {
        "start": start.isoformat() if start else None,
        "end": end.isoformat() if end else None,
        "from_requested_window": since is not None,
        # True when the run declared where its observation stops, so a consumer
        # can tell a real boundary from "it ended when the process ended".
        "end_is_declared": until is not None,
    }


def summarize_manifest_payload(
    payload: Any, *, relative_path: str, source: str, environment: str, run_id: str
) -> dict[str, Any]:
    """A manifest reduced to counts and identifiers. Never raises."""
    base: dict[str, Any] = {
        "source": source,
        "ledger_source": COLLECTOR_TO_SOURCE.get(source, source),
        "environment": environment,
        "run_id": run_id,
        "manifest_path": relative_path,
        "malformed": False,
        "malformed_reason": None,
    }
    if not isinstance(payload, Mapping):
        base.update(
            _malformed(source, run_id, "manifest is not a JSON object", last_activity_at=None)
        )
        return base
    manifest_run_id = payload.get("run_id")
    if isinstance(manifest_run_id, str) and manifest_run_id and manifest_run_id != run_id:
        base["manifest_run_id"] = manifest_run_id
    status = payload.get("status")
    files = payload.get("files")
    skips_total, skip_kinds = _kind_counts(payload.get("skips"))
    errors_total, error_kinds = _kind_counts(payload.get("errors"))
    truncation_total, truncation_kinds = _kind_counts(payload.get("truncation"))
    started_at = parse_instant(payload.get("started_at"))
    finished_at = parse_instant(payload.get("finished_at"))
    coverage_notes = payload.get("coverage_notes")
    base.update(
        {
            "state": MANIFEST_STATE.get(str(status), "unknown"),
            "manifest_status": status if isinstance(status, str) else None,
            "schema_version": payload.get("schema_version"),
            "capture_profile": payload.get("capture_profile"),
            "capture_density": payload.get("capture_density"),
            "dry_run": payload.get("dry_run"),
            "started_at": started_at.isoformat() if started_at else None,
            "finished_at": finished_at.isoformat() if finished_at else None,
            "last_activity_at": (finished_at or started_at).isoformat()
            if (finished_at or started_at)
            else None,
            "checkpoint_advanced": payload.get("checkpoint_advanced"),
            "checkpoint_in_present": bool(payload.get("checkpoint_in")),
            "raw_file_count": len(files) if isinstance(files, list) else None,
            "raw_bytes": (
                sum(
                    int(entry.get("compressed_bytes") or 0)
                    for entry in files
                    if isinstance(entry, Mapping)
                )
                if isinstance(files, list)
                else None
            ),
            "raw_from_manifest": isinstance(files, list),
            "pages_archived": payload.get("pages_archived"),
            "rate_limit_hits": payload.get("rate_limit_hits"),
            "truncated": payload.get("truncated"),
            "truncation_total": truncation_total,
            "truncation_kinds": truncation_kinds,
            "skips_total": skips_total,
            "skip_kinds": skip_kinds,
            "errors_total": errors_total,
            "error_kinds": error_kinds,
            "coverage_note_keys": _coverage_note_keys(coverage_notes),
            "window": _window(payload),
            "rule": classify_rule(
                payload,
                manifest_relative_path=relative_path,
                source=source,
                started_at=started_at,
                run_id=run_id,
            ),
        }
    )
    if base["state"] == "unknown":
        base["malformed_reason"] = (
            f"manifest status {status!r} is not one of {sorted(MANIFEST_STATE)}"
        )
    return base


def _malformed(
    source: str, run_id: str, reason: str, *, last_activity_at: str | None
) -> dict[str, Any]:
    """A manifest that cannot be trusted, anchored in time anyway.

    A quarantined manifest still has to appear on the right date: a reader who
    cannot see it would read that date as 미수집, which is exactly the wrong
    conclusion. The run id and the file's own mtime are the only time evidence
    left, so they are used and the run stays `malformed`.
    """
    started_at = _run_id_instant(run_id)
    return {
        "state": "malformed",
        "malformed": True,
        "malformed_reason": reason,
        "started_at": started_at,
        "finished_at": None,
        "last_activity_at": last_activity_at or started_at,
        "rule": classify_rule(
            None,
            manifest_relative_path=None,
            source=source,
            started_at=parse_instant(started_at),
            run_id=run_id,
        ),
    }


def _coverage_note_keys(notes: Any, *, limit: int = 24) -> list[str]:
    """The stable key of each coverage note, without its prose."""
    if not isinstance(notes, list):
        return []
    keys: list[str] = []
    for note in notes:
        if not isinstance(note, str):
            continue
        key = note.split(":", 1)[0].strip()
        if key and key not in keys:
            keys.append(key)
    return keys[:limit]


def load_manifest_summary(
    path: Path, *, source: str, environment: str, run_id: str, relative_path: str
) -> dict[str, Any]:
    """Cached by (path, mtime, size): the archive never rewrites a manifest."""

    try:
        stat = path.stat()
        key = (str(path), stat.st_mtime_ns, stat.st_size)
    except OSError as error:
        return {
            "source": source,
            "ledger_source": COLLECTOR_TO_SOURCE.get(source, source),
            "environment": environment,
            "run_id": run_id,
            "manifest_path": relative_path,
            **_malformed(
                source,
                run_id,
                f"manifest is unreadable ({type(error).__name__})",
                last_activity_at=None,
            ),
        }

    def build() -> dict[str, Any]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            # A partially written or truncated manifest lands here and is
            # quarantined: it is listed, never counted as a success.
            return {
                "source": source,
                "ledger_source": COLLECTOR_TO_SOURCE.get(source, source),
                "environment": environment,
                "run_id": run_id,
                "manifest_path": relative_path,
                **_malformed(
                    source,
                    run_id,
                    f"manifest could not be parsed ({type(error).__name__})",
                    last_activity_at=datetime.fromtimestamp(
                        stat.st_mtime, tz=timezone.utc
                    ).isoformat(),
                ),
            }
        return summarize_manifest_payload(
            payload,
            relative_path=relative_path,
            source=source,
            environment=environment,
            run_id=run_id,
        )

    return dict(_MANIFEST_CACHE.get_or_set(key, build))


# ------------------------------------------------------------ raw archive


def scan_raw_run(path: Path) -> dict[str, Any]:
    """File count, bytes and newest mtime of one raw run directory.

    Metadata only: no archived page is ever opened. The entry cap keeps a run
    that has written hundreds of thousands of pages from turning a page
    refresh into an unbounded walk, and says so rather than under-reporting
    silently.
    """

    def build() -> dict[str, Any]:
        count = 0
        total = 0
        newest: float | None = None
        truncated = False
        try:
            with os.scandir(path) as entries:
                for entry in entries:
                    if count >= MAX_RAW_ENTRIES_SCANNED:
                        truncated = True
                        break
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    try:
                        stat = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    count += 1
                    total += stat.st_size
                    if newest is None or stat.st_mtime > newest:
                        newest = stat.st_mtime
        except OSError as error:
            return {
                "file_count": None,
                "bytes": None,
                "last_mtime": None,
                "scan_truncated": False,
                "error": type(error).__name__,
            }
        return {
            "file_count": count,
            "bytes": total,
            "last_mtime": (
                datetime.fromtimestamp(newest, tz=timezone.utc).isoformat() if newest else None
            ),
            "scan_truncated": truncated,
            "error": None,
        }

    return dict(_RAW_SCAN_CACHE.get_or_set(str(path), build))


def _recent_day_dirs(source_root: Path, *, root: Path, limit: int) -> list[Path]:
    """Newest <YYYY>/<MM>/<DD> directories under one raw source/environment.

    Only recent days are walked. A run that has been unfinished for longer
    than this is long past stale, and a full walk of every day the archive has
    ever held is not something an HTTP request should do.
    """
    found: list[Path] = []
    for year in sorted(_dir_names(source_root), reverse=True):
        year_dir = safe_child(source_root, year, root=root)
        if year_dir is None:
            continue
        for month in sorted(_dir_names(year_dir), reverse=True):
            month_dir = safe_child(year_dir, month, root=root)
            if month_dir is None:
                continue
            for day in sorted(_dir_names(month_dir), reverse=True):
                day_dir = safe_child(month_dir, day, root=root)
                if day_dir is None:
                    continue
                found.append(day_dir)
                if len(found) >= limit:
                    return found
    return found


# ------------------------------------------------------------------ ledger


def count_ledger_records(paths: CollectionPaths, ledger_source: str, run_id: str) -> dict[str, Any]:
    """Records in this run's ledger JSONL, or a stated reason there are none."""
    directory = paths.ledger_root / "ledger"
    child = safe_child(directory, ledger_source, root=paths.ledger_root)
    if child is None:
        return {"records": None, "path": None, "reason": "unsupported ledger source"}
    target = child / f"live-{run_id}.jsonl"
    if not is_safe_name(f"live-{run_id}.jsonl") or not within(target, paths.ledger_root):
        return {"records": None, "path": None, "reason": "unsupported ledger path"}
    try:
        stat = target.stat()
    except OSError:
        return {"records": None, "path": None, "reason": "no ledger file for this run"}
    if stat.st_size > MAX_LEDGER_COUNT_BYTES:
        return {
            "records": None,
            "path": str(target),
            "bytes": stat.st_size,
            "reason": "ledger file is too large to count per request",
        }

    def build() -> dict[str, Any]:
        lines = 0
        try:
            with open(target, "rb") as stream:
                for chunk in iter(lambda: stream.read(4 << 20), b""):
                    lines += chunk.count(b"\n")
        except OSError as error:
            return {"records": None, "path": str(target), "reason": type(error).__name__}
        return {"records": lines, "path": str(target), "bytes": stat.st_size, "reason": None}

    return dict(_LEDGER_CACHE.get_or_set((str(target), stat.st_mtime_ns, stat.st_size), build))


# ------------------------------------------------------------ run discovery


@dataclass
class RunIndex:
    runs: dict[tuple[str, str, str], dict[str, Any]] = field(default_factory=dict)
    environments: dict[str, set[str]] = field(default_factory=dict)
    manifest_errors: list[dict[str, Any]] = field(default_factory=list)

    def upsert(self, source: str, environment: str, run_id: str, values: Mapping[str, Any]) -> None:
        key = (source, environment, run_id)
        record = self.runs.setdefault(
            key,
            {
                "source": source,
                "ledger_source": COLLECTOR_TO_SOURCE.get(source, source),
                "environment": environment,
                "run_id": run_id,
                "state": "unknown",
                "manifest_path": None,
                "manifest_revisions": 0,
            },
        )
        record.update(values)
        self.environments.setdefault(source, set()).add(environment)


def _manifest_entries(directory: Path, root: Path) -> dict[str, list[str]]:
    """run_id -> manifest file names, newest revision last."""
    grouped: dict[str, list[str]] = {}
    for name in _file_names(directory):
        if name in NON_MANIFEST_NAMES:
            continue
        match = _MANIFEST_NAME.match(name)
        if match is None:
            continue
        stem = match.group("run_id")
        revision = _MANIFEST_REVISION.match(stem)
        run_id = revision.group("run_id") if revision else stem
        if not is_safe_name(run_id):
            continue
        if safe_child(directory, name, root=root) is None:
            continue
        grouped.setdefault(run_id, []).append(name)
    for names in grouped.values():
        names.sort(key=lambda value: (len(value), value))
    return grouped


def build_run_index(
    paths: CollectionPaths,
    *,
    sources: Iterable[str] | None = None,
    environment: str | None = None,
    include_active: bool = True,
    stale_after_seconds: int = DEFAULT_STALE_AFTER_SECONDS,
    now: datetime | None = None,
) -> RunIndex:
    """Every run this archive can see, from manifests, raw dirs and snapshots."""
    moment = now or datetime.now(timezone.utc)
    wanted = tuple(sources) if sources is not None else COLLECTOR_SOURCES
    index = RunIndex()

    for source in wanted:
        if source not in COLLECTOR_SOURCES:
            continue
        manifest_source = safe_child(paths.manifest_root, source, root=paths.archive_root)
        if manifest_source is not None:
            for env in _dir_names(manifest_source):
                if environment is not None and env != environment:
                    index.environments.setdefault(source, set()).add(env)
                    continue
                env_dir = safe_child(manifest_source, env, root=paths.archive_root)
                if env_dir is None:
                    continue
                index.environments.setdefault(source, set()).add(env)
                for run_id, names in _manifest_entries(env_dir, paths.archive_root).items():
                    latest = names[-1]
                    path = env_dir / latest
                    summary = load_manifest_summary(
                        path,
                        source=source,
                        environment=env,
                        run_id=run_id,
                        relative_path=paths.relative(path),
                    )
                    summary["manifest_revisions"] = len(names)
                    summary["manifest_names"] = names
                    index.upsert(source, env, run_id, summary)
                    if summary.get("malformed"):
                        index.manifest_errors.append(
                            {
                                "source": source,
                                "environment": env,
                                "run_id": run_id,
                                "manifest_path": summary.get("manifest_path"),
                                "reason": summary.get("malformed_reason"),
                            }
                        )

        if not include_active:
            continue
        raw_source = safe_child(paths.raw_root, source, root=paths.archive_root)
        if raw_source is None:
            continue
        for env in _dir_names(raw_source):
            if environment is not None and env != environment:
                index.environments.setdefault(source, set()).add(env)
                continue
            env_dir = safe_child(raw_source, env, root=paths.archive_root)
            if env_dir is None:
                continue
            index.environments.setdefault(source, set()).add(env)
            for day_dir in _recent_day_dirs(
                env_dir, root=paths.archive_root, limit=MAX_ACTIVE_DAY_DIRS
            ):
                for run_id in _dir_names(day_dir):
                    run_dir = safe_child(day_dir, run_id, root=paths.archive_root)
                    if run_dir is None:
                        continue
                    key = (source, env, run_id)
                    if key in index.runs:
                        # The run finished: its manifest is the authority and
                        # already carries the file list, so the directory is
                        # not walked at all.
                        index.runs[key].setdefault("raw_run_dir", paths.relative(run_dir))
                        continue
                    scan = scan_raw_run(run_dir)
                    last_mtime = parse_instant(scan.get("last_mtime"))
                    age = (moment - last_mtime).total_seconds() if last_mtime else None
                    state = "running"
                    reason = None
                    if age is None:
                        state = "unknown"
                        reason = "the run directory has no readable modification time"
                    elif age > stale_after_seconds:
                        state = "stale"
                        reason = (
                            f"no raw page written for {int(age)}s and no manifest exists"
                        )
                    index.upsert(
                        source,
                        env,
                        run_id,
                        {
                            "state": state,
                            "state_reason": reason,
                            "manifest_path": None,
                            "manifest_status": None,
                            "raw_run_dir": paths.relative(run_dir),
                            "raw_file_count": scan.get("file_count"),
                            "raw_bytes": scan.get("bytes"),
                            "raw_last_mtime": scan.get("last_mtime"),
                            "raw_scan_truncated": scan.get("scan_truncated", False),
                            "raw_from_manifest": False,
                            "last_activity_at": scan.get("last_mtime"),
                            "started_at": _run_id_instant(run_id),
                            "rule": classify_rule(
                                None,
                                manifest_relative_path=None,
                                source=source,
                                started_at=parse_instant(_run_id_instant(run_id)),
                                run_id=run_id,
                            ),
                        },
                    )

    _merge_snapshots(
        index,
        paths,
        sources=wanted,
        environment=environment,
        stale_after_seconds=stale_after_seconds,
        now=moment,
    )
    return index


def _run_id_instant(run_id: str) -> str | None:
    """The UTC instant encoded in a <YYYYMMDD>T<HHMMSS>Z-<hex> run id."""
    if not _V1_RUN_ID.match(run_id):
        return None
    try:
        parsed = datetime.strptime(run_id.split("-", 1)[0], "%Y%m%dT%H%M%SZ")
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc).isoformat()


def _merge_snapshots(
    index: RunIndex,
    paths: CollectionPaths,
    *,
    sources: Iterable[str],
    environment: str | None,
    stale_after_seconds: int,
    now: datetime,
) -> None:
    root = progress_root(paths.config_root)
    if not within(root, paths.config_root):
        return
    wanted = set(sources)
    for snapshot in iter_snapshots(root, environment=environment):
        source = str(snapshot.get("source") or "")
        env = str(snapshot.get("environment") or "")
        run_id = str(snapshot.get("run_id") or "")
        if source not in wanted or not env or not run_id:
            continue
        liveness = snapshot_liveness(
            snapshot, now=now, stale_after_seconds=stale_after_seconds
        )
        key = (source, env, run_id)
        existing = index.runs.get(key)
        values: dict[str, Any] = {
            "progress": {
                "phase": snapshot.get("phase"),
                "status": snapshot.get("status"),
                "updated_at": snapshot.get("updated_at"),
                "files_written": snapshot.get("files_written"),
                "bytes_written": snapshot.get("bytes_written"),
                "liveness": liveness,
                "host_known": snapshot.get("host") is not None,
            }
        }
        ledger = snapshot.get("ledger")
        if isinstance(ledger, Mapping):
            values["ledger_records"] = ledger.get("records_written")
            values["ledger_schema_errors"] = ledger.get("schema_errors")
            values["ledger_from_snapshot"] = True
        if existing is None or existing.get("manifest_path") is None:
            # No manifest: the snapshot is the better evidence of what this
            # run is doing right now.
            if liveness["state"] in {"running", "stale"}:
                values["state"] = liveness["state"]
                values["state_reason"] = liveness["reason"]
            values.setdefault("started_at", snapshot.get("started_at"))
            last = snapshot.get("updated_at")
            if existing is None or not existing.get("last_activity_at"):
                values["last_activity_at"] = last
            if existing is None:
                values.update(
                    {
                        "raw_run_dir": snapshot.get("raw_run_dir"),
                        "raw_file_count": snapshot.get("files_written"),
                        "raw_bytes": snapshot.get("bytes_written"),
                        "raw_from_manifest": False,
                        "capture_density": snapshot.get("capture_density"),
                        "dry_run": snapshot.get("dry_run"),
                        "rule": classify_rule(
                            snapshot,
                            manifest_relative_path=None,
                            source=source,
                            started_at=parse_instant(snapshot.get("started_at")),
                            run_id=run_id,
                        ),
                    }
                )
        index.upsert(source, env, run_id, values)


# ------------------------------------------------------------ legacy (V0)

# Legacy source directory names, mapped onto canonical ledger sources. Names
# outside this map (github, slurm, gdrive) are counted but not attributed to a
# collection source, because this registry only covers the three sources.
LEGACY_SOURCE_DIRS = {"slack": "slack", "notion": "notion", "gcal": "google_calendar"}


def legacy_inventory(paths: CollectionPaths) -> dict[str, Any]:
    """Date-level index of the legacy dumps, built from directory names only.

    This never opens a payload file and never walks below
    ``<root>/daily_raw/<date>/<source>``, so it is a metadata index rather
    than a rescan of the legacy archive. It is cached, and it stops at a
    deadline and says so instead of holding an HTTP request open.
    """

    def build() -> dict[str, Any]:
        started = time.monotonic()
        dates: dict[str, dict[str, list[str]]] = {}
        roots: list[str] = []
        complete = True
        if not within(paths.legacy_root, paths.archive_root):
            return {
                "complete": False,
                "reason": "legacy root is outside the raw archive root",
                "roots": [],
                "dates": {},
                "observed": {},
            }
        for root_name in _dir_names(paths.legacy_root):
            root_dir = safe_child(paths.legacy_root, root_name, root=paths.archive_root)
            if root_dir is None:
                continue
            daily_raw = root_dir / "daily_raw"
            if not daily_raw.is_dir():
                continue
            roots.append(root_name)
            for day in _dir_names(daily_raw):
                if not _ISO_DATE.match(day) or parse_iso_date(day) is None:
                    continue
                if time.monotonic() - started > LEGACY_INVENTORY_MAX_SECONDS:
                    complete = False
                    break
                day_dir = safe_child(daily_raw, day, root=paths.archive_root)
                if day_dir is None:
                    continue
                bucket = dates.setdefault(day, {})
                for source_dir in _dir_names(day_dir):
                    canonical = LEGACY_SOURCE_DIRS.get(source_dir)
                    if canonical is None:
                        continue
                    bucket.setdefault(canonical, []).append(f"{root_name}/{source_dir}")
            if not complete:
                break
        observed: dict[str, dict[str, Any]] = {}
        for day, sources in dates.items():
            for canonical in sources:
                entry = observed.setdefault(
                    canonical, {"first": day, "last": day, "dates": 0}
                )
                entry["first"] = min(entry["first"], day)
                entry["last"] = max(entry["last"], day)
                entry["dates"] += 1
        return {
            "complete": complete,
            "reason": None if complete else "the inventory scan reached its time budget",
            "roots": sorted(roots),
            "dates": dates,
            "observed": observed,
        }

    return _LEGACY_INVENTORY_CACHE.get_or_set(str(paths.legacy_root), build)


def legacy_source_dir(paths: CollectionPaths, *, day: str, entry: str) -> Path | None:
    """``<legacy>/<root>/daily_raw/<day>/<source>`` for one inventory entry.

    ``entry`` is a ``<root>/<source dir>`` pair taken from the inventory, so
    both halves came from the filesystem; each segment is still re-checked
    before it is joined.
    """
    root_name, _, source_dir = entry.partition("/")
    directory = paths.legacy_root
    for segment in (root_name, "daily_raw", day, source_dir):
        child = safe_child(directory, segment, root=paths.archive_root)
        if child is None:
            return None
        directory = child
    return directory


def legacy_meta(paths: CollectionPaths, *, day: str, entry: str) -> dict[str, Any] | None:
    """The facts a legacy meta.json records, or None when there is none.

    ``status`` is read but reported as not-evidence: the legacy collector
    hardcoded it to "ok" on every date, including dates whose own
    ``truncation_warnings`` show loss.
    """
    directory = legacy_source_dir(paths, day=day, entry=entry)
    if directory is None:
        return None
    target = directory / "meta.json"
    if not within(target, paths.archive_root):
        return None
    try:
        stat = target.stat()
    except OSError:
        return None

    def build() -> dict[str, Any] | None:
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"readable": False, "reason": "meta.json could not be parsed"}
        if not isinstance(payload, Mapping):
            return {"readable": False, "reason": "meta.json is not an object"}
        api_calls = payload.get("api_calls")
        warnings = payload.get("truncation_warnings")
        meta = payload.get("_meta")
        return {
            "readable": True,
            "date": day,
            "declared_status": payload.get("status"),
            "declared_status_is_evidence": False,
            "collected_at": payload.get("collection_time")
            or payload.get("collected_at")
            or (meta.get("collected_at") if isinstance(meta, Mapping) else None),
            "api_calls_total": (
                api_calls.get("total") if isinstance(api_calls, Mapping) else None
            ),
            "rate_limit_hits": payload.get("rate_limit_hits"),
            "truncation_warnings": len(warnings) if isinstance(warnings, list) else None,
            "source_schema_version": (
                meta.get("source_schema_version") if isinstance(meta, Mapping) else None
            ),
        }

    return _LEGACY_META_CACHE.get_or_set((str(target), stat.st_mtime_ns, stat.st_size), build)


# ---------------------------------------------------------------- coverage

# Cell classifications. `not_collected` and `unknown` are deliberately
# distinct: the first means no evidence exists, the second means evidence
# exists but does not say whether the date was collected.
COVERAGE_COLLECTED = "collected"
COVERAGE_PARTIAL = "partial"
COVERAGE_RUNNING = "running"
COVERAGE_FAILED = "failed"
COVERAGE_NOT_COLLECTED = "not_collected"
COVERAGE_UNKNOWN = "unknown"
# A run that finished and named what it could not reach. Honest reporting is
# not a defect, so it is kept apart from `partial`, which means the run does
# not know what it missed.
COVERAGE_COLLECTED_WITH_SKIPS = "collected_with_skips"
# A V0 legacy date. A directory exists, and that is all it proves: the legacy
# dumps carry no run identity and their meta.json `status` is hardcoded, so
# nothing about them can assert completeness. Distinct from `unknown`, which
# means evidence exists but could not be parsed.
COVERAGE_UNVERIFIED = "unverified"
# A V0 legacy date whose meta.json was never opened, because the requested
# range was wider than `MAX_LEGACY_META_PROBE_DAYS`. Distinct from
# `unverified`: that one means the record was read and proves nothing, this one
# means nobody looked. Collapsing the two would let a query's own width change
# a date's verdict without saying so -- the same date reads `partial` in a
# 30-day window and `unverified` in a 276-day one.
COVERAGE_UNEXAMINED = "unexamined"

# How strong the evidence behind a cell is. Its purpose is to make it
# impossible for a UI to paint a directory listing and a run manifest on the
# same badge scale.
EVIDENCE_MANIFEST = "manifest"
EVIDENCE_DIRECTORY_ONLY = "directory_only"
EVIDENCE_MIXED = "mixed"

_INCOMPLETE_STATES = {"degraded", "failed"}
_ACTIVE_STATES = {"running", "stale"}


# Time-coverage of a date, kept apart from observation quality. A run that
# merely touched a date says nothing about whether the whole date was seen.
TIME_COVERAGE_COMPLETE = "complete"
TIME_COVERAGE_PARTIAL = "partial"
TIME_COVERAGE_IN_PROGRESS = "in_progress"


def _run_window_end(run: Mapping[str, Any]) -> datetime | None:
    """How far into time this run actually observed."""
    window = run.get("window") or {}
    return (
        parse_instant(window.get("end"))
        or parse_instant(run.get("last_activity_at"))
        or parse_instant(run.get("started_at"))
    )


def _time_coverage(
    observed_through: datetime | None, day_end: datetime, now: datetime
) -> str:
    """Whether the whole of a KST date has been observed yet.

    A date still in progress can never be complete, however clean the runs
    that touched it are: the hours that have not happened cannot have been
    collected. This is the same rule as the legacy and skips fixes -- a badge
    may not claim more than the evidence supports.
    """
    if now < day_end:
        return TIME_COVERAGE_IN_PROGRESS
    if observed_through is not None and observed_through >= day_end:
        return TIME_COVERAGE_COMPLETE
    return TIME_COVERAGE_PARTIAL


def _run_intersects_day(run: Mapping[str, Any], start: datetime, end: datetime) -> bool:
    """Does this run's observation window intersect [start, end)?

    Intersection is the right rule for *attribution* -- it means this run has
    something to say about this date. It is not a completeness claim; that is
    `_time_coverage`'s job.
    """
    window = run.get("window") or {}
    window_start = parse_instant(window.get("start")) or parse_instant(run.get("started_at"))
    window_end = (
        parse_instant(window.get("end"))
        or parse_instant(run.get("last_activity_at"))
        or parse_instant(run.get("started_at"))
    )
    if window_start is None and window_end is None:
        return False
    if window_start is None:
        window_start = window_end
    if window_end is None or window_end < window_start:
        window_end = window_start
    if (window_end - window_start) > timedelta(days=MAX_COVERAGE_DAYS_PER_RUN):
        window_start = window_end - timedelta(days=MAX_COVERAGE_DAYS_PER_RUN)
    # A declared end is the collector's exclusive upper bound: a slice with
    # `until` at 8/20 00:00 KST collected nothing at that instant, so it has
    # nothing to say about 8/20. An undeclared end is just when the process
    # stopped, and that instant was inside the observation.
    if window.get("end_is_declared"):
        return window_start < end and window_end > start
    return window_start < end and window_end >= start


def _run_evidence_class(runs: list[Mapping[str, Any]]) -> str:
    """Which grade of evidence backs these runs.

    A run with a manifest is a recorded observation. A run without one is a
    directory being written (or abandoned), which proves only that something
    ran. The two are never merged into one claim.
    """
    with_manifest = any(run.get("manifest_path") for run in runs)
    directory_only = any(not run.get("manifest_path") for run in runs)
    if with_manifest and directory_only:
        return EVIDENCE_MIXED
    return EVIDENCE_MANIFEST if with_manifest else EVIDENCE_DIRECTORY_ONLY


def _cell_from_runs(runs: list[Mapping[str, Any]]) -> dict[str, Any]:
    states = [str(run.get("state")) for run in runs]
    rule_counts: dict[tuple[str | None, str], int] = {}
    for run in runs:
        rule = run.get("rule") or {}
        key = (rule.get("version"), str(rule.get("attribution") or "unknown"))
        rule_counts[key] = rule_counts.get(key, 0) + 1
    # `degraded` is a stronger failure signal than `success_with_skips`
    # (docs/daily-collection.md): a degraded run does not know what it missed,
    # while a run with skips named every one of them. Only the former, and a
    # truncated run, make a date incomplete.
    truncated = any(bool(run.get("truncated")) for run in runs)
    incomplete = any(state in _INCOMPLETE_STATES for state in states) or truncated
    skipped_only = "success_with_skips" in states and not incomplete
    settled = [state for state in states if state in {"success", "success_with_skips"}]
    if "running" in states:
        coverage = COVERAGE_RUNNING
    elif all(state == "malformed" for state in states):
        coverage = COVERAGE_UNKNOWN
    elif states and all(state == "failed" for state in states):
        coverage = COVERAGE_FAILED
    elif "stale" in states and not settled:
        # A crashed capture wrote raw pages and no manifest. It is evidence
        # that something ran, and no evidence at all about what it covered.
        coverage = COVERAGE_UNKNOWN
    elif (
        incomplete
        or "stale" in states
        or any(state in {"malformed", "unknown"} for state in states)
    ):
        coverage = COVERAGE_PARTIAL
    elif skipped_only:
        coverage = COVERAGE_COLLECTED_WITH_SKIPS
    else:
        coverage = COVERAGE_COLLECTED
    completeness = "unknown"
    if coverage == COVERAGE_COLLECTED:
        completeness = "complete"
    elif coverage == COVERAGE_COLLECTED_WITH_SKIPS:
        # Everything the run set out to read, minus what it explicitly named.
        completeness = "complete_with_known_gaps"
    elif coverage in {COVERAGE_PARTIAL, COVERAGE_FAILED}:
        completeness = "incomplete"
    notes: list[str] = []
    skips_total = sum(int(run.get("skips_total") or 0) for run in runs)
    if skips_total:
        kinds: dict[str, int] = {}
        capped = 0
        for run in runs:
            for entry in run.get("skip_kinds") or []:
                name = str(entry.get("kind"))
                if name == "__truncated__":
                    capped += int(entry.get("count") or 0)
                    continue
                kinds[name] = kinds.get(name, 0) + int(entry.get("count") or 0)
        ordered = sorted(kinds.items(), key=lambda item: (-item[1], item[0]))
        shown = ordered[:6]
        ranked = ", ".join(f"{kind} {count}" for kind, count in shown)
        # The per-run list is already capped upstream, so the merged breakdown
        # can be a subset. Say so rather than letting it read as exhaustive.
        remainder = len(ordered) - len(shown)
        if remainder > 0:
            ranked += f", 외 {remainder}종"
        elif capped:
            ranked += f", 외 {capped}종"
        notes.append(
            f"이 날짜의 실행이 건너뛴 항목 {skips_total}건" + (f" ({ranked})" if ranked else "")
        )
    if "stale" in states:
        notes.append(
            "a run of this date wrote raw pages and no manifest; what it covered is unknown"
        )
    if any(state == "malformed" for state in states):
        notes.append("a manifest for this date could not be parsed and is quarantined")
    last = max(
        runs,
        key=lambda run: str(run.get("last_activity_at") or run.get("started_at") or ""),
    )
    return {
        "coverage": coverage,
        "runs": len(runs),
        "runs_known": True,
        "last_status": last.get("manifest_status") or last.get("state"),
        "last_run_id": last.get("run_id"),
        "completeness": completeness,
        "rule_versions": [
            {"version": version, "attribution": attribution, "count": count}
            for (version, attribution), count in sorted(
                rule_counts.items(), key=lambda item: (str(item[0][0]), item[0][1])
            )
        ],
        "evidence": sorted(
            {
                str(run.get("manifest_path") or run.get("raw_run_dir"))
                for run in runs
                if run.get("manifest_path") or run.get("raw_run_dir")
            }
        )[:12],
        "states": sorted(set(states)),
        # A finished run is evidenced by its manifest; a running or crashed one
        # is evidenced only by the raw directory it is writing. Calling the
        # latter `manifest` was the same over-claim this module exists to
        # prevent, one grade up.
        "evidence_class": _run_evidence_class(runs),
        # Not applicable rather than False: this cell rests on run manifests, so
        # no legacy meta.json was relevant to read. False would say the probe
        # was skipped and something is therefore unknown, which is not true here.
        "legacy_meta_probed": None,
        "notes": notes,
    }


def _apply_time_coverage(
    cell: dict[str, Any],
    runs: list[Mapping[str, Any]],
    *,
    day_end: datetime,
    now: datetime,
) -> None:
    """Add the time axis to a run-backed cell and gate its completeness.

    Observation quality (`coverage`) and time coverage are separate questions.
    A run can be flawless and still have seen only half a day. `completeness`
    is the place the two meet, so it may never say `complete` for a date whose
    remaining hours nobody has observed -- including every date still running.
    """
    ends = [end for end in (_run_window_end(run) for run in runs) if end is not None]
    observed_through = max(ends) if ends else None
    time_coverage = _time_coverage(observed_through, day_end, now)
    cell["observed_through"] = observed_through.isoformat() if observed_through else None
    cell["time_coverage"] = time_coverage

    if cell["coverage"] not in {COVERAGE_COLLECTED, COVERAGE_COLLECTED_WITH_SKIPS}:
        return
    if time_coverage == TIME_COVERAGE_COMPLETE:
        return
    if time_coverage == TIME_COVERAGE_IN_PROGRESS:
        cell["completeness"] = "in_progress"
        cell["notes"].append(
            "이 날짜는 아직 끝나지 않았습니다. 남은 시간대는 아직 발생하지 않아 수집될 수 없습니다."
        )
    else:
        cell["completeness"] = "incomplete"
        cell["notes"].append(
            "이 날짜의 뒷부분을 관측한 실행이 없습니다. "
            + (
                f"마지막 관측 {observed_through.astimezone(KST):%H:%M} KST 까지입니다."
                if observed_through
                else "관측 종료 시각을 알 수 없습니다."
            )
        )
    # search.messages is an index and lags the live channel, so a date is only
    # settled once a run has read past its end.
    cell["notes"].append(
        "날짜 완결은 그 날짜가 끝난 뒤 최소 한 번의 실행을 요구합니다 "
        "(검색 인덱스 지연을 26시간 겹침 창이 덮습니다)."
    )


def _legacy_evidence(
    paths: CollectionPaths, *, day: str, entries: list[str], limit: int = 12
) -> list[str]:
    """Local identifiers for the legacy directories behind one date."""
    found: list[str] = []
    for entry in entries[:limit]:
        directory = legacy_source_dir(paths, day=day, entry=entry)
        if directory is not None:
            found.append(paths.relative(directory))
    return found


def _legacy_cell(
    paths: CollectionPaths, *, day: str, relative_dirs: list[str], probe_meta: bool
) -> dict[str, Any]:
    metas = []
    if probe_meta:
        for entry in relative_dirs[:4]:
            meta = legacy_meta(paths, day=day, entry=entry)
            if meta is not None:
                metas.append(meta)
    notes = [
        "V0 legacy dumps carry no run identity, so the number of runs behind this "
        "date cannot be counted.",
        "meta.json status is hardcoded and is not evidence of completeness.",
    ]
    truncation = [
        meta.get("truncation_warnings")
        for meta in metas
        if meta.get("readable") and isinstance(meta.get("truncation_warnings"), int)
    ]
    completeness = "unknown"
    # A directory proves a dump exists, not that it was complete. V0 carries no
    # run identity and its meta.json `status` is hardcoded, so there is nothing
    # here that can assert coverage -- the cell says `unverified` rather than
    # borrowing the word a V1 manifest earns.
    #
    # But `unverified` is only honest once the record has been read. When the
    # requested range was too wide to open meta.json, the truncation branch
    # below cannot be reached at all, so a date carrying a truncation warning
    # would silently read as though it carried none. That is the query changing
    # the answer, and the cell has to say so instead.
    coverage = COVERAGE_UNVERIFIED if probe_meta else COVERAGE_UNEXAMINED
    if not probe_meta:
        notes.append(
            f"조회 범위가 {MAX_LEGACY_META_PROBE_DAYS}일을 넘어 이 날짜의 레거시 "
            "meta.json 을 읽지 않았습니다. truncation 기록이 있는지 알 수 없습니다 — "
            "범위를 좁혀 다시 조회하면 확인됩니다."
        )
    elif not metas:
        notes.append(
            "레거시 meta.json 을 찾지 못했거나 읽을 수 없습니다. 디렉터리만 증거입니다."
        )
    if truncation and any(value > 0 for value in truncation):
        coverage = COVERAGE_PARTIAL
        completeness = "incomplete"
        notes.append("meta.json records truncation warnings for this date.")
    return {
        "coverage": coverage,
        "runs": None,
        "runs_known": False,
        "last_status": None,
        "last_run_id": None,
        "completeness": completeness,
        "rule_versions": [{"version": "V0", "attribution": "legacy", "count": len(relative_dirs)}],
        "evidence": _legacy_evidence(paths, day=day, entries=relative_dirs),
        "states": [],
        "evidence_class": EVIDENCE_DIRECTORY_ONLY,
        # V0 dumps have no observation window, so there is nothing to measure
        # a date's time coverage against. Left null rather than invented.
        "observed_through": None,
        "time_coverage": None,
        "legacy_meta": metas,
        # Per cell, not just per response: a weekday rollup or a filtered view
        # can carry cells from more than one probe decision, and a consumer
        # holding one cell must still be able to tell whether it was examined.
        "legacy_meta_probed": probe_meta,
        "notes": notes,
    }


def coverage(
    paths: CollectionPaths,
    *,
    start: date_type,
    end: date_type,
    sources: Iterable[str] | None = None,
    environment: str | None = None,
    group: str = "date",
    now: datetime | None = None,
    index: RunIndex | None = None,
) -> dict[str, Any]:
    """Per-KST-date (or per-weekday) collection coverage for each source.

    A V1 run is attributed to every KST date its observation window touches,
    so a run that straddles midnight KST appears on both dates. Legacy dates
    come from the V0 directory index. A date with neither is 미수집, never a
    guess.
    """
    moment = now or datetime.now(timezone.utc)
    if end < start:
        start, end = end, start
    span = (end - start).days + 1
    truncated_range = span > MAX_COVERAGE_DAYS
    if truncated_range:
        start = end - timedelta(days=MAX_COVERAGE_DAYS - 1)
        span = MAX_COVERAGE_DAYS
    wanted = [
        source
        for source in (sources if sources is not None else SOURCE_TO_COLLECTOR)
        if source in SOURCE_TO_COLLECTOR
    ]
    collector_sources = [SOURCE_TO_COLLECTOR[source] for source in wanted]
    run_index = index or build_run_index(
        paths, sources=collector_sources, environment=environment, now=moment
    )
    runs_by_source: dict[str, list[Mapping[str, Any]]] = {source: [] for source in wanted}
    for run in run_index.runs.values():
        ledger_source = str(run.get("ledger_source"))
        if ledger_source in runs_by_source:
            runs_by_source[ledger_source].append(run)

    inventory = legacy_inventory(paths)
    probe_meta = span <= MAX_LEGACY_META_PROBE_DAYS
    rows: list[dict[str, Any]] = []
    day = start
    while day <= end:
        day_start, day_end = kst_day_bounds(day)
        iso = day.isoformat()
        cells: dict[str, Any] = {}
        for source in wanted:
            matching = [
                run for run in runs_by_source[source]
                if _run_intersects_day(run, day_start, day_end)
            ]
            legacy_dirs = (inventory["dates"].get(iso) or {}).get(source) or []
            if matching:
                cell = _cell_from_runs(matching)
                _apply_time_coverage(cell, matching, day_end=day_end, now=moment)
                if legacy_dirs:
                    cell["rule_versions"].append(
                        {"version": "V0", "attribution": "legacy", "count": len(legacy_dirs)}
                    )
                    cell["evidence"] = (
                        cell["evidence"] + _legacy_evidence(paths, day=iso, entries=legacy_dirs)
                    )[:12]
                    # The V1 verdict stands; the cell records that a weaker V0
                    # source also covers this date rather than blending them.
                    cell["evidence_class"] = EVIDENCE_MIXED
                    # A legacy dump is present here, so whether its record was
                    # read is a real question about this cell -- unlike a cell
                    # with runs only, where it stays not-applicable.
                    cell["legacy_meta_probed"] = probe_meta
            elif legacy_dirs:
                cell = _legacy_cell(
                    paths, day=iso, relative_dirs=legacy_dirs, probe_meta=probe_meta
                )
            else:
                cell = {
                    "coverage": COVERAGE_NOT_COLLECTED
                    if inventory["complete"]
                    else COVERAGE_UNKNOWN,
                    "runs": 0,
                    "runs_known": inventory["complete"],
                    "last_status": None,
                    "last_run_id": None,
                    "completeness": "unknown",
                    "rule_versions": [],
                    "evidence": [],
                    "states": [],
                    "evidence_class": None,
                    "observed_through": None,
                    "time_coverage": None,
                    # No legacy dump exists for this date, so there was no
                    # meta.json to open and the probe decision is irrelevant.
                    "legacy_meta_probed": None,
                    "notes": (
                        []
                        if inventory["complete"]
                        else ["the legacy inventory is incomplete, so absence is not evidence"]
                    ),
                }
            density = None
            for entry in cell["rule_versions"]:
                rule = rule_for_version(entry["version"])
                source_rule = rule.source_rule(source) if rule else None
                if source_rule is not None:
                    density = source_rule.density_kind
                    break
            cell["density"] = density
            cell["source"] = source
            cells[source] = cell
        rows.append(
            {
                "date": iso,
                "weekday": weekday_label(day),
                "weekday_index": day.weekday(),
                "cells": cells,
            }
        )
        day += timedelta(days=1)

    payload: dict[str, Any] = {
        "generated_at": moment.isoformat(),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "timezone": "Asia/Seoul (+09:00)",
        "group": "weekday" if group == "weekday" else "date",
        "sources": wanted,
        "environment": environment,
        "environment_scope": environment or "all",
        "range_truncated": truncated_range,
        "legacy_inventory": {
            "complete": inventory["complete"],
            "reason": inventory["reason"],
            "roots": inventory["roots"],
            "observed": inventory["observed"],
        },
        "legacy_meta_probed": probe_meta,
        # The threshold and the requested span, so a reader can see for itself
        # why the probe was skipped and how far to narrow the range to get a
        # verdict rather than an `unexamined`.
        "legacy_meta_probe_limit_days": MAX_LEGACY_META_PROBE_DAYS,
        "requested_span_days": span,
        "rows": rows,
    }
    if group == "weekday":
        payload["weekday_rows"] = _weekday_rollup(rows, wanted)
    return payload


def _weekday_rollup(rows: list[dict[str, Any]], sources: list[str]) -> list[dict[str, Any]]:
    buckets: dict[int, dict[str, Any]] = {}
    for row in rows:
        bucket = buckets.setdefault(
            row["weekday_index"],
            {
                "weekday": row["weekday"],
                "weekday_index": row["weekday_index"],
                "dates": 0,
                "cells": {
                    source: {
                        "coverage_counts": {},
                        "runs": 0,
                        "runs_known": True,
                        "rule_versions": {},
                        "dates_not_collected": 0,
                        # A rollup that only counted verdicts would let the
                        # unexamined dates disappear into a total. The weekday
                        # view has to be able to say how much of itself nobody
                        # looked at.
                        "dates_unexamined": 0,
                    }
                    for source in sources
                },
            },
        )
        bucket["dates"] += 1
        for source in sources:
            cell = row["cells"][source]
            target = bucket["cells"][source]
            counts = target["coverage_counts"]
            counts[cell["coverage"]] = counts.get(cell["coverage"], 0) + 1
            if cell["coverage"] == COVERAGE_NOT_COLLECTED:
                target["dates_not_collected"] += 1
            if cell["coverage"] == COVERAGE_UNEXAMINED:
                target["dates_unexamined"] += 1
            if cell["runs_known"] and isinstance(cell["runs"], int):
                target["runs"] += cell["runs"]
            else:
                target["runs_known"] = False
            for entry in cell["rule_versions"]:
                label = f"{entry['version']}·{entry['attribution']}"
                target["rule_versions"][label] = (
                    target["rule_versions"].get(label, 0) + entry["count"]
                )
    return [buckets[index] for index in sorted(buckets)]


# ------------------------------------------------------------- public view

_RUN_FIELDS = (
    "source",
    "ledger_source",
    "environment",
    "run_id",
    "state",
    "state_reason",
    "manifest_status",
    "manifest_path",
    "manifest_revisions",
    "malformed",
    "malformed_reason",
    "schema_version",
    "capture_profile",
    "capture_density",
    "dry_run",
    "started_at",
    "finished_at",
    "last_activity_at",
    "checkpoint_advanced",
    "checkpoint_in_present",
    "raw_run_dir",
    "raw_file_count",
    "raw_bytes",
    "raw_last_mtime",
    "raw_scan_truncated",
    "raw_from_manifest",
    "pages_archived",
    "rate_limit_hits",
    "truncated",
    "truncation_total",
    "truncation_kinds",
    "skips_total",
    "skip_kinds",
    "errors_total",
    "error_kinds",
    "coverage_note_keys",
    "window",
    "rule",
    "progress",
    "ledger_records",
    "ledger_schema_errors",
    "ledger_from_snapshot",
)


def _sort_key(run: Mapping[str, Any]) -> str:
    return str(
        run.get("last_activity_at") or run.get("finished_at") or run.get("started_at") or run.get("run_id") or ""
    )


def run_view(paths: CollectionPaths, run: Mapping[str, Any], *, with_ledger: bool = True) -> dict[str, Any]:
    """One run, reduced to what the dashboard may show. No raw content."""
    view = {key: run.get(key) for key in _RUN_FIELDS}
    view.setdefault("manifest_revisions", run.get("manifest_revisions") or 0)
    if with_ledger and view.get("ledger_records") is None:
        counted = count_ledger_records(
            paths, str(run.get("ledger_source")), str(run.get("run_id"))
        )
        view["ledger_records"] = counted.get("records")
        view["ledger_path"] = paths.relative(Path(counted["path"])) if counted.get("path") else None
        view["ledger_reason"] = counted.get("reason")
    elif view.get("ledger_records") is not None:
        view["ledger_reason"] = None
    if view.get("ledger_schema_errors") is None:
        # Schema errors are produced by the ledger stage, not by the capture.
        # A run whose stage never reported them says so instead of showing 0.
        view["ledger_schema_errors_known"] = False
    else:
        view["ledger_schema_errors_known"] = True
    view["started_date_kst"] = kst_date(parse_instant(view.get("started_at")))
    view["finished_date_kst"] = kst_date(parse_instant(view.get("finished_at")))
    return view


def list_runs(
    paths: CollectionPaths,
    *,
    source: str | None = None,
    environment: str | None = None,
    limit: int = 50,
    index: RunIndex | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    sources = [source] if source else list(COLLECTOR_SOURCES)
    run_index = index or build_run_index(
        paths, sources=sources, environment=environment, now=now
    )
    ordered = sorted(run_index.runs.values(), key=_sort_key, reverse=True)
    if source:
        ordered = [run for run in ordered if run.get("source") == source]
    if environment:
        ordered = [run for run in ordered if run.get("environment") == environment]
    limited = ordered[: max(1, limit)]
    return {
        "count": len(ordered),
        "returned": len(limited),
        "items": [run_view(paths, run) for run in limited],
        "environments": {
            key: sorted(value) for key, value in sorted(run_index.environments.items())
        },
        "manifest_errors": run_index.manifest_errors[:50],
    }


def overview(
    paths: CollectionPaths,
    *,
    environment: str | None = None,
    limit: int = 20,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Status cards plus the most recent runs, for the top of the page."""
    moment = now or datetime.now(timezone.utc)
    index = build_run_index(paths, environment=environment, now=moment)
    ordered = sorted(index.runs.values(), key=_sort_key, reverse=True)
    cards = []
    for collector_source in COLLECTOR_SOURCES:
        runs = [run for run in ordered if run.get("source") == collector_source]
        states: dict[str, int] = {}
        for run in runs:
            state = str(run.get("state"))
            states[state] = states.get(state, 0) + 1
        active = [run for run in runs if str(run.get("state")) in _ACTIVE_STATES]
        succeeded = [
            run
            for run in runs
            if str(run.get("state")) in {"success", "success_with_skips"}
        ]
        cards.append(
            {
                "source": collector_source,
                "ledger_source": COLLECTOR_TO_SOURCE.get(collector_source, collector_source),
                "runs": len(runs),
                "states": states,
                "active": [run_view(paths, run, with_ledger=False) for run in active[:5]],
                "last_run": run_view(paths, runs[0]) if runs else None,
                "last_success": run_view(paths, succeeded[0]) if succeeded else None,
            }
        )
    progress_dir = progress_root(paths.config_root)
    return {
        "generated_at": moment.isoformat(),
        "timezone": "Asia/Seoul (+09:00)",
        "environment": environment,
        "environment_scope": environment or "all",
        "environments": {key: sorted(value) for key, value in sorted(index.environments.items())},
        "cards": cards,
        "recent_runs": [run_view(paths, run) for run in ordered[: max(1, limit)]],
        "manifest_errors": index.manifest_errors[:50],
        "roots": {
            "archive_root": str(paths.archive_root),
            "ledger_root": str(paths.ledger_root),
            "legacy_root": str(paths.legacy_root),
            "progress_root": str(progress_dir),
            "progress_available": progress_dir.is_dir(),
        },
        "stale_after_seconds": DEFAULT_STALE_AFTER_SECONDS,
        "active_scan_days": MAX_ACTIVE_DAY_DIRS,
    }
