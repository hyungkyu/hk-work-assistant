"""Slurm accounting capture from the infra node's sacct dump.

The source is `http://infra-node:8888/api/download/jobs-raw-<cloud>`, which
answers 302 with an S3 presigned URL. The dump is the full 117-column sacct
export for one cloud, pipe separated and gzipped; there is no time-window
query and no pagination, so a window is produced by taking the dump once and
projecting it onto days locally.

What this collector inherits from `_migration/34_slurm_jobs_build.py`, and
why each part is not negotiable:

  * **Day keys come from `End`, never from `Submit`.** The naver cluster
    (`mlxp`) reports an empty `Submit` on every one of its 17,395 jobs while
    `End` is always present. Keying on `Submit` deletes that cluster whole.
  * **Finished state is decided by a blacklist.** The legacy script began with
    a whitelist of terminal states and silently dropped 6,836 `SUCCEEDED`
    jobs, because clusters do not agree on state names. Unknown states are
    kept and counted, never discarded.
  * **All 117 columns are preserved, as a header plus rows.** Repeating the
    keys on every row costs three to four times the space for nothing.
    Derived values -- efficiency, job classification -- are not computed here:
    HK's rule is that a definition which changes later cannot be recovered
    from a value that was already reduced.
  * **Step rows (`.batch`, `.extern`) follow their parent's end date.** They
    hold the only real resource usage (`MaxRSS`, `TRESUsageIn*`), so they are
    archived, but they are not given a ledger entity type yet: they are
    sub-resources of a job rather than activities, and projecting them would
    turn one job into several timeline events.

Two things the legacy script counted and then threw away are recorded here as
named coverage notes instead, because a number that is only printed to a
terminal is lost:

  * jobs that finished but carry no `End` value, which therefore have no day
    to be filed under;
  * the retention floor, which differs per cloud -- kakao reaches back to
    2025-07-08, aws only to 2026-02-27, naver to 2026-03-26. Older work is
    outside what this API can answer, and the legacy archive is the only
    evidence for it.

The presigned URL is never logged, never written to a manifest and never
returned: it carries a signature valid for 900 seconds and is a credential.
"""

from __future__ import annotations

import gzip
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Protocol, Sequence

from .archive import RawArchive
from .github_collector import KST, Window, kst_day

SLURM_SOURCE = "slurm"
SLURM_CAPTURE_PROFILE = "live-slurm-sacct-dump/v1"
CHECKPOINT_SCHEMA_VERSION = 1

# Cloud names the download endpoint accepts, and the `Cluster` column value
# each one reports. `all` is deliberately not the default: per-cloud dumps are
# smaller, fail independently, and keep one cloud's outage from looking like
# everyone's quiet day.
CLOUDS = ("kakao", "aws", "naver")

# `Cluster` value -> our folder name. Extends the legacy CLUSTER_MAP with
# `rlwrld-26q3`, which previously fell through under its raw name.
CLUSTER_MAP = {
    "deepops": "kakao",
    "skt-rlwrld": "aws_skt",
    "naver-mlxp": "naver_mlxp",
    "mlxp": "naver_mlxp",
    "rlwrld-26q3": "rlwrld_26q3",
}

# States that mean "not finished yet". Everything else is treated as finished,
# including a state this list has never seen. Inverting this into a whitelist
# is what lost 6,836 jobs.
NOT_FINISHED = frozenset(
    {
        "RUNNING",
        "PENDING",
        "SUSPENDED",
        "REQUEUED",
        "REQUEUE_HOLD",
        "REQUEUE_FED",
        "RESIZING",
        "SIGNALING",
        "STAGE_OUT",
        "SPECIAL_EXIT",
        "RESV_DEL_HOLD",
    }
)

# Reference only, for reporting a state nobody has seen before. Never used to
# decide whether a job is finished.
KNOWN_FINISHED = frozenset(
    {
        "COMPLETED",
        "SUCCEEDED",
        "FAILED",
        "CANCELLED",
        "TIMEOUT",
        "OUT_OF_MEMORY",
        "NODE_FAIL",
        "PREEMPTED",
        "BOOT_FAIL",
        "DEADLINE",
        "REVOKED",
    }
)

REQUIRED_COLUMNS = ("JobID", "State", "End", "Cluster")

