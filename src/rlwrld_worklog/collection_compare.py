"""Compare two completed collection runs without exposing collected content.

The comparison deliberately separates document/activity coverage from capture
cost.  A faster run is useful, but it must not earn a pass merely by returning
less data.  Conversely, fewer descendant blocks are not treated like missing
pages when a collection rule intentionally changed its traversal boundary.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


PRIMARY_ENTITY_TYPES = {
    "slack": {"message"},
    "notion": {"page", "data_source"},
    "google_calendar": {"event"},
    "github": {
        "commit",
        "pull_request",
        "review",
        "review_comment",
        "issue_comment",
        "issue",
    },
    "slurm": {"job"},
}


class ComparisonInputError(ValueError):
    """The supplied evidence cannot be compared safely."""


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ComparisonInputError(f"cannot read JSON evidence {path}: {error}") from error
    if not isinstance(value, dict):
        raise ComparisonInputError(f"JSON evidence must be an object: {path}")
    return value


def _ledger_ids(path: Path) -> dict[str, set[str]]:
    result: dict[str, set[str]] = defaultdict(set)
    try:
        with path.open(encoding="utf-8") as handle:
            for number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as error:
                    raise ComparisonInputError(
                        f"invalid ledger JSON at {path}:{number}: {error}"
                    ) from error
                entity_type = record.get("entity_type")
                entity_id = record.get("source_entity_id")
                if isinstance(entity_type, str) and isinstance(entity_id, str):
                    result[entity_type].add(entity_id)
    except OSError as error:
        raise ComparisonInputError(f"cannot read ledger {path}: {error}") from error
    return result


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        raise ComparisonInputError(f"cannot hash evidence {path}: {error}") from error
    return digest.hexdigest()


def _window(manifest: dict[str, Any]) -> tuple[str | None, str | None]:
    requested = manifest.get("requested_window")
    requested = requested if isinstance(requested, dict) else {}
    since = requested.get("since_effective") or manifest.get("since_effective") or manifest.get("since")
    until = requested.get("until") or manifest.get("until")
    return (
        str(since) if since is not None else None,
        str(until) if until is not None else None,
    )


def _source(manifest: dict[str, Any]) -> str:
    source = manifest.get("source") or manifest.get("collector")
    if source == "google-calendar":
        return "google_calendar"
    return str(source or "unknown")


def _duration(manifest: dict[str, Any]) -> float | None:
    try:
        return round(
            (
                datetime.fromisoformat(str(manifest["finished_at"]))
                - datetime.fromisoformat(str(manifest["started_at"]))
            ).total_seconds(),
            3,
        )
    except (KeyError, TypeError, ValueError):
        return None


def _counter(manifest: dict[str, Any], key: str, default: int = 0) -> int:
    counters = manifest.get("counters")
    counters = counters if isinstance(counters, dict) else {}
    value = counters.get(key, manifest.get(key, default))
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else default


def _api_total(manifest: dict[str, Any]) -> int | None:
    counters = manifest.get("counters")
    counters = counters if isinstance(counters, dict) else {}
    calls = counters.get("api_call_counts") or manifest.get("api_call_counts")
    if not isinstance(calls, dict):
        return None
    return sum(int(value) for value in calls.values() if isinstance(value, (int, float)))


def _skip_counts(manifest: dict[str, Any]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    skips = manifest.get("skips")
    if not isinstance(skips, list):
        return {}
    for skip in skips:
        if isinstance(skip, dict):
            reason = skip.get("reason") or skip.get("phase") or skip.get("kind") or "unknown"
        else:
            reason = "unknown"
        counts[str(reason)] += 1
    return dict(sorted(counts.items()))


def _archive_root(manifest_path: Path) -> Path | None:
    resolved = manifest_path.resolve()
    for parent in resolved.parents:
        if parent.name == "manifests":
            return parent.parent
    return None


def _safe_file(root: Path, relative: str) -> Path | None:
    candidate = (root / relative).resolve()
    return candidate if candidate.is_relative_to(root.resolve()) else None


def _notion_moved_after_window(
    manifest_path: Path,
    manifest: dict[str, Any],
    missing: set[str],
    until: str | None,
) -> set[str]:
    """Find missing Notion objects that search now places after the old window.

    Only IDs and timestamps are inspected.  Neither is returned to the caller;
    the public report contains a count, so private page content cannot leak.
    """
    if not missing or not until:
        return set()
    root = _archive_root(manifest_path)
    if root is None:
        return set()
    try:
        window_end = datetime.fromisoformat(until.replace("Z", "+00:00"))
    except ValueError:
        return set()
    moved: set[str] = set()
    files = manifest.get("files")
    for item in files if isinstance(files, list) else []:
        if not isinstance(item, dict) or item.get("kind") != "search":
            continue
        relative = item.get("path")
        path = _safe_file(root, relative) if isinstance(relative, str) else None
        if path is None:
            continue
        try:
            opener = gzip.open if path.suffix == ".gz" else open
            with opener(path, "rt", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, json.JSONDecodeError):
            continue
        for value in payload.get("results", []) if isinstance(payload, dict) else []:
            if not isinstance(value, dict) or value.get("id") not in missing:
                continue
            edited = value.get("last_edited_time")
            if not isinstance(edited, str):
                continue
            try:
                edited_at = datetime.fromisoformat(edited.replace("Z", "+00:00"))
            except ValueError:
                continue
            if edited_at >= window_end:
                moved.add(str(value["id"]))
    return moved


def _verify_manifest_files(path: Path, manifest: dict[str, Any]) -> dict[str, int]:
    root = _archive_root(path)
    checked = missing = mismatched = unsafe = 0
    files = manifest.get("files")
    if root is None:
        return {"checked": 0, "missing": 0, "mismatched": 0, "unsafe": 1}
    for item in files if isinstance(files, list) else []:
        if not isinstance(item, dict):
            continue
        relative, expected = item.get("path"), item.get("sha256")
        if not isinstance(relative, str) or not isinstance(expected, str):
            continue
        file_path = _safe_file(root, relative)
        if file_path is None:
            unsafe += 1
        elif not file_path.is_file():
            missing += 1
        else:
            checked += 1
            mismatched += _sha256(file_path) != expected
    return {"checked": checked, "missing": missing, "mismatched": mismatched, "unsafe": unsafe}


def _entity_report(
    baseline: dict[str, set[str]], candidate: dict[str, set[str]], primary: set[str]
) -> dict[str, dict[str, Any]]:
    report: dict[str, dict[str, Any]] = {}
    for entity_type in sorted(set(baseline) | set(candidate)):
        old, new = baseline[entity_type], candidate[entity_type]
        overlap = old & new
        report[entity_type] = {
            "role": "primary" if entity_type in primary else "supplemental",
            "baseline": len(old),
            "candidate": len(new),
            "overlap": len(overlap),
            "baseline_only": len(old - new),
            "candidate_only": len(new - old),
            "retention": round(len(overlap) / len(old), 6) if old else None,
        }
    return report


def compare_runs(
    *,
    baseline_manifest_path: Path,
    candidate_manifest_path: Path,
    baseline_ledger_path: Path,
    candidate_ledger_path: Path,
    material_retention: float = 0.95,
    verify_files: bool = False,
) -> dict[str, Any]:
    if not 0 < material_retention <= 1:
        raise ComparisonInputError("material_retention must be greater than 0 and at most 1")
    baseline_manifest = _read_json(baseline_manifest_path)
    candidate_manifest = _read_json(candidate_manifest_path)
    baseline_ids = _ledger_ids(baseline_ledger_path)
    candidate_ids = _ledger_ids(candidate_ledger_path)
    baseline_source, candidate_source = _source(baseline_manifest), _source(candidate_manifest)
    baseline_window, candidate_window = _window(baseline_manifest), _window(candidate_manifest)
    scope_matches = baseline_source == candidate_source and baseline_window == candidate_window
    primary = PRIMARY_ENTITY_TYPES.get(candidate_source, set(baseline_ids) | set(candidate_ids))
    entities = _entity_report(baseline_ids, candidate_ids, primary)

    missing_primary_ids = set().union(
        *(baseline_ids[k] - candidate_ids[k] for k in primary)
    ) if primary else set()
    moved = (
        _notion_moved_after_window(
            candidate_manifest_path,
            candidate_manifest,
            missing_primary_ids,
            candidate_window[1],
        )
        if candidate_source == "notion"
        else set()
    )
    unexplained = missing_primary_ids - moved
    baseline_primary = sum(len(baseline_ids[k]) for k in primary)
    added_primary_ids = set().union(
        *(candidate_ids[k] - baseline_ids[k] for k in primary)
    ) if primary else set()
    primary_gain_ratio = len(added_primary_ids) / baseline_primary if baseline_primary else 0.0
    retained_primary = baseline_primary - len(missing_primary_ids)
    relevant_primary = baseline_primary - len(moved)
    adjusted_retention = retained_primary / relevant_primary if relevant_primary else 1.0

    candidate_skips = _skip_counts(candidate_manifest)
    baseline_skips = _skip_counts(baseline_manifest)
    hard_reasons: list[str] = []
    review_reasons: list[str] = []
    if not scope_matches:
        hard_reasons.append("source_or_window_mismatch")
    if candidate_manifest.get("truncated"):
        hard_reasons.append("candidate_truncated")
    if candidate_manifest.get("errors"):
        hard_reasons.append("candidate_errors")
    if _counter(candidate_manifest, "objects_failed_unresolved"):
        hard_reasons.append("candidate_unresolved_objects")
    if _counter(candidate_manifest, "schema_errors"):
        hard_reasons.append("candidate_schema_errors")
    if adjusted_retention < material_retention:
        hard_reasons.append("primary_retention_below_material_threshold")
    elif unexplained:
        review_reasons.append("unexplained_primary_entities_missing")
    if primary_gain_ratio >= (1.0 - material_retention) and added_primary_ids:
        review_reasons.append("material_primary_entities_added")
    if sum(candidate_skips.values()) > sum(baseline_skips.values()):
        review_reasons.append("candidate_has_more_known_skips")
    if candidate_manifest.get("coverage_complete") is False and baseline_manifest.get("coverage_complete") is True:
        review_reasons.append("candidate_coverage_regressed")

    integrity = None
    if verify_files:
        integrity = {
            "baseline": _verify_manifest_files(baseline_manifest_path, baseline_manifest),
            "candidate": _verify_manifest_files(candidate_manifest_path, candidate_manifest),
        }
        if any(
            side[key]
            for side in integrity.values()
            for key in ("missing", "mismatched", "unsafe")
        ):
            hard_reasons.append("raw_file_integrity_failure")

    verdict = "fail" if hard_reasons else "review" if review_reasons else "pass"
    impact = (
        "high"
        if verdict == "fail" or "material_primary_entities_added" in review_reasons
        else "low" if verdict == "review" else "none"
    )
    baseline_duration, candidate_duration = _duration(baseline_manifest), _duration(candidate_manifest)
    baseline_api, candidate_api = _api_total(baseline_manifest), _api_total(candidate_manifest)
    return {
        "schema_version": 1,
        "verdict": verdict,
        "material_impact": impact,
        "reasons": hard_reasons + review_reasons,
        "scope": {
            "matches": scope_matches,
            "source": candidate_source,
            "baseline_window": {"since": baseline_window[0], "until": baseline_window[1]},
            "candidate_window": {"since": candidate_window[0], "until": candidate_window[1]},
            "baseline_rule": baseline_manifest.get("collection_rule_version"),
            "candidate_rule": candidate_manifest.get("collection_rule_version"),
            "baseline_run_id": baseline_manifest.get("run_id"),
            "candidate_run_id": candidate_manifest.get("run_id"),
        },
        "primary_coverage": {
            "entity_types": sorted(primary),
            "baseline": baseline_primary,
            "retained": retained_primary,
            "added": len(added_primary_ids),
            "gain_ratio": round(primary_gain_ratio, 6),
            "moved_after_window": len(moved),
            "missing_unexplained": len(unexplained),
            "adjusted_retention": round(adjusted_retention, 6),
            "material_threshold": material_retention,
        },
        "entities": entities,
        "quality": {
            "baseline_status": baseline_manifest.get("status"),
            "candidate_status": candidate_manifest.get("status"),
            "baseline_coverage_complete": baseline_manifest.get("coverage_complete"),
            "candidate_coverage_complete": candidate_manifest.get("coverage_complete"),
            "baseline_skips": baseline_skips,
            "candidate_skips": candidate_skips,
            "candidate_errors": len(candidate_manifest.get("errors") or []),
            "candidate_truncated": bool(candidate_manifest.get("truncated")),
            "candidate_unresolved_objects": _counter(
                candidate_manifest, "objects_failed_unresolved"
            ),
            "candidate_schema_errors": _counter(candidate_manifest, "schema_errors"),
        },
        "cost": {
            "baseline_duration_seconds": baseline_duration,
            "candidate_duration_seconds": candidate_duration,
            "duration_ratio": round(candidate_duration / baseline_duration, 6)
            if baseline_duration and candidate_duration is not None
            else None,
            "baseline_api_calls": baseline_api,
            "candidate_api_calls": candidate_api,
            "api_call_ratio": round(candidate_api / baseline_api, 6)
            if baseline_api and candidate_api is not None
            else None,
            "baseline_raw_files": len(baseline_manifest.get("files") or []),
            "candidate_raw_files": len(candidate_manifest.get("files") or []),
        },
        "integrity": integrity,
        "evidence_sha256": {
            "baseline_manifest": _sha256(baseline_manifest_path),
            "candidate_manifest": _sha256(candidate_manifest_path),
            "baseline_ledger": _sha256(baseline_ledger_path),
            "candidate_ledger": _sha256(candidate_ledger_path),
        },
    }


def compact_report(report: dict[str, Any]) -> dict[str, Any]:
    """The decision-bearing subset, intended for cheap agent/operator reads."""
    scope = report["scope"]
    coverage = report["primary_coverage"]
    quality = report["quality"]
    cost = report["cost"]
    integrity = report.get("integrity")
    return {
        "verdict": report["verdict"],
        "material_impact": report["material_impact"],
        "reasons": report["reasons"],
        "source": scope["source"],
        "scope_matches": scope["matches"],
        "baseline_rule": scope["baseline_rule"],
        "candidate_rule": scope["candidate_rule"],
        "adjusted_primary_retention": coverage["adjusted_retention"],
        "primary_added": coverage["added"],
        "primary_gain_ratio": coverage["gain_ratio"],
        "missing_unexplained": coverage["missing_unexplained"],
        "moved_after_window": coverage["moved_after_window"],
        "candidate_errors": quality["candidate_errors"],
        "candidate_unresolved_objects": quality["candidate_unresolved_objects"],
        "candidate_schema_errors": quality["candidate_schema_errors"],
        "duration_ratio": cost["duration_ratio"],
        "api_call_ratio": cost["api_call_ratio"],
        "integrity_ok": None
        if integrity is None
        else not any(
            side[key]
            for side in integrity.values()
            for key in ("missing", "mismatched", "unsafe")
        ),
    }
