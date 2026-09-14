"""Verification: counts, duplicates, gaps, and provenance traceability.

Three independent checks, each of which can fail on its own:

  counts       legacy source records vs ledger records vs database rows
  duplicates   distinct entities per observation window, and cross-window
               repeats, reported separately because they mean different things
  provenance   every ledger row names a legacy file that still exists and
               still hashes to the recorded value (principle 8)

The provenance check re-hashes real files, so it is sampled by default.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from .common import file_sha256
from .convert import iter_extracted_text_files, iter_ledger_files, read_jsonl
from .schema import validate_record


@dataclass
class VerifyReport:
    source: str
    ledger_root: str
    # Every root the records were read from. The ledger lives under more than
    # one root -- a live staging root and the backfill archives -- and a
    # verifier that could only see one always reported fewer records than the
    # database held, which made `ok:false` the permanent answer and the check
    # worthless. `ledger_root` stays as the first one for readers that have
    # only ever seen a single root.
    ledger_roots: list[str] = field(default_factory=list)
    roots_with_no_files: list[str] = field(default_factory=list)
    records_by_root: dict[str, int] = field(default_factory=dict)
    records_in_more_than_one_root: int = 0
    legacy_root: str | None = None
    ledger_files: int = 0
    ledger_records: int = 0
    extracted_text: int = 0
    by_entity_type: dict[str, int] = field(default_factory=dict)
    by_capture_profile: dict[str, int] = field(default_factory=dict)
    schema_invalid: int = 0
    schema_invalid_samples: list[str] = field(default_factory=list)
    unknown_counts: dict[str, int] = field(default_factory=dict)
    duplicates: dict[str, Any] = field(default_factory=dict)
    provenance: dict[str, Any] = field(default_factory=dict)
    observation_dates: dict[str, Any] = field(default_factory=dict)
    database: dict[str, Any] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "ok": self.ok,
            "ledger_root": self.ledger_root,
            "ledger_roots": self.ledger_roots,
            "roots_with_no_files": self.roots_with_no_files,
            "records_by_root": dict(sorted(self.records_by_root.items())),
            "records_in_more_than_one_root": self.records_in_more_than_one_root,
            "legacy_root": self.legacy_root,
            "ledger_files": self.ledger_files,
            "ledger_records": self.ledger_records,
            "extracted_text": self.extracted_text,
            "by_entity_type": dict(sorted(self.by_entity_type.items())),
            "by_capture_profile": dict(sorted(self.by_capture_profile.items())),
            "schema_invalid": self.schema_invalid,
            "schema_invalid_samples": self.schema_invalid_samples[:10],
            "unknown_counts": dict(sorted(self.unknown_counts.items())),
            "duplicates": self.duplicates,
            "provenance": self.provenance,
            "observation_dates": self.observation_dates,
            "database": self.database,
            "failures": self.failures,
            "warnings": self.warnings,
        }


def verify_ledger(
    *,
    ledger_root: Path | None = None,
    ledger_roots: Sequence[Path] | None = None,
    source: str,
    legacy_root: Path | None = None,
    database_url: str | None = None,
    provenance_sample: int = 200,
    validate_schema: bool = True,
) -> VerifyReport:
    roots = [Path(root) for root in (ledger_roots or [])]
    if ledger_root is not None:
        roots.insert(0, Path(ledger_root))
    if not roots:
        raise ValueError("verify_ledger needs at least one ledger root")
    # Same root named twice is a caller mistake, not data: counting it twice
    # would inflate the file totals and fail the database comparison in the
    # opposite direction from the bug being fixed here.
    seen_roots: list[Path] = []
    for root in roots:
        if root not in seen_roots:
            seen_roots.append(root)
    roots = seen_roots

    report = VerifyReport(source=source, ledger_root=str(roots[0]))
    report.ledger_roots = [str(root) for root in roots]
    if legacy_root:
        report.legacy_root = str(legacy_root)

    entity_counter: Counter = Counter()
    profile_counter: Counter = Counter()
    unknown_counter: Counter = Counter()
    window_counter: Counter = Counter()

    # (entity_type, source_entity_id, window_start) -> content hashes seen
    within_window: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    # (entity_type, source_entity_id, window_start) -> capture profiles seen
    within_window_profiles: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    # (entity_type, source_entity_id, window_start) -> containers seen
    within_window_containers: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    within_window_rows: Counter = Counter()
    # (entity_type, source_entity_id) -> distinct windows
    across_windows: dict[tuple[str, str], set[str]] = defaultdict(set)
    content_hashes: Counter = Counter()
    provenance_files: dict[str, str] = {}
    missing_provenance = 0

    files: list[tuple[Path, Path]] = []
    for root in roots:
        found = iter_ledger_files(root, source)
        if not found:
            # Named and empty. A root that holds nothing is usually a wrong
            # path, and a verifier that quietly ignored it would report a
            # clean run over a fraction of the ledger.
            report.roots_with_no_files.append(str(root))
        files.extend((root, path) for path in found)
    report.ledger_files = len(files)

    # A record can sit in two roots -- a day that was collected live and then
    # re-archived by a backfill. It is one record; counted twice it would make
    # the file total exceed the database and fail for the opposite reason.
    seen_ledger_ids: set[str] = set()
    for root, path in files:
        for record in read_jsonl(path):
            ledger_id = record.get("ledger_id")
            if ledger_id:
                if ledger_id in seen_ledger_ids:
                    report.records_in_more_than_one_root += 1
                    continue
                seen_ledger_ids.add(ledger_id)
            report.records_by_root[str(root)] = report.records_by_root.get(str(root), 0) + 1
            report.ledger_records += 1
            entity = record.get("entity_type", "?")
            entity_counter[entity] += 1
            profile_counter[record.get("capture_profile", "?")] += 1

            if validate_schema:
                errors = validate_record(record)
                if errors:
                    report.schema_invalid += 1
                    if len(report.schema_invalid_samples) < 10:
                        report.schema_invalid_samples.append(
                            f"{record.get('ledger_id')}: {errors[0]}"
                        )

            window = (record.get("observation_window") or {}).get("start") or "unknown-date"
            window_counter[window] += 1
            entity_id = record.get("source_entity_id", "")
            key = (entity, entity_id, window)
            digest = record.get("content_hash", "")
            within_window[key].add(digest)
            within_window_rows[key] += 1
            within_window_profiles[key].add(record.get("capture_profile", "?"))
            container = (record.get("scope") or {}).get("container")
            if container:
                within_window_containers[key].add(str(container))
            across_windows[(entity, entity_id)].add(window)
            content_hashes[digest] += 1

            provenance = record.get("provenance") or {}
            source_file = provenance.get("source_file")
            digest = provenance.get("source_file_sha256")
            if not source_file or not digest:
                missing_provenance += 1
            else:
                provenance_files.setdefault(source_file, digest)

            tenant = record.get("tenant") or {}
            if tenant.get("status") == "unknown":
                unknown_counter["tenant_workspace_unknown"] += 1
            if record.get("source_updated_at_status") == "unknown":
                unknown_counter["source_updated_at_unknown"] += 1
            if (record.get("deleted_state") or {}).get("status") == "unknown":
                unknown_counter["deleted_state_unknown"] += 1
            completeness = (record.get("capture_completeness") or {}).get("status")
            if completeness == "unknown":
                unknown_counter["capture_completeness_unknown"] += 1
            elif completeness == "not_recorded":
                unknown_counter["capture_completeness_not_recorded"] += 1
            if (record.get("scope") or {}).get("calendar_id_status") == "unknown":
                unknown_counter["scope_calendar_unknown"] += 1
            if (record.get("visibility_routing") or {}).get("routing_anomaly"):
                unknown_counter["visibility_routing_anomaly"] += 1

    seen_text_ids: set[str] = set()
    for root in roots:
        for path in iter_extracted_text_files(root, source):
            for row in read_jsonl(path):
                key = str(row.get("ledger_id") or row.get("document_id") or "")
                if key:
                    if key in seen_text_ids:
                        continue
                    seen_text_ids.add(key)
                report.extracted_text += 1

    report.by_entity_type = dict(entity_counter)
    report.by_capture_profile = dict(profile_counter)
    report.unknown_counts = dict(unknown_counter)

    # An entity can legitimately be observed more than once inside one window
    # when two capture paths saw it: conversations.history vs search.messages,
    # or daily_raw vs thread_store. Those copies carry different fields --
    # `edited` exists only on the search-supplemented copy -- so collapsing
    # them would destroy data. Only byte-identical content is a true duplicate,
    # and that is already impossible because content_hash is part of ledger_id.
    identical_rows = sum(
        within_window_rows[key] - len(hashes) for key, hashes in within_window.items()
    )
    multi_observation = {key: hashes for key, hashes in within_window.items() if len(hashes) > 1}
    profile_pairs: Counter = Counter()
    container_pairs: Counter = Counter()
    cross_visibility = 0
    for key in multi_observation:
        profile_pairs[" + ".join(sorted(within_window_profiles[key]))] += 1
        containers = sorted(within_window_containers[key])
        if containers:
            container_pairs[" + ".join(containers)] += 1
        # The same message under both a company-wide and a restricted
        # container is the visibility routing incident, not a capture artifact.
        if "common" in containers and ({"dm", "private"} & set(containers)):
            cross_visibility += 1

    repeated_across = {key: len(windows) for key, windows in across_windows.items() if len(windows) > 1}
    report.duplicates = {
        "distinct_entity_window_pairs": len(within_window),
        "identical_content_rows": identical_rows,
        "entities_with_multiple_observations_in_window": len(multi_observation),
        "extra_observations_in_window": sum(len(hashes) - 1 for hashes in multi_observation.values()),
        "multi_observation_by_capture_profile": dict(profile_pairs.most_common(10)),
        "multi_observation_by_container": dict(container_pairs.most_common(10)),
        "same_entity_in_public_and_restricted_container": cross_visibility,
        "distinct_entities": len(across_windows),
        "entities_seen_in_multiple_windows": len(repeated_across),
        "max_windows_for_one_entity": max(repeated_across.values(), default=1),
        "distinct_content_hashes": len(content_hashes),
        "note": (
            "identical_content_rows must be 0. Multiple observations of one "
            "entity inside a window are expected where two capture paths "
            "overlap and their copies differ; they are kept because the "
            "search-supplemented copy is the only one carrying edit metadata. "
            "Entities across several windows are separate historical "
            "observations, not duplicates."
        ),
    }
    if identical_rows:
        report.failures.append(
            f"{identical_rows} rows are byte-identical within an entity and window"
        )
    if cross_visibility:
        report.warnings.append(
            f"{cross_visibility} entities appear under both a company-wide and a "
            "restricted container in the same window (visibility routing incident)"
        )
    if report.schema_invalid:
        report.failures.append(f"{report.schema_invalid} records fail the standard v1 schema")
    if missing_provenance:
        report.failures.append(f"{missing_provenance} records have no source file or hash")

    report.observation_dates = {
        "count": len(window_counter),
        "first": min(window_counter, default=None),
        "last": max(window_counter, default=None),
        "unknown_date_records": window_counter.get("unknown-date", 0),
    }

    report.provenance = _verify_provenance(
        provenance_files, legacy_root=legacy_root, sample=provenance_sample
    )
    if report.provenance.get("hash_mismatch"):
        report.failures.append(
            f"{report.provenance['hash_mismatch']} legacy files no longer match the recorded hash"
        )
    if report.provenance.get("missing"):
        report.failures.append(f"{report.provenance['missing']} legacy files referenced but not found")

    if database_url:
        report.database = _verify_database(database_url, source, report)

    return report


def _verify_provenance(
    provenance_files: dict[str, str], *, legacy_root: Path | None, sample: int
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "distinct_source_files": len(provenance_files),
        "checked": 0,
        "verified": 0,
        "hash_mismatch": 0,
        "missing": 0,
        "skipped_no_legacy_root": legacy_root is None,
        "mismatch_samples": [],
    }
    if legacy_root is None or not provenance_files:
        return result
    # Deterministic sample: sorted, then evenly spaced.
    names = sorted(provenance_files)
    if sample and sample < len(names):
        step = len(names) / sample
        names = [names[int(index * step)] for index in range(sample)]
    for name in names:
        result["checked"] += 1
        path = legacy_root / name
        if not path.is_file():
            result["missing"] += 1
            if len(result["mismatch_samples"]) < 10:
                result["mismatch_samples"].append(f"missing:{name}")
            continue
        if file_sha256(str(path)) == provenance_files[name]:
            result["verified"] += 1
        else:
            result["hash_mismatch"] += 1
            if len(result["mismatch_samples"]) < 10:
                result["mismatch_samples"].append(f"hash_mismatch:{name}")
    return result


def _verify_database(database_url: str, source: str, report: VerifyReport) -> dict[str, Any]:
    import psycopg

    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM ledger_records WHERE source = %s", (source,))
            ledger_rows = cursor.fetchone()[0]
            cursor.execute(
                "SELECT count(*) FROM ledger_extracted_text WHERE source = %s", (source,)
            )
            text_rows = cursor.fetchone()[0]
            cursor.execute("SELECT count(*) FROM timeline_events WHERE source = %s", (source,))
            timeline_rows = cursor.fetchone()[0]
            cursor.execute(
                """
                SELECT origin, count(*) FROM source_object_heads
                WHERE source = %s GROUP BY origin
                """,
                (source,),
            )
            heads = dict(cursor.fetchall())
            cursor.execute(
                """
                SELECT count(*) FROM ledger_records
                WHERE source = %s AND (source_file = '' OR source_file_sha256 = '')
                """,
                (source,),
            )
            untraceable = cursor.fetchone()[0]
    database = {
        "ledger_records": ledger_rows,
        "extracted_text": text_rows,
        "timeline_events": timeline_rows,
        "heads_by_origin": heads,
        "rows_without_provenance": untraceable,
        "ledger_matches_files": ledger_rows == report.ledger_records,
        "extracted_text_matches_files": text_rows == report.extracted_text,
    }
    if untraceable:
        report.failures.append(f"{untraceable} database ledger rows lack provenance")
    if ledger_rows != report.ledger_records:
        report.failures.append(
            f"database has {ledger_rows} ledger rows but files hold {report.ledger_records}"
        )
    if text_rows != report.extracted_text:
        report.failures.append(
            f"database has {text_rows} extracted-text rows but files hold {report.extracted_text}"
        )
    return database


def write_report(report: VerifyReport, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report.as_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path
