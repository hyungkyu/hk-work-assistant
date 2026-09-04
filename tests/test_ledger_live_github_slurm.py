"""GitHub and Slurm capture runs -> standard v1 ledger rows.

Driven through the real collectors with the scripted fakes from the collector
test modules, so the whole path is exercised: fake API or fake dump ->
immutable raw archive -> run manifest -> ledger records. No network call is
made anywhere in this file.

The properties worth pinning here are the ones that would otherwise be found
in production: that a commit lands on the KST day it was made rather than the
night the backfill ran, that a repository mirrored under two names is two
scopes rather than one lost row, that Slurm step rows are reported as
unconverted instead of vanishing, and that converting a run twice produces
exactly the same rows.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from test_github_collector import (  # noqa: E402
    FakeMirrors,
    FakeRest,
    commit as mirror_commit,
    repository,
    rest_commit,
)
from test_slurm_collector import FakeFetcher, HEADER, row, write_dump  # noqa: E402

from rlwrld_worklog.archive import RawArchive  # noqa: E402
from rlwrld_worklog.github_collector import GithubCollector, Window  # noqa: E402
from rlwrld_worklog.ledger.live import LiveConvertResult, _STREAMS, convert_live_run  # noqa: E402
from rlwrld_worklog.ledger.schema import validate_record  # noqa: E402
from rlwrld_worklog.slurm_collector import SLURM_SOURCE, SlurmCollector  # noqa: E402

ORG = "example-org"


def _archive(tmp_path: Path, source: str, run_id: str) -> RawArchive:
    return RawArchive(
        tmp_path, source, run_id, "test", config_root=tmp_path / "config"
    )


def capture_github(tmp_path: Path, *, rest: FakeRest, mirrors: FakeMirrors, **kwargs: Any) -> Path:
    archive = _archive(tmp_path, "github", "gh-run-1")
    collector = GithubCollector(rest, mirrors, archive, organization=ORG)
    options: dict[str, Any] = {"kinds": (), "backfill": True}
    options.update(kwargs)
    result = collector.collect(window=Window.parse("2026-08-01", "2026-08-31"), **options)
    return result.manifest_path


def capture_slurm(tmp_path: Path, dumps: dict[str, Path], **kwargs: Any) -> Path:
    archive = _archive(tmp_path, SLURM_SOURCE, "slurm-run-1")
    collector = SlurmCollector(FakeFetcher(dumps), archive, staging_root=tmp_path / "staging")
    options: dict[str, Any] = {"clouds": tuple(dumps), "backfill": True}
    options.update(kwargs)
    result = collector.collect(window=Window.parse("2026-08-01", "2026-08-31"), **options)
    return result.manifest_path


def convert(tmp_path: Path, manifest_path: Path, source: str) -> Any:
    return convert_live_run(
        archive_root=tmp_path,
        manifest_path=manifest_path,
        out_root=tmp_path / "ledger-out",
        source=source,
    )


def rows_for(tmp_path: Path, manifest_path: Path, source: str) -> list[Any]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    result = LiveConvertResult(source=source, run_id=str(manifest["run_id"]))
    return list(_STREAMS[source](tmp_path, manifest, result)), result


# ------------------------------------------------------------------ github


class TestGithubConversion:
    def test_a_mirror_commit_becomes_one_valid_record(self, tmp_path: Path) -> None:
        rest = FakeRest(repositories=[[repository("alpha")]])
        mirrors = FakeMirrors({"alpha": [mirror_commit("a" * 40, "2026-08-05T05:00:00Z")]})
        manifest = capture_github(tmp_path, rest=rest, mirrors=mirrors)

        records, _ = rows_for(tmp_path, manifest, "github")

        commits = [record for record in records if record.entity_type == "commit"]
        assert len(commits) == 1
        record = commits[0]
        assert validate_record(record.to_dict()) == []
        assert record.source == "github"
        assert record.capture_profile == "live-github-commit-from-mirror/v1"
        assert record.source_entity_id == f"alpha:{'a' * 40}"
        assert record.scope["repository"] == "alpha"
        assert record.tenant["workspace_id"] == ORG
        # The body survives into the row, which the legacy collector lost.
        assert "longer body" in record.raw_payload["body"]

    def test_a_commit_is_filed_on_its_kst_day_not_the_run_day(self, tmp_path: Path) -> None:
        # 2026-08-01T20:00Z is already the 2nd in Seoul. Taking the window
        # from the run's finish time would file a whole month of commits under
        # the night the backfill happened to run.
        rest = FakeRest(repositories=[[repository("alpha")]])
        mirrors = FakeMirrors({"alpha": [mirror_commit("b" * 40, "2026-08-01T20:00:00Z")]})
        manifest = capture_github(tmp_path, rest=rest, mirrors=mirrors)

        records, _ = rows_for(tmp_path, manifest, "github")

        commit_record = next(record for record in records if record.entity_type == "commit")
        assert commit_record.observation_window["start"] == "2026-08-02"
        assert commit_record.observation_window["tz"] == "Asia/Seoul"

    def test_a_rest_commit_lands_on_the_same_day_as_a_mirror_commit(self, tmp_path: Path) -> None:
        # The two shapes nest their dates differently. If the accessor missed
        # the REST shape, an unmirrored repository's commits would be filed on
        # a different day than everyone else's.
        rest = FakeRest(
            repositories=[[repository("unmirrored")]],
            commits={"unmirrored": [rest_commit("c" * 40, "2026-08-01T20:00:00Z")]},
        )
        manifest = capture_github(tmp_path, rest=rest, mirrors=FakeMirrors({}))

        records, _ = rows_for(tmp_path, manifest, "github")

        commit_record = next(record for record in records if record.entity_type == "commit")
        assert commit_record.observation_window["start"] == "2026-08-02"
        assert commit_record.capture_profile == "live-github-rest/v1"
        assert commit_record.source_updated_at is not None

    def test_the_five_rest_kinds_each_convert(self, tmp_path: Path) -> None:
        rest = FakeRest(
            repositories=[[repository("alpha")]],
            pull_requests={
                "alpha": [[{"number": 7, "updated_at": "2026-08-05T05:00:00Z", "title": "pr"}]]
            },
            reviews={
                ("alpha", 7): [{"id": 11, "state": "APPROVED", "submitted_at": "2026-08-05T06:00:00Z"}]
            },
            review_comments={"alpha": [{"id": 21, "created_at": "2026-08-05T07:00:00Z", "path": "a.py"}]},
            issue_comments={"alpha": [{"id": 31, "created_at": "2026-08-05T08:00:00Z"}]},
            issues={"alpha": [{"number": 41, "updated_at": "2026-08-05T09:00:00Z", "title": "issue"}]},
        )
        manifest = capture_github(
            tmp_path,
            rest=rest,
            mirrors=FakeMirrors({}),
            include_commits=False,
            kinds=("pull_request", "review", "review_comment", "issue_comment", "issue"),
        )

        records, result = rows_for(tmp_path, manifest, "github")

        counts = {}
        for record in records:
            counts[record.entity_type] = counts.get(record.entity_type, 0) + 1
            assert validate_record(record.to_dict()) == []
        assert counts["pull_request"] == 1
        assert counts["review"] == 1
        assert counts["review_comment"] == 1
        assert counts["issue_comment"] == 1
        assert counts["issue"] == 1
        assert result.unhandled_kinds == {}

    def test_a_repository_becomes_a_dimension_record(self, tmp_path: Path) -> None:
        rest = FakeRest(repositories=[[repository("alpha", archived=True)]])
        manifest = capture_github(tmp_path, rest=rest, mirrors=FakeMirrors({"alpha": []}))

        records, _ = rows_for(tmp_path, manifest, "github")

        repositories = [record for record in records if record.entity_type == "repository"]
        assert len(repositories) == 1
        assert repositories[0].source_entity_id == f"{ORG}/alpha"
        assert repositories[0].relations["archived"] is True
        assert repositories[0].scope["kind"] == "github_organization"

    def test_one_commit_under_two_repository_names_is_two_scoped_rows(self, tmp_path: Path) -> None:
        # A renamed repository stays reachable under its old name, so it is
        # mirrored twice. The commit is the same object but it is a real
        # observation in each container, and the repository is part of a
        # commit's natural key. Collapsing them would drop an observation;
        # counting them as two commits is what the collector's own counter
        # already distinguishes.
        shared = mirror_commit("d" * 40, "2026-08-05T05:00:00Z")
        rest = FakeRest(repositories=[[repository("new-name"), repository("old-name")]])
        mirrors = FakeMirrors({"new-name": [shared], "old-name": [shared]})
        manifest = capture_github(tmp_path, rest=rest, mirrors=mirrors)

        records, _ = rows_for(tmp_path, manifest, "github")

        commits = [record for record in records if record.entity_type == "commit"]
        assert len(commits) == 2
        assert {record.scope["repository"] for record in commits} == {"new-name", "old-name"}
        assert len({record.ledger_id for record in commits}) == 2

    def test_an_unknown_page_kind_is_counted_not_dropped(self, tmp_path: Path) -> None:
        archive = _archive(tmp_path, "github", "gh-run-odd")
        archive.set_requested_window({"since": "2026-08-01", "until": "2026-08-31"})
        archive.write_page("something-new", {"items": [{"id": 1}]}, endpoint="/x", item_count=1)
        manifest_path = archive.finish({"status": "success", "organization": ORG})

        records, result = rows_for(tmp_path, manifest_path, "github")

        assert records == []
        assert result.unhandled_kinds == {"something-new": 1}

    def test_an_edited_raw_page_aborts_the_conversion(self, tmp_path: Path) -> None:
        # The manifest is an index; the bytes are the authority. A page that
        # no longer matches its recorded hash must never become a row.
        rest = FakeRest(repositories=[[repository("alpha")]])
        mirrors = FakeMirrors({"alpha": [mirror_commit("e" * 40, "2026-08-05T05:00:00Z")]})
        manifest_path = capture_github(tmp_path, rest=rest, mirrors=mirrors)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        target = tmp_path / manifest["files"][0]["path"]
        target.write_bytes(target.read_bytes() + b"tampered")

        with pytest.raises(ValueError, match="does not match the manifest hash"):
            list(rows_for(tmp_path, manifest_path, "github")[0])


# ------------------------------------------------------------------- slurm


class TestSlurmConversion:
    def test_a_finished_job_becomes_one_valid_record(self, tmp_path: Path) -> None:
        dump = write_dump(tmp_path / "dumps" / "kakao.psv.gz", [row("101")])
        manifest = capture_slurm(tmp_path, {"kakao": dump})

        records, _ = rows_for(tmp_path, manifest, "slurm")

        assert len(records) == 1
        record = records[0]
        assert validate_record(record.to_dict()) == []
        assert record.source == "slurm"
        assert record.entity_type == "job"
        assert record.source_entity_id == "kakao:101"
        assert record.capture_profile == "live-slurm-sacct-dump/v1"
        # Every column reaches the row; nothing is reduced on the way in.
        assert set(record.raw_payload) == set(HEADER)

    def test_the_day_comes_from_the_page_not_the_run(self, tmp_path: Path) -> None:
        dump = write_dump(
            tmp_path / "dumps" / "kakao.psv.gz",
            [row("101", end="2026-08-07T10:00:00")],
        )
        manifest = capture_slurm(tmp_path, {"kakao": dump})

        records, _ = rows_for(tmp_path, manifest, "slurm")

        assert records[0].observation_window["start"] == "2026-08-07"
        assert records[0].observation_window["tz"] == "Asia/Seoul"

    def test_step_rows_are_reported_as_unconverted(self, tmp_path: Path) -> None:
        # They hold the only real resource usage and stay in the archive, but
        # they are not activities and have no entity type yet. Saying so is
        # the difference between a decision and a silent omission.
        dump = write_dump(
            tmp_path / "dumps" / "kakao.psv.gz",
            [row("101"), row("101.batch", max_rss="1024K"), row("101.extern")],
        )
        manifest = capture_slurm(tmp_path, {"kakao": dump})

        records, result = rows_for(tmp_path, manifest, "slurm")

        assert len(records) == 1
        assert result.unhandled_kinds == {"slurm_step_rows_not_converted": 2}

    def test_a_cluster_keeps_the_name_sacct_reported(self, tmp_path: Path) -> None:
        # The collector maps cluster names for output folders. A ledger row
        # holds what the source said, so a consumer can still tell them apart.
        dump = write_dump(tmp_path / "dumps" / "naver.psv.gz", [row("201", cluster="mlxp")])
        manifest = capture_slurm(tmp_path, {"naver": dump})

        records, _ = rows_for(tmp_path, manifest, "slurm")

        assert records[0].scope["cluster"] == "mlxp"
        assert records[0].scope["cluster_naming"] == "as_reported_by_sacct"

    def test_an_absent_submit_time_is_recorded_as_lossy_not_invented(self, tmp_path: Path) -> None:
        # naver reports no Submit on any job. The row says so rather than
        # borrowing another timestamp silently.
        dump = write_dump(
            tmp_path / "dumps" / "naver.psv.gz",
            [row("201", cluster="mlxp", submit="", state="SUCCEEDED")],
        )
        manifest = capture_slurm(tmp_path, {"naver": dump})

        records, _ = rows_for(tmp_path, manifest, "slurm")

        record = records[0]
        assert record.capture_completeness["lossy_fields"] == {"submit_time": "absent for this cluster"}
        assert record.relations["submit"] is None
        assert record.source_updated_at is not None

    def test_more_than_one_cloud_converts_in_one_run(self, tmp_path: Path) -> None:
        dumps = {
            "kakao": write_dump(tmp_path / "dumps" / "kakao.psv.gz", [row("101")]),
            "naver": write_dump(tmp_path / "dumps" / "naver.psv.gz", [row("201", cluster="mlxp")]),
        }
        manifest = capture_slurm(tmp_path, dumps)

        records, _ = rows_for(tmp_path, manifest, "slurm")

        assert {record.tenant["workspace_id"] for record in records} == {"kakao", "naver"}


# ------------------------------------------------------------- idempotency


class TestIdempotency:
    @pytest.mark.parametrize("source", ["github", "slurm"])
    def test_converting_twice_gives_the_same_rows(self, tmp_path: Path, source: str) -> None:
        if source == "github":
            rest = FakeRest(repositories=[[repository("alpha")]])
            mirrors = FakeMirrors({"alpha": [mirror_commit("f" * 40, "2026-08-05T05:00:00Z")]})
            manifest = capture_github(tmp_path, rest=rest, mirrors=mirrors)
        else:
            dump = write_dump(tmp_path / "dumps" / "kakao.psv.gz", [row("101")])
            manifest = capture_slurm(tmp_path, {"kakao": dump})

        first, _ = rows_for(tmp_path, manifest, source)
        second, _ = rows_for(tmp_path, manifest, source)

        assert [record.ledger_id for record in first] == [record.ledger_id for record in second]
        assert [record.to_dict() for record in first] == [record.to_dict() for record in second]

    def test_the_written_file_is_byte_identical_across_conversions(self, tmp_path: Path) -> None:
        dump = write_dump(tmp_path / "dumps" / "kakao.psv.gz", [row("101"), row("102")])
        manifest = capture_slurm(tmp_path, {"kakao": dump})

        first = convert(tmp_path, manifest, "slurm")
        first_bytes = Path(first.output_path).read_bytes()
        second = convert(tmp_path, manifest, "slurm")
        second_bytes = Path(second.output_path).read_bytes()

        assert first.records_written == second.records_written == 2
        assert first_bytes == second_bytes