# Per-cloud first day the API can answer for. Anything earlier is not missing
# data, it is outside retention, and the legacy archive is its only evidence.
RETENTION_FLOOR = {
    "kakao": "2025-07-08",
    "aws": "2026-02-27",
    "naver": "2026-03-26",
}

COVERAGE_NOTES = (
    "slurm.day_key_is_end_not_submit: a job is filed under the KST day it ended. The naver "
    "cluster (mlxp) reports an empty Submit on every job, so keying on Submit would drop that "
    "cluster entirely.",
    "slurm.finished_state_is_a_blacklist: any state outside the not-finished list counts as "
    "finished, including one never seen before. A whitelist previously discarded 6,836 "
    "SUCCEEDED jobs because clusters do not agree on state names.",
    "slurm.all_117_columns_preserved: the sacct export is archived as a header plus rows with "
    "no field dropped and no derived value computed. Efficiency and job classification are "
    "decided by the consumer, not here.",
    "slurm.step_rows_follow_their_parent: .batch and .extern rows are archived under their "
    "parent job's end date because they carry the only real resource usage. They have no "
    "ledger entity type yet, so they are raw-only.",
    "slurm.running_jobs_are_not_captured: only finished jobs are archived, once, on the day "
    "they ended. A job still running at capture time appears in a later run, not this one.",
)

# Repeated observations of one job are deliberately NOT a coverage note. A
# coverage note names something the capture could not get; here nothing is
# lost -- every row reaches the archive -- and what needs saying is a counting
# definition, which the manifest carries as `repeat_observations` alongside
# `jobs_in_window` and `parent_rows_archived`. Minting a note key for it would
# force a new published rule version to describe a capture that did not
# change.


class DumpFetcher(Protocol):
    """Fetches one cloud's gzipped sacct dump to a local path.

    Implementations follow the 302 to S3 themselves and must not return,
    log or store the presigned URL.
    """

    def fetch(self, cloud: str, destination: Path) -> dict[str, Any]: ...


@dataclass(frozen=True)
class SlurmCollectionResult:
    run_id: str
    window: Window
    clouds_attempted: tuple[str, ...]
    clouds_collected: tuple[str, ...]
    jobs: int
    parent_rows: int
    step_rows: int
    manifest_path: Path
    checkpoint_advanced: bool = False
    days: dict[str, dict[str, int]] = field(default_factory=dict)
    counters: dict[str, Any] = field(default_factory=dict)


def parse_sacct_timestamp(value: str | None) -> datetime | None:
    """Parse an sacct timestamp, or None when there is nothing to parse.

    sacct writes local time without an offset for most clusters and an
    offset-bearing ISO string for others. A naive value is read as KST, which
    is the timezone every cluster in this fleet is configured for; the
    distinction is recorded so a later reader is not guessing.
    """
    text = (value or "").strip()
    if not text or text in {"Unknown", "None", "N/A", "NONE"}:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        for pattern in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                parsed = datetime.strptime(text, pattern)
                break
            except ValueError:
                continue
        else:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=KST)
    return parsed


def base_job_id(job_id: str | None) -> str:
    """`12223.batch` -> `12223`; `12223_5.extern` -> `12223_5`."""
    return (job_id or "").split(".", 1)[0]


def is_step_row(job_id: str | None) -> bool:
    return "." in (job_id or "")


def normalized_state(value: str | None) -> str:
    """The first word of the State column. `CANCELLED by 1234` -> `CANCELLED`."""
    text = (value or "").strip()
    return text.split()[0] if text else ""


