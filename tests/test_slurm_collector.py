"""Slurm capture: End-based day keys, blacklist state handling, 117 columns.

The dumps are written to a temporary directory by the test itself and a fake
fetcher hands them over, so nothing here reaches the infra node or S3. Every
job id, user and cluster name is invented.

The three regression tests at the end each fail against the behaviour the
legacy tooling started with.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any, Sequence

import pytest

from rlwrld_worklog.archive import RawArchive
from rlwrld_worklog.github_collector import Window
from rlwrld_worklog.slurm_collector import (
    CLUSTER_MAP,
    SLURM_SOURCE,
    SlurmCollector,
    base_job_id,
    is_step_row,
    normalized_state,
    parse_sacct_timestamp,
)

# A trimmed stand-in for the 117-column export. The projection depends on four
# columns by name, never by position, so a shorter header exercises the same
# code path -- and a test that a wider header also works is included below.
HEADER = ["Account", "Cluster", "JobID", "JobName", "State", "Submit", "Start", "End", "MaxRSS"]


def row(
    job_id: str,
    *,
    state: str = "COMPLETED",
    end: str = "2026-08-01T10:00:00",
    submit: str = "2026-08-01T09:00:00",
    cluster: str = "deepops",
    max_rss: str = "",
) -> list[str]:
    return [
        "p-rlwrld",
        cluster,
        job_id,
        f"job-{job_id}",
        state,
        submit,
        "2026-08-01T09:30:00",
        end,
        max_rss,
    ]


def write_dump(path: Path, rows: Sequence[Sequence[str]], *, header: Sequence[str] = HEADER) -> Path:
    body = "|".join(header) + "\n" + "\n".join("|".join(entry) for entry in rows) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        handle.write(body)
    return path


class FakeFetcher:
    """Serves prepared dumps, and records that the file is removed after use."""

    def __init__(self, dumps: dict[str, Path], *, failing: dict[str, Exception] | None = None) -> None:
        self.dumps = dumps
        self.failing = failing or {}
        self.fetched: list[str] = []
        self.destinations: list[Path] = []

    def fetch(self, cloud: str, destination: Path) -> dict[str, Any]:
        if cloud in self.failing:
            raise self.failing[cloud]
        source = self.dumps.get(cloud)
        if source is None:
            raise FileNotFoundError(cloud)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())
        self.fetched.append(cloud)
        self.destinations.append(destination)
        return {
            "dump_bytes": destination.stat().st_size,
            "dump_sha256": "0" * 64,
            "dump_source": f"http://infra-node.invalid:8888/api/download/jobs-raw-{cloud}",
        }


def _archive(tmp_path: Path, **overrides: Any) -> RawArchive:
    options: dict[str, Any] = {"config_root": tmp_path / "config"}
    options.update(overrides)
    return RawArchive(tmp_path / "archive", SLURM_SOURCE, "run-1", "test", **options)


def _collector(tmp_path: Path, fetcher: FakeFetcher, **overrides: Any) -> tuple[RawArchive, SlurmCollector]:
    archive = _archive(tmp_path, **overrides)
    return archive, SlurmCollector(fetcher, archive, staging_root=tmp_path / "staging")


def read_pages(archive: RawArchive) -> list[dict[str, Any]]:
    return [
        json.loads(gzip.decompress((archive.root / entry["path"]).read_bytes()).decode())
        for entry in archive.files
    ]


# ------------------------------------------------------------------ helpers


class TestHelpers:
    def test_a_step_row_is_recognised_and_reduced_to_its_parent(self) -> None:
        assert is_step_row("12223.batch") is True
        assert is_step_row("12223_5.extern") is True
        assert is_step_row("12223") is False
        assert base_job_id("12223.batch") == "12223"
        assert base_job_id("12223_5.extern") == "12223_5"
        assert base_job_id("12223") == "12223"

    def test_a_state_keeps_only_its_first_word(self) -> None:
        assert normalized_state("CANCELLED by 1234") == "CANCELLED"
        assert normalized_state("COMPLETED") == "COMPLETED"
        assert normalized_state("") == ""
        assert normalized_state(None) == ""

    def test_a_naive_timestamp_is_read_as_kst(self) -> None:
        parsed = parse_sacct_timestamp("2026-08-01T10:00:00")
        assert parsed is not None
        assert parsed.utcoffset().total_seconds() == 9 * 3600

    def test_an_offset_bearing_timestamp_keeps_its_offset(self) -> None:
        parsed = parse_sacct_timestamp("2026-08-16T19:30:01+00:00")
        assert parsed is not None
        assert parsed.utcoffset().total_seconds() == 0

    def test_the_placeholder_values_sacct_writes_are_not_dates(self) -> None:
        for value in ("", "   ", "Unknown", "None", "N/A", "NONE", "garbage"):
            assert parse_sacct_timestamp(value) is None

    def test_the_cluster_map_covers_the_names_the_fleet_reports(self) -> None:
        assert CLUSTER_MAP["mlxp"] == "naver_mlxp"
        assert CLUSTER_MAP["deepops"] == "kakao"
        # Added by this port; it previously fell through under its raw name.
        assert CLUSTER_MAP["rlwrld-26q3"] == "rlwrld_26q3"


# --------------------------------------------------------------- collection


class TestCollect:
    def test_jobs_are_filed_under_the_kst_day_they_ended(self, tmp_path: Path) -> None:
        dump = write_dump(
            tmp_path / "dumps" / "kakao.psv.gz",
            [
                row("1", end="2026-08-01T10:00:00"),
                row("2", end="2026-08-02T23:59:00"),
            ],
        )
        fetcher = FakeFetcher({"kakao": dump})
        archive, collector = _collector(tmp_path, fetcher)

        result = collector.collect(
            window=Window.parse("2026-08-01", "2026-08-02"), clouds=("kakao",), backfill=True
        )

        assert result.jobs == 2
        assert result.days["2026-08-01"]["job"] == 1
        assert result.days["2026-08-02"]["job"] == 1
        pages = read_pages(archive)
        assert {page["day"] for page in pages} == {"2026-08-01", "2026-08-02"}
        assert all(page["day_key_field"] == "End" for page in pages)

    def test_all_columns_are_preserved_as_a_header_plus_rows(self, tmp_path: Path) -> None:
        wide = HEADER + [f"Extra{index}" for index in range(108)]
        entry = row("1") + [""] * 108
        dump = write_dump(tmp_path / "dumps" / "kakao.psv.gz", [entry], header=wide)
        fetcher = FakeFetcher({"kakao": dump})
        archive, collector = _collector(tmp_path, fetcher)

        collector.collect(window=Window.parse("2026-08-01"), clouds=("kakao",), backfill=True)

        page = read_pages(archive)[0]
        assert len(page["columns"]) == 117
        assert len(page["rows"][0]) == 117
        # Keys are not repeated per row: that costs three to four times the space.
        assert isinstance(page["rows"][0], list)

    def test_a_step_row_follows_its_parents_day(self, tmp_path: Path) -> None:
        dump = write_dump(
            tmp_path / "dumps" / "kakao.psv.gz",
            [
                row("7", end="2026-08-02T01:00:00"),
                # The step row's own End is on another day and is ignored.
                row("7.batch", end="2026-08-01T01:00:00", max_rss="1024K"),
                row("7.extern", end=""),
            ],
        )
        fetcher = FakeFetcher({"kakao": dump})
        archive, collector = _collector(tmp_path, fetcher)

        result = collector.collect(
            window=Window.parse("2026-08-01", "2026-08-02"), clouds=("kakao",), backfill=True
        )

        assert result.jobs == 1
        assert result.step_rows == 2
        assert result.days["2026-08-02"]["job_step"] == 2
        assert "job_step" not in result.days["2026-08-01"]
        page = next(page for page in read_pages(archive) if page["day"] == "2026-08-02")
        assert len(page["rows"]) == 3

    def test_steps_can_be_excluded_without_losing_the_parent(self, tmp_path: Path) -> None:
        dump = write_dump(
            tmp_path / "dumps" / "kakao.psv.gz",
            [row("7"), row("7.batch", max_rss="1024K")],
        )
        archive, collector = _collector(tmp_path, FakeFetcher({"kakao": dump}))

        result = collector.collect(
            window=Window.parse("2026-08-01"), clouds=("kakao",), backfill=True, include_steps=False
        )

        assert result.jobs == 1
        assert result.step_rows == 0

    def test_a_job_observed_twice_is_one_job_and_two_archived_rows(self, tmp_path: Path) -> None:
        """The dump repeats a job across snapshots; the counts must not.

        On the real August kakao dump this difference was 8,995 rows. Both
        numbers matter -- every observation belongs in the archive, and only
        one of them is a job that finished -- so both are reported, and the
        one called "jobs" is the distinct count.
        """
        dump = write_dump(
            tmp_path / "dumps" / "kakao.psv.gz",
            [
                # An earlier observation of job 5, before it finished. The
                # first pass rejects it; the second pass still archives it,
                # because it is a row belonging to a job that was kept.
                row("5", state="RUNNING", end=""),
                row("5", state="COMPLETED", end="2026-08-01T10:00:00"),
                row("6", state="COMPLETED", end="2026-08-01T11:00:00"),
            ],
        )
        archive, collector = _collector(tmp_path, FakeFetcher({"kakao": dump}))

        result = collector.collect(window=Window.parse("2026-08-01"), clouds=("kakao",), backfill=True)

        assert result.jobs == 2
        assert result.parent_rows == 3
        assert result.counters["repeat_observations"] == 1
        per_cloud = result.counters["per_cloud"]["kakao"]
        assert per_cloud["jobs_in_window"] == 2
        assert per_cloud["parent_rows_archived"] == 3
        assert per_cloud["repeat_observations"] == 1
        # The day figure a reader takes for "jobs that finished" stays 2.
        assert result.days["2026-08-01"]["job"] == 2
        assert result.days["2026-08-01"]["parent_row"] == 3
        # Every row reached the archive: nothing was dropped to make the
        # counts agree.
        page = read_pages(archive)[0]
        assert len(page["rows"]) == 3
        manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
        assert manifest["jobs"] == 2
        assert manifest["parent_rows_archived"] == 3

    def test_without_repeats_the_two_counts_agree(self, tmp_path: Path) -> None:
        dump = write_dump(tmp_path / "dumps" / "kakao.psv.gz", [row("1"), row("2")])
        archive, collector = _collector(tmp_path, FakeFetcher({"kakao": dump}))

        result = collector.collect(window=Window.parse("2026-08-01"), clouds=("kakao",), backfill=True)

        assert result.jobs == result.parent_rows == 2
        assert result.counters["repeat_observations"] == 0

    def test_a_running_job_is_not_captured(self, tmp_path: Path) -> None:
        dump = write_dump(
            tmp_path / "dumps" / "kakao.psv.gz",
            [row("1", state="RUNNING", end=""), row("2", state="PENDING", end="")],
        )
        archive, collector = _collector(tmp_path, FakeFetcher({"kakao": dump}))

        result = collector.collect(window=Window.parse("2026-08-01"), clouds=("kakao",), backfill=True)

        assert result.jobs == 0
        assert result.counters["per_cloud"]["kakao"]["not_finished_skipped"] == 2
        # Not counted as "finished but undatable": they are simply not done.
        assert result.counters["per_cloud"]["kakao"]["finished_without_end"] == 0

    def test_a_finished_job_without_an_end_value_is_counted_not_dropped(self, tmp_path: Path) -> None:
        dump = write_dump(
            tmp_path / "dumps" / "kakao.psv.gz",
            [row("1", state="COMPLETED", end=""), row("2", state="FAILED", end="Unknown")],
        )
        archive, collector = _collector(tmp_path, FakeFetcher({"kakao": dump}))

        result = collector.collect(window=Window.parse("2026-08-01"), clouds=("kakao",), backfill=True)

        assert result.jobs == 0
        assert result.counters["per_cloud"]["kakao"]["finished_without_end"] == 2
        assert any("finished_without_end_timestamp" in note for note in archive.coverage_notes)
        skip = next(entry for entry in archive.skips if entry["kind"] == "finished_without_end")
        assert skip["states"] == {"COMPLETED": 1, "FAILED": 1}

    def test_an_unmapped_cluster_is_reported_and_kept_under_its_raw_name(self, tmp_path: Path) -> None:
        dump = write_dump(tmp_path / "dumps" / "kakao.psv.gz", [row("1", cluster="brand-new")])
        archive, collector = _collector(tmp_path, FakeFetcher({"kakao": dump}))

        result = collector.collect(window=Window.parse("2026-08-01"), clouds=("kakao",), backfill=True)

        assert result.jobs == 1
        assert result.counters["per_cloud"]["kakao"]["clusters"] == {"brand-new": 1}
        assert any(entry["kind"] == "cluster_not_mapped" for entry in archive.skips)

    def test_a_missing_required_column_is_refused_rather_than_guessed(self, tmp_path: Path) -> None:
        header = [name for name in HEADER if name != "End"]
        rows = [[value for name, value in zip(HEADER, row("1")) if name != "End"]]
        dump = write_dump(tmp_path / "dumps" / "kakao.psv.gz", rows, header=header)
        archive, collector = _collector(tmp_path, FakeFetcher({"kakao": dump}))

        result = collector.collect(window=Window.parse("2026-08-01"), clouds=("kakao",), backfill=True)

        assert result.jobs == 0
        assert result.clouds_collected == ()
        error = next(entry for entry in archive.errors if entry["kind"] == "schema_changed")
        assert "End" in error["detail"]

    def test_one_unavailable_cloud_does_not_lose_the_others(self, tmp_path: Path) -> None:
        kakao = write_dump(tmp_path / "dumps" / "kakao.psv.gz", [row("1")])
        fetcher = FakeFetcher(
            {"kakao": kakao}, failing={"naver": RuntimeError("unreachable")}
        )
        archive, collector = _collector(tmp_path, fetcher)

        result = collector.collect(
            window=Window.parse("2026-08-01"), clouds=("kakao", "naver"), backfill=True
        )

        assert result.clouds_collected == ("kakao",)
        assert result.jobs == 1
        assert any(entry["kind"] == "dump_unavailable" for entry in archive.skips)
        manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
        assert manifest["status"] == "success_with_skips"

    def test_every_cloud_failing_is_a_failed_run_not_an_empty_success(self, tmp_path: Path) -> None:
        fetcher = FakeFetcher({}, failing={"kakao": RuntimeError("unreachable")})
        archive, collector = _collector(tmp_path, fetcher)

        result = collector.collect(window=Window.parse("2026-08-01"), clouds=("kakao",), backfill=True)

        manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
        assert manifest["status"] == "failed"

    def test_the_downloaded_dump_is_removed_after_projection(self, tmp_path: Path) -> None:
        dump = write_dump(tmp_path / "dumps" / "kakao.psv.gz", [row("1")])
        fetcher = FakeFetcher({"kakao": dump})
        archive, collector = _collector(tmp_path, fetcher)

        collector.collect(window=Window.parse("2026-08-01"), clouds=("kakao",), backfill=True)

        assert fetcher.destinations and not fetcher.destinations[0].exists()
        # The original dump is untouched; only the staged copy goes.
        assert dump.exists()

    def test_no_presigned_url_reaches_the_manifest(self, tmp_path: Path) -> None:
        dump = write_dump(tmp_path / "dumps" / "kakao.psv.gz", [row("1")])

        class LeakyFetcher(FakeFetcher):
            def fetch(self, cloud: str, destination: Path) -> dict[str, Any]:
                facts = super().fetch(cloud, destination)
                facts["url"] = "https://s3.invalid/dump?X-Amz-Signature=leaked"
                facts["presigned_url"] = "https://s3.invalid/dump?X-Amz-Signature=leaked"
                return facts

        archive, collector = _collector(tmp_path, LeakyFetcher({"kakao": dump}))
        result = collector.collect(window=Window.parse("2026-08-01"), clouds=("kakao",), backfill=True)

        text = result.manifest_path.read_text(encoding="utf-8")
        assert "X-Amz-Signature" not in text
        assert "leaked" not in text

    def test_a_window_before_a_clouds_retention_floor_says_so(self, tmp_path: Path) -> None:
        dump = write_dump(tmp_path / "dumps" / "naver.psv.gz", [row("1", cluster="mlxp")])
        archive, collector = _collector(tmp_path, FakeFetcher({"naver": dump}))

        collector.collect(
            window=Window.parse("2026-01-01", "2026-08-01"), clouds=("naver",), backfill=True
        )

        note = next(note for note in archive.coverage_notes if "api_retention_floor" in note)
        # The key is constant so the rule registry can declare it; the cloud
        # and the floor date live in the message.
        assert note.startswith("slurm.api_retention_floor:")
        assert "naver" in note and "2026-03-26" in note

    def test_a_window_inside_retention_adds_no_such_note(self, tmp_path: Path) -> None:
        dump = write_dump(tmp_path / "dumps" / "naver.psv.gz", [row("1", cluster="mlxp")])
        archive, collector = _collector(tmp_path, FakeFetcher({"naver": dump}))

        collector.collect(window=Window.parse("2026-08-01"), clouds=("naver",), backfill=True)

        assert not any("api_retention_floor" in note for note in archive.coverage_notes)


# ---------------------------------------------------------------- watermark


class TestCheckpoint:
    def test_a_backfill_neither_reads_nor_moves_the_watermark(self, tmp_path: Path) -> None:
        seed = _archive(tmp_path)
        seed.write_checkpoint(
            {"schema_version": 1, "source": SLURM_SOURCE, "run_id": "earlier", "collected_through": "2026-08-20"}
        )
        dump = write_dump(tmp_path / "dumps" / "kakao.psv.gz", [row("1")])
        archive, collector = _collector(tmp_path, FakeFetcher({"kakao": dump}))

        result = collector.collect(window=Window.parse("2026-08-01"), clouds=("kakao",), backfill=True)

        assert result.checkpoint_advanced is False
        stored = json.loads(archive.checkpoint_path.read_text(encoding="utf-8"))
        assert stored["collected_through"] == "2026-08-20"

    def test_a_complete_incremental_run_advances_it(self, tmp_path: Path) -> None:
        dump = write_dump(tmp_path / "dumps" / "kakao.psv.gz", [row("1")])
        archive, collector = _collector(tmp_path, FakeFetcher({"kakao": dump}))

        result = collector.collect(window=Window.parse("2026-08-01"), clouds=("kakao",))

        assert result.checkpoint_advanced is True
        stored = json.loads(archive.checkpoint_path.read_text(encoding="utf-8"))
        assert stored["collected_through"] == "2026-08-01"

    def test_a_partial_run_holds_the_watermark_back(self, tmp_path: Path) -> None:
        kakao = write_dump(tmp_path / "dumps" / "kakao.psv.gz", [row("1")])
        fetcher = FakeFetcher({"kakao": kakao}, failing={"naver": RuntimeError("unreachable")})
        archive, collector = _collector(tmp_path, fetcher)

        result = collector.collect(window=Window.parse("2026-08-01"), clouds=("kakao", "naver"))

        assert result.checkpoint_advanced is False
        assert any("checkpoint_held_back_on_partial_run" in note for note in archive.coverage_notes)


# ------------------------------------------------- legacy regression tests


class TestLegacyLossesAreNotPorted:
    def test_a_cluster_with_no_submit_time_is_not_lost(self, tmp_path: Path) -> None:
        # naver (mlxp) reports an empty Submit on all 17,395 of its jobs while
        # End is always present. Keying on Submit deletes the cluster whole.
        dump = write_dump(
            tmp_path / "dumps" / "naver.psv.gz",
            [
                row("1", cluster="mlxp", submit="", state="SUCCEEDED", end="2026-08-01T19:30:01+00:00"),
                row("2", cluster="mlxp", submit="", state="SUCCEEDED", end="2026-08-01T20:00:00+00:00"),
            ],
        )
        archive, collector = _collector(tmp_path, FakeFetcher({"naver": dump}))

        result = collector.collect(
            window=Window.parse("2026-08-01", "2026-08-02"), clouds=("naver",), backfill=True
        )

        assert result.jobs == 2
        assert result.counters["per_cloud"]["naver"]["clusters"] == {"naver_mlxp": 2}

    def test_an_unrecognised_finished_state_is_kept(self, tmp_path: Path) -> None:
        # The whitelist era dropped 6,836 SUCCEEDED jobs because clusters do
        # not agree on state names. Unknown means keep and report.
        dump = write_dump(
            tmp_path / "dumps" / "kakao.psv.gz",
            [
                row("1", state="SUCCEEDED"),
                row("2", state="SOMETHING_NEW"),
                row("3", state=""),
            ],
        )
        archive, collector = _collector(tmp_path, FakeFetcher({"kakao": dump}))

        result = collector.collect(window=Window.parse("2026-08-01"), clouds=("kakao",), backfill=True)

        assert result.jobs == 3
        novel = next(entry for entry in archive.skips if entry["kind"] == "novel_states")
        assert novel["states"] == {"SOMETHING_NEW": 1}
        # An empty state is kept too, and shows up in the state census.
        assert result.counters["per_cloud"]["kakao"]["states"]["(empty)"] == 1

    def test_no_derived_metric_is_computed_into_the_archive(self, tmp_path: Path) -> None:
        # HK's rule: a definition that changes later cannot be recovered from
        # a value that was already reduced. The archive holds original strings.
        dump = write_dump(tmp_path / "dumps" / "kakao.psv.gz", [row("1", max_rss="2048K")])
        archive, collector = _collector(tmp_path, FakeFetcher({"kakao": dump}))

        collector.collect(window=Window.parse("2026-08-01"), clouds=("kakao",), backfill=True)

        page = read_pages(archive)[0]
        assert set(page) == {
            "cloud",
            "day",
            "timezone",
            "capture_profile",
            "day_key_field",
            "columns",
            "rows",
        }
        assert page["rows"][0][page["columns"].index("MaxRSS")] == "2048K"
