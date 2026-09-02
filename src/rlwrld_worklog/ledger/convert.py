"""Legacy -> standard ledger conversion batch.

Output layout (all under --out-root, never inside the legacy tree):

    ledger/<source>/<observation_date>.jsonl        LedgerRecord, one per line
    extracted_text/<source>/<observation_date>.jsonl ExtractedText artifacts
    _manifest/<source>.json                          counts, per-file hashes

Records are deterministic: the same legacy input produces byte-identical
output, so a re-run is safe and a diff is meaningful. Run-specific values live
in the manifest, not in the records.

Writes are atomic (temp file + rename) and the legacy tree is only ever read.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .common import MetaSignalResolver, SlackWorkspaceResolver, file_sha256
from .legacy_notion import NotionConvertStats, iter_notion_records
from .legacy_slack import SlackConvertStats, iter_slack_records
from .schema import CONVERTER_VERSION, ExtractedText, LedgerRecord, validate_record

SUPPORTED_SOURCES = ("slack", "notion")
UNIMPLEMENTED_SOURCES = {
    "google_calendar": (
        "Google Calendar conversion is Phase 2b. Blocking design point: legacy "
        "events carry no calendarId, so scope.calendar must be recorded as "
        "unknown rather than inferred (principle 5)."
    )
}


@dataclass
class ConvertResult:
    source: str
    dry_run: bool
    records_written: int = 0
    artifacts_written: int = 0
    files_written: int = 0
    schema_errors: int = 0
    schema_error_samples: list[str] = field(default_factory=list)
    by_entity_type: dict[str, int] = field(default_factory=dict)
    by_window_start: dict[str, int] = field(default_factory=dict)
    unknown_counts: dict[str, int] = field(default_factory=dict)
    source_stats: dict[str, Any] = field(default_factory=dict)
    output_files: dict[str, str] = field(default_factory=dict)
    manifest_path: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "dry_run": self.dry_run,
            "records_written": self.records_written,
            "artifacts_written": self.artifacts_written,
            "files_written": self.files_written,
            "schema_errors": self.schema_errors,
            "schema_error_samples": self.schema_error_samples[:20],
            "by_entity_type": dict(sorted(self.by_entity_type.items())),
            "by_window_start_sample": dict(sorted(self.by_window_start.items())[:5]),
            "observation_dates": len(self.by_window_start),
            "unknown_counts": dict(sorted(self.unknown_counts.items())),
            "source_stats": self.source_stats,
            "manifest": self.manifest_path,
        }


class _PartitionWriter:
    """Buffers JSONL by observation date and writes each partition atomically."""

    def __init__(self, base: Path, *, dry_run: bool) -> None:
        self._base = base
        self._dry_run = dry_run
        self._buffers: dict[str, list[str]] = {}

    def add(self, partition: str, payload: dict[str, Any]) -> None:
        line = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        self._buffers.setdefault(partition, []).append(line)

    def flush(self) -> dict[str, str]:
        written: dict[str, str] = {}
        if self._dry_run:
            return written
        self._base.mkdir(parents=True, exist_ok=True)
        for partition, lines in sorted(self._buffers.items()):
            # Sorting makes the file order independent of filesystem walk order.
            lines.sort()
            target = self._base / f"{partition}.jsonl"
            temporary = target.with_suffix(".jsonl.tmp")
            temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
            os.replace(temporary, target)
            written[str(target)] = file_sha256(str(target))
        return written

    @property
    def partitions(self) -> dict[str, list[str]]:
        return self._buffers


def _partition_of(window: dict[str, Any]) -> str:
    start = window.get("start")
    return start if isinstance(start, str) and start else "unknown-date"


def _count_unknowns(record: LedgerRecord, counters: Counter) -> None:
    if record.tenant.get("status") == "unknown":
        counters["tenant_workspace_unknown"] += 1
    if record.source_updated_at_status == "unknown":
        counters["source_updated_at_unknown"] += 1
    if record.deleted_state.get("status") == "unknown":
        counters["deleted_state_unknown"] += 1
    completeness = record.capture_completeness.get("status")
    if completeness == "unknown":
        counters["capture_completeness_unknown"] += 1
    elif completeness == "not_recorded":
        counters["capture_completeness_not_recorded"] += 1
    if record.observation_window.get("granularity") == "unknown":
        counters["observation_window_unknown"] += 1
    if record.scope.get("calendar_id_status") == "unknown":
        counters["scope_calendar_unknown"] += 1
    if record.visibility_routing.get("routing_anomaly"):
        counters["visibility_routing_anomaly"] += 1
    if record.capture_completeness.get("lossy_fields"):
        counters["records_with_lossy_fields"] += 1


def convert_source(
    *,
    legacy_root: Path,
    out_root: Path,
    source: str,
    dry_run: bool = False,
    validate: bool = True,
    roots: tuple[str, ...] = ("shared", "personal"),
    salvage_comments: bool = False,
    limit: int | None = None,
) -> ConvertResult:
    if source in UNIMPLEMENTED_SOURCES:
        raise NotImplementedError(UNIMPLEMENTED_SOURCES[source])
    if source not in SUPPORTED_SOURCES:
        raise ValueError(f"unsupported source: {source}")
    if not legacy_root.is_dir():
        raise FileNotFoundError(f"legacy root not found: {legacy_root}")

    result = ConvertResult(source=source, dry_run=dry_run)
    meta_resolver = MetaSignalResolver(legacy_root)
    ledger_writer = _PartitionWriter(out_root / "ledger" / source, dry_run=dry_run)
    text_writer = _PartitionWriter(out_root / "extracted_text" / source, dry_run=dry_run)
    entity_counter: Counter = Counter()
    window_counter: Counter = Counter()
    unknown_counter: Counter = Counter()

    if source == "slack":
        stats: Any = SlackConvertStats()
        workspace_resolver = SlackWorkspaceResolver(legacy_root)
        workspace_resolver.prime()
        stream: Iterable[Any] = iter_slack_records(
            legacy_root,
            stats=stats,
            meta_resolver=meta_resolver,
            workspace_resolver=workspace_resolver,
            roots=roots,
        )
    else:
        stats = NotionConvertStats()
        stream = iter_notion_records(
            legacy_root,
            stats=stats,
            meta_resolver=meta_resolver,
            roots=roots,
            salvage_comments=salvage_comments,
        )

    for item in stream:
        if isinstance(item, ExtractedText):
            text_writer.add(_text_partition(item), item.to_dict())
            result.artifacts_written += 1
            continue
        payload = item.to_dict()
        if validate:
            errors = validate_record(payload)
            if errors:
                result.schema_errors += 1
                if len(result.schema_error_samples) < 20:
                    result.schema_error_samples.append(f"{item.ledger_id}: {errors[0]}")
                continue
        partition = _partition_of(item.observation_window)
        ledger_writer.add(partition, payload)
        entity_counter[item.entity_type] += 1
        window_counter[partition] += 1
        _count_unknowns(item, unknown_counter)
        result.records_written += 1
        if limit and result.records_written >= limit:
            break

    ledger_files = ledger_writer.flush()
    text_files = text_writer.flush()
    result.files_written = len(ledger_files) + len(text_files)
    result.output_files = {**ledger_files, **text_files}
    result.by_entity_type = dict(entity_counter)
    result.by_window_start = dict(window_counter)
    result.unknown_counts = dict(unknown_counter)
    result.source_stats = stats.as_dict()

    if not dry_run:
        result.manifest_path = str(
            _write_manifest(
                out_root=out_root,
                source=source,
                result=result,
                legacy_root=legacy_root,
                roots=roots,
                salvage_comments=salvage_comments,
            )
        )
    return result


def _text_partition(artifact: ExtractedText) -> str:
    reference = artifact.provenance.get("source_file", "")
    for part in str(reference).split("/"):
        if len(part) == 10 and part[4] == "-" and part[7] == "-":
            return part
    return "unknown-date"


def _write_manifest(
    *,
    out_root: Path,
    source: str,
    result: ConvertResult,
    legacy_root: Path,
    roots: tuple[str, ...],
    salvage_comments: bool,
) -> Path:
    manifest_dir = out_root / "_manifest"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "source": source,
        "converter_version": CONVERTER_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "legacy_root": str(legacy_root),
        "roots": list(roots),
        "salvage_comments": salvage_comments,
        "records_written": result.records_written,
        "artifacts_written": result.artifacts_written,
        "by_entity_type": result.by_entity_type,
        "observation_dates": len(result.by_window_start),
        "records_by_observation_date": dict(sorted(result.by_window_start.items())),
        "unknown_counts": result.unknown_counts,
        "schema_errors": result.schema_errors,
        "source_stats": result.source_stats,
        "output_files": result.output_files,
    }
    target = manifest_dir / f"{source}.json"
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(temporary, target)
    return target


def iter_ledger_files(out_root: Path, source: str) -> list[Path]:
    base = out_root / "ledger" / source
    return sorted(base.glob("*.jsonl")) if base.is_dir() else []


def iter_extracted_text_files(out_root: Path, source: str) -> list[Path]:
    base = out_root / "extracted_text" / source
    return sorted(base.glob("*.jsonl")) if base.is_dir() else []


def read_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                yield json.loads(line)