class SlurmCollector:
    def __init__(self, fetcher: DumpFetcher, archive: RawArchive, *, staging_root: Path) -> None:
        self.fetcher = fetcher
        self.archive = archive
        self.staging_root = staging_root

    def collect(
        self,
        *,
        window: Window,
        clouds: Sequence[str] = CLOUDS,
        advance_checkpoint: bool = True,
        backfill: bool = False,
        include_steps: bool = True,
    ) -> SlurmCollectionResult:
        archive = self.archive
        checkpoint = archive.read_checkpoint()
        archive.set_checkpoint_in(
            {
                "run_id": checkpoint.get("run_id"),
                "collected_through": checkpoint.get("collected_through"),
                "ignored_for_backfill": backfill,
            }
        )
        archive.set_requested_window(
            {
                **window.as_dict(),
                "mode": "backfill" if backfill else "incremental",
                "clouds": list(clouds),
                "include_steps": include_steps,
            }
        )
        for note in COVERAGE_NOTES:
            archive.note_coverage(note)

        days: dict[str, dict[str, int]] = {day: {} for day in window.days}
        per_cloud: dict[str, dict[str, Any]] = {}
        collected: list[str] = []
        jobs_total = 0
        parent_rows_total = 0
        steps_total = 0

        self.staging_root.mkdir(parents=True, exist_ok=True)
        for cloud in clouds:
            self._note_retention(cloud, window=window)
            destination = self.staging_root / f"{archive.run_id}-{cloud}.psv.gz"
            try:
                fetched = self.fetcher.fetch(cloud, destination)
            except Exception as error:  # the fetcher decides what is fatal
                archive.note_skip(
                    "dump_unavailable", cloud=cloud, error=f"{type(error).__name__}: {error}"[:200]
                )
                per_cloud[cloud] = {"status": "unavailable"}
                continue
            try:
                summary = self._project_cloud(
                    cloud, destination, window=window, days=days, include_steps=include_steps
                )
            except SlurmSchemaError as error:
                archive.note_error("schema_changed", cloud=cloud, detail=str(error)[:200])
                per_cloud[cloud] = {"status": "schema_changed", "detail": str(error)[:200]}
                continue
            finally:
                # The dump is a means, not evidence: what is preserved is the
                # archived projection. Leaving 191MB of staging behind on every
                # run fills the disk the daily batch needs.
                destination.unlink(missing_ok=True)
            summary.update(
                {
                    key: value
                    for key, value in fetched.items()
                    # A fetcher must not hand back a URL; if it does, it is
                    # dropped here rather than reaching a manifest.
                    if key not in {"url", "presigned_url", "location"}
                }
            )
            summary["status"] = "ok"
            per_cloud[cloud] = summary
            collected.append(cloud)
            jobs_total += summary["jobs_in_window"]
            parent_rows_total += summary["parent_rows_archived"]
            steps_total += summary["step_rows_in_window"]

        counters = {
            "clouds_attempted": list(clouds),
            "clouds_collected": collected,
            "jobs_in_window": jobs_total,
            "parent_rows_archived": parent_rows_total,
            "repeat_observations": parent_rows_total - jobs_total,
            "step_rows_in_window": steps_total,
            "days": {day: dict(counts) for day, counts in days.items()},
            "per_cloud": per_cloud,
        }
        status = "success"
        if not collected:
            status = "failed"
        elif archive.skips or archive.errors:
            status = "success_with_skips"

        checkpoint_advanced = False
        complete = len(collected) == len(clouds)
        if (
            advance_checkpoint
            and not backfill
            and not archive.dry_run
            and not archive.truncated
            and complete
        ):
            archive.write_checkpoint(
                {
                    "schema_version": CHECKPOINT_SCHEMA_VERSION,
                    "source": SLURM_SOURCE,
                    "run_id": archive.run_id,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    "collected_through": window.end_date.isoformat(),
                    "clouds": collected,
                }
            )
            checkpoint_advanced = True
        elif advance_checkpoint and not backfill and not complete:
            archive.note_coverage(
                "slurm.checkpoint_held_back_on_partial_run: at least one cloud did not answer, "
                "so the watermark stays where it was and the next run repeats this window."
            )

        manifest_path = archive.finish(
            {
                "status": status,
                "mode": "backfill" if backfill else "incremental",
                "clouds_attempted": list(clouds),
                "clouds_collected": collected,
                "jobs": jobs_total,
                "parent_rows_archived": parent_rows_total,
                "step_rows": steps_total,
                "counters": counters,
            }
        )
        return SlurmCollectionResult(
            run_id=archive.run_id,
            window=window,
            clouds_attempted=tuple(clouds),
            clouds_collected=tuple(collected),
            jobs=jobs_total,
            parent_rows=parent_rows_total,
            step_rows=steps_total,
            manifest_path=manifest_path,
            checkpoint_advanced=checkpoint_advanced,
            days={day: dict(counts) for day, counts in days.items()},
            counters=counters,
        )

    # ---------------------------------------------------------- projection

    def _note_retention(self, cloud: str, *, window: Window) -> None:
        floor = RETENTION_FLOOR.get(cloud)
        if not floor:
            return
        if window.start_date < date.fromisoformat(floor):
            # The key stays constant and the cloud is named in the message.
            # Building the cloud and date into the key would make it
            # undeclarable: the rule registry names note keys literally, and a
            # key that varies per cloud could never be published.
            self.archive.note_coverage(
                f"slurm.api_retention_floor: the {cloud} dump begins {floor}. Days in the "
                "requested window before that are outside what the API can answer, not days "
                "without work; the legacy archive is their only evidence."
            )

    def _project_cloud(
        self,
        cloud: str,
        dump: Path,
        *,
        window: Window,
        days: dict[str, dict[str, int]],
        include_steps: bool,
    ) -> dict[str, Any]:
        """Two passes over the dump, exactly as the legacy build script did.

        The first finds the finished parent jobs whose end date lands in the
        window; the second collects those rows plus their step rows. Two
        passes are needed because a step row carries no usable end date of its
        own and has to inherit its parent's.

        A job can appear in the dump more than once -- an earlier `RUNNING`
        observation and a later finished one, for instance -- and the second
        pass archives every row that belongs to a kept job, including the
        observations the first pass filtered out. That is the right thing for
        an immutable archive, and it means the number of archived parent rows
        is not the number of jobs. Both are reported, separately and under
        names that say which is which: on the August kakao dump they differ by
        8,995.
        """
        header, keep, first = self._first_pass(cloud, dump, window=window)
        index = {name: position for position, name in enumerate(header)}
        distinct_by_day: dict[str, int] = {}
        for day in keep.values():
            distinct_by_day[day] = distinct_by_day.get(day, 0) + 1
        rows_by_day: dict[str, list[list[str]]] = {}
        step_rows = 0
        collected_rows = 0
        for parts in self._rows(dump, header):
            job_id = parts[index["JobID"]]
            step = is_step_row(job_id)
            if step and not include_steps:
                continue
            day = keep.get(base_job_id(job_id) if step else job_id)
            if day is None:
                continue
            rows_by_day.setdefault(day, []).append(self._fit(parts, len(header)))
            collected_rows += 1
            if step:
                step_rows += 1

        for day in sorted(rows_by_day):
            rows = rows_by_day[day]
            self.archive.write_page(
                f"jobs-{cloud}-{day}",
                {
                    "cloud": cloud,
                    "day": day,
                    "timezone": "Asia/Seoul",
                    "capture_profile": SLURM_CAPTURE_PROFILE,
                    "day_key_field": "End",
                    "columns": header,
                    "rows": rows,
                },
                endpoint=f"/api/download/jobs-raw-{cloud}",
                request={"cloud": cloud, "day": day, "projection": "end_date"},
                item_count=len(rows),
            )
            bucket = days.setdefault(day, {})
            # Distinct jobs, taken from the first pass rather than from the
            # archived rows: a repeated observation of one job is one job on
            # the day it ended, and counting rows here would inflate the
            # per-day figure a reader takes for "jobs that finished".
            bucket["job"] = bucket.get("job", 0) + distinct_by_day.get(day, 0)
            bucket["job_step"] = bucket.get("job_step", 0) + sum(
                1 for row in rows if is_step_row(row[index["JobID"]])
            )
            bucket["parent_row"] = bucket.get("parent_row", 0) + sum(
                1 for row in rows if not is_step_row(row[index["JobID"]])
            )

        if first["novel_states"]:
            self.archive.note_coverage(
                "slurm.unrecognised_finished_states_were_kept: a state outside the reference "
                "list was treated as finished and archived. Discarding it would be the "
                "whitelist mistake again."
            )
            self.archive.note_skip(
                "novel_states", cloud=cloud, states=dict(sorted(first["novel_states"].items()))
            )
        if first["finished_without_end"]:
            # The legacy script counted these and printed them. A printed
            # number is lost; a named coverage note is not.
            self.archive.note_coverage(
                "slurm.finished_without_end_timestamp: some jobs report a finished state with "
                "no End value, so there is no day to file them under. They are counted here "
                "rather than dropped silently."
            )
            self.archive.note_skip(
                "finished_without_end",
                cloud=cloud,
                jobs=first["finished_without_end"],
                states=dict(sorted(first["no_end_states"].items())),
            )
        if first["unknown_clusters"]:
            self.archive.note_skip(
                "cluster_not_mapped",
                cloud=cloud,
                clusters=dict(sorted(first["unknown_clusters"].items())),
            )

        parent_rows = collected_rows - step_rows
        return {
            "columns": len(header),
            "dump_rows": first["rows"],
            "parent_jobs": first["parent_jobs"],
            # Distinct jobs whose end date lands in the window. This is the
            # number that answers "how many jobs finished".
            "jobs_in_window": len(keep),
            # Every archived parent row, repeated observations included. Equal
            # to jobs_in_window only when no job was observed twice.
            "parent_rows_archived": parent_rows,
            "repeat_observations": parent_rows - len(keep),
            "step_rows_in_window": step_rows,
            "rows_archived": collected_rows,
            "days_with_rows": len(rows_by_day),
            "not_finished_skipped": first["not_finished"],
            "finished_without_end": first["finished_without_end"],
            "outside_window": first["outside_window"],
            "clusters": dict(sorted(first["clusters"].items())),
            "states": dict(sorted(first["states"].items())),
        }

    def _first_pass(
        self, cloud: str, dump: Path, *, window: Window
    ) -> tuple[list[str], dict[str, str], dict[str, Any]]:
        header = self._header(dump)
        index = {name: position for position, name in enumerate(header)}
        missing = [name for name in REQUIRED_COLUMNS if name not in index]
        if missing:
            raise SlurmSchemaError(f"{cloud}: dump lacks {', '.join(missing)}")

        keep: dict[str, str] = {}
        stats: dict[str, Any] = {
            "rows": 0,
            "parent_jobs": 0,
            "not_finished": 0,
            "finished_without_end": 0,
            "outside_window": 0,
            "states": Counter(),
            "novel_states": Counter(),
            "no_end_states": Counter(),
            "clusters": Counter(),
            "unknown_clusters": Counter(),
        }
        for parts in self._rows(dump, header):
            stats["rows"] += 1
            job_id = parts[index["JobID"]]
            if is_step_row(job_id):
                continue
            stats["parent_jobs"] += 1
            state = normalized_state(parts[index["State"]])
            stats["states"][state or "(empty)"] += 1
            if state in NOT_FINISHED:
                stats["not_finished"] += 1
                continue
            if state and state not in KNOWN_FINISHED:
                stats["novel_states"][state] += 1
            ended = parse_sacct_timestamp(parts[index["End"]])
            if ended is None:
                stats["finished_without_end"] += 1
                stats["no_end_states"][state or "(empty)"] += 1
                continue
            if not window.contains(ended):
                stats["outside_window"] += 1
                continue
            raw_cluster = parts[index["Cluster"]] or "?"
            if raw_cluster not in CLUSTER_MAP:
                stats["unknown_clusters"][raw_cluster] += 1
            stats["clusters"][CLUSTER_MAP.get(raw_cluster, raw_cluster)] += 1
            keep[job_id] = kst_day(ended) or window.start_date.isoformat()
        return header, keep, stats

    @staticmethod
    def _header(dump: Path) -> list[str]:
        with gzip.open(dump, "rt", encoding="utf-8", errors="replace") as handle:
            return handle.readline().rstrip("\n").split("|")

    @staticmethod
    def _rows(dump: Path, header: Sequence[str]) -> Iterator[list[str]]:
        width = len(header)
        with gzip.open(dump, "rt", encoding="utf-8", errors="replace") as handle:
            handle.readline()
            for line in handle:
                parts = line.rstrip("\n").split("|")
                if len(parts) < width:
                    # Short rows are padded rather than skipped: the legacy
                    # script did the same, and preserving the original over
                    # tidiness is the archive's whole point.
                    parts = parts + [""] * (width - len(parts))
                yield parts

    @staticmethod
    def _fit(parts: list[str], width: int) -> list[str]:
        if len(parts) < width:
            return parts + [""] * (width - len(parts))
        return parts[:width]


class SlurmSchemaError(RuntimeError):
    """The dump does not carry the columns the projection depends on."""


def make_slurm_collector(
    *,
    fetcher: DumpFetcher,
    archive_root: Path,
    environment: str,
    staging_root: Path | None = None,
    capture_density: str = "full",
    dry_run: bool = False,
    config_root: Path | None = None,
) -> tuple[RawArchive, SlurmCollector]:
    run_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:12]}"
    archive = RawArchive(
        archive_root,
        SLURM_SOURCE,
        run_id,
        environment,
        capture_profile=SLURM_CAPTURE_PROFILE,
        capture_density=capture_density,
        dry_run=dry_run,
        config_root=config_root,
    )
    return archive, SlurmCollector(
        fetcher, archive, staging_root=staging_root or archive_root / "staging" / SLURM_SOURCE
    )
