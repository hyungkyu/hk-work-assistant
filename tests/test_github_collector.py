"""GitHub capture: window arithmetic, day keys, and the legacy losses it drops.

A scripted fake REST client and a scripted fake mirror stand in for GitHub and
for git: nothing here touches the network, a real repository, or the real
mirror directory. Every identifier, login and message is invented.

The four regression tests at the end are the point of the port. Each one fails
against the behaviour of `legacy/claude/weekly/scripts/github_daily_collect.py`.
"""

from __future__ import annotations

import gzip
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from rlwrld_worklog.archive import RawArchive
from rlwrld_worklog.github_collector import (
    GITHUB_SOURCE,
    KST,
    GithubCollector,
    GitHubApiError,
    Window,
    kst_day,
    parse_timestamp,
)

ORG = "example-org"


def _archive(tmp_path: Path, **overrides: Any) -> RawArchive:
    options: dict[str, Any] = {
        "capture_profile": "live-github-api/v1",
        "config_root": tmp_path / "config",
    }
    options.update(overrides)
    return RawArchive(tmp_path / "archive", GITHUB_SOURCE, "run-1", "test", **options)


def repository(name: str, **overrides: Any) -> dict[str, Any]:
    # `pushed_at` is in the real listing and is what a mirror has to keep up
    # with. Leaving it out of the fixture would let a coverage bug pass.
    body = {
        "name": name,
        "full_name": f"{ORG}/{name}",
        "archived": False,
        "visibility": "private",
        "pushed_at": "2026-08-15T05:00:00Z",
    }
    body.update(overrides)
    return body


def commit(sha: str, committed_at: str, **overrides: Any) -> dict[str, Any]:
    body = {
        "sha": sha,
        "sha_short": sha[:7],
        "parents": ["p1"],
        "parent_count": 1,
        "is_merge": False,
        "author_name": "Author One",
        "author_email": "author@example.invalid",
        "authored_at": committed_at,
        "committer_name": "Author One",
        "committer_email": "author@example.invalid",
        "committed_at": committed_at,
        "subject": "change something",
        "body": "change something\n\nwith a longer body\nover two lines",
    }
    body.update(overrides)
    return body


class FakeMirrors:
    def __init__(
        self,
        commits: dict[str, list[dict[str, Any]]],
        *,
        missing: set[str] | None = None,
        fetched_at: dict[str, datetime | None] | None = None,
        newest_at: dict[str, datetime | None] | None = None,
        behind: dict[str, set[str] | None] | None = None,
        mirrored: set[str] | None = None,
    ) -> None:
        self.commits = commits
        self.missing = missing or set()
        self.fetched_at = fetched_at or {}
        # What the refs actually hold, as distinct from when git last touched
        # the directory. The two disagree exactly when a fetch moved
        # FETCH_HEAD without moving a ref.
        self.newest_at = newest_at or {}
        # Remote branches the mirror lacks, as `git ls-remote` would report.
        self.behind = behind or {}
        # Names the directory holds beyond those with commits, so a mirror for
        # a repository the API no longer lists can be represented.
        self.mirrored = mirrored or set()
        self.calls: list[tuple[str, str, str]] = []

    def has_repository(self, repo: str) -> bool:
        return repo not in self.missing and repo in self.repositories()

    def last_fetch_at(self, repo: str) -> datetime | None:
        return self.fetched_at.get(repo)

    def newest_commit_at(self, repo: str) -> datetime | None:
        return self.newest_at.get(repo)

    def branches_behind(self, repo: str) -> set[str] | None:
        return self.behind.get(repo)

    def repositories(self) -> list[str]:
        return sorted(set(self.commits) | set(self.fetched_at) | set(self.newest_at) | self.mirrored)

    def log(self, repo: str, *, since: datetime, until: datetime, include_diffstat: bool) -> list[dict[str, Any]]:
        if repo in self.missing:
            raise FileNotFoundError(repo)
        self.calls.append((repo, since.isoformat(), until.isoformat()))
        return list(self.commits.get(repo, []))


class FakeRest:
    """Returns scripted pages. Each listing may be split across pages."""

    def __init__(
        self,
        *,
        repositories: list[list[dict[str, Any]]] | None = None,
        pull_requests: dict[str, list[list[dict[str, Any]]]] | None = None,
        reviews: dict[tuple[str, int], list[dict[str, Any]]] | None = None,
        review_comments: dict[str, list[dict[str, Any]]] | None = None,
        issue_comments: dict[str, list[dict[str, Any]]] | None = None,
        issues: dict[str, list[dict[str, Any]]] | None = None,
        commits: dict[str, list[dict[str, Any]]] | None = None,
        failing: dict[str, GitHubApiError] | None = None,
    ) -> None:
        self.repositories = repositories if repositories is not None else [[repository("alpha")]]
        self.pull_requests = pull_requests or {}
        self.reviews = reviews or {}
        self.review_comments = review_comments or {}
        self.issue_comments = issue_comments or {}
        self.issues = issues or {}
        self.commits = commits or {}
        self.failing = failing or {}
        self.rate_limit_waits = 0
        self.pull_request_pages_read = 0
        self.commit_calls: list[tuple[str, str, str]] = []
        self.since_arguments: list[str] = []

    def _page(self, pages: list[list[dict[str, Any]]], page: str | None):
        index = int(page or 1) - 1
        if index >= len(pages):
            return [], None
        items = pages[index]
        following = str(index + 2) if index + 1 < len(pages) else None
        return list(items), following

    def list_repositories(self, *, page: str | None = None):
        return self._page(self.repositories, page)

    def list_pull_requests(self, repo: str, *, page: str | None = None):
        if "pull_request" in self.failing:
            raise self.failing["pull_request"]
        self.pull_request_pages_read += 1
        return self._page(self.pull_requests.get(repo, []), page)

    def list_reviews(self, repo: str, number: int, *, page: str | None = None):
        return list(self.reviews.get((repo, number), [])), None

    def list_review_comments(self, repo: str, *, since: str, page: str | None = None):
        self.since_arguments.append(since)
        return list(self.review_comments.get(repo, [])), None

    def list_issue_comments(self, repo: str, *, since: str, page: str | None = None):
        self.since_arguments.append(since)
        return list(self.issue_comments.get(repo, [])), None

    def list_issues(self, repo: str, *, since: str, page: str | None = None):
        if "issue" in self.failing:
            raise self.failing["issue"]
        self.since_arguments.append(since)
        return list(self.issues.get(repo, [])), None

    def list_commits(self, repo: str, *, since: str, until: str, page: str | None = None):
        if "commit" in self.failing:
            raise self.failing["commit"]
        self.commit_calls.append((repo, since, until))
        return list(self.commits.get(repo, [])), None


class MirrorLister:
    """Selects repositories from the mirror directory, like the real one."""

    repository_endpoint = "git-mirror-directory"

    def __init__(self, mirrors: "FakeMirrors", rest: FakeRest) -> None:
        self.mirrors = mirrors
        self.rest = rest

    @property
    def rate_limit_waits(self) -> int:
        return 0

    def list_repositories(self, *, page: str | None = None):
        if page:
            return [], None
        return [{"name": name} for name in self.mirrors.repositories()], None

    def __getattr__(self, name: str) -> Any:
        return getattr(self.rest, name)


class RestWithoutCommits(FakeRest):
    """A client that cannot answer for commits at all."""

    list_commits = None  # type: ignore[assignment]


def rest_commit(sha: str, committed_at: str) -> dict[str, Any]:
    """A commits-API object, in the shape the API actually returns."""
    return {
        "sha": sha,
        "commit": {
            "author": {"name": "Author One", "email": "author@example.invalid", "date": committed_at},
            "committer": {"name": "Author One", "email": "author@example.invalid", "date": committed_at},
            "message": "change something\n\nwith a body",
        },
        "parents": [{"sha": "p1"}],
        "html_url": f"https://github.invalid/{sha}",
    }


def read_archived(archive: RawArchive) -> list[dict[str, Any]]:
    bodies = []
    for entry in archive.files:
        path = archive.root / entry["path"]
        bodies.append(json.loads(gzip.decompress(path.read_bytes()).decode()))
    return bodies


# ------------------------------------------------------------------ window


class TestWindow:
    def test_a_kst_day_is_the_nine_hour_shifted_utc_interval(self) -> None:
        window = Window.parse("2026-08-01")
        assert window.start_at == datetime(2026, 8, 1, tzinfo=KST)
        assert window.start_at.astimezone(timezone.utc) == datetime(2026, 7, 31, 15, tzinfo=timezone.utc)
        assert window.end_at.astimezone(timezone.utc) == datetime(2026, 8, 1, 15, tzinfo=timezone.utc)

    def test_the_window_is_half_open_so_days_never_double_count(self) -> None:
        window = Window.parse("2026-08-01", "2026-08-02")
        assert window.contains(datetime(2026, 8, 1, tzinfo=KST))
        assert window.contains(datetime(2026, 8, 2, 23, 59, 59, tzinfo=KST))
        assert not window.contains(datetime(2026, 8, 3, tzinfo=KST))
        assert not window.contains(datetime(2026, 7, 31, 23, 59, 59, tzinfo=KST))
        assert window.days == ("2026-08-01", "2026-08-02")

    def test_an_end_before_the_start_is_refused(self) -> None:
        with pytest.raises(ValueError):
            Window.parse("2026-08-02", "2026-08-01")

    def test_a_utc_timestamp_lands_on_the_kst_day_a_reader_expects(self) -> None:
        # 2026-08-01T20:00Z is already 2026-08-02 in Seoul.
        assert kst_day(parse_timestamp("2026-08-01T20:00:00Z")) == "2026-08-02"
        assert kst_day(parse_timestamp("2026-08-01T14:00:00Z")) == "2026-08-01"

    def test_an_unparseable_timestamp_is_none_rather_than_a_guess(self) -> None:
        assert parse_timestamp("") is None
        assert parse_timestamp("not a date") is None
        assert parse_timestamp(None) is None
        assert kst_day(None) is None


# ------------------------------------------------------------- collection


class TestCollect:
    def test_a_commit_is_archived_verbatim_and_counted_on_its_kst_day(self, tmp_path: Path) -> None:
        archive = _archive(tmp_path)
        mirrors = FakeMirrors({"alpha": [commit("a" * 40, "2026-08-01T20:00:00Z")]})
        collector = GithubCollector(FakeRest(), mirrors, archive, organization=ORG)

        result = collector.collect(window=Window.parse("2026-08-01", "2026-08-02"), kinds=(), backfill=True)

        assert result.commits == 1
        # 20:00Z on the 1st is the 2nd in Seoul.
        assert result.days["2026-08-02"]["commit"] == 1
        assert "commit" not in result.days["2026-08-01"]
        bodies = read_archived(archive)
        commits_page = next(body for body in bodies if body.get("source") == "bare_mirror")
        assert commits_page["commits"][0]["body"].endswith("over two lines")
        assert commits_page["capture_profile"] == "live-github-commit-from-mirror/v1"

    def test_a_missing_mirror_falls_back_to_rest_and_says_which_source_answered(
        self, tmp_path: Path
    ) -> None:
        # A zero here has to be attributable: "REST says no commits" and "we
        # had no way to look" are different facts, and the manifest must not
        # blur them. The skip path is covered in TestRestCommitFallback.
        archive = _archive(tmp_path)
        mirrors = FakeMirrors({}, missing={"alpha"})
        collector = GithubCollector(FakeRest(), mirrors, archive, organization=ORG)

        result = collector.collect(window=Window.parse("2026-08-01"), kinds=(), backfill=True)

        assert result.repositories_skipped == 0
        assert result.commits == 0
        assert result.counters["per_repository"]["alpha"]["commit_source"] == "rest"
        assert not any(skip["kind"] == "mirror_missing" for skip in archive.skips)

    def test_pull_requests_stop_at_the_window_edge_instead_of_reading_all_history(
        self, tmp_path: Path
    ) -> None:
        archive = _archive(tmp_path)
        rest = FakeRest(
            pull_requests={
                "alpha": [
                    [{"number": 9, "updated_at": "2026-08-01T05:00:00Z", "title": "in window"}],
                    [{"number": 8, "updated_at": "2026-07-01T05:00:00Z", "title": "older"}],
                    [{"number": 7, "updated_at": "2026-06-01T05:00:00Z", "title": "older still"}],
                ]
            }
        )
        collector = GithubCollector(rest, FakeMirrors({}), archive, organization=ORG)

        result = collector.collect(
            window=Window.parse("2026-08-01"),
            kinds=("pull_request",),
            include_commits=False,
            backfill=True,
        )

        assert result.rest_counts["pull_request"] == 1
        # The second page ends the walk; the third is never requested.
        assert rest.pull_request_pages_read == 2

    def test_a_review_is_filed_under_its_own_submitted_day(self, tmp_path: Path) -> None:
        archive = _archive(tmp_path)
        rest = FakeRest(
            pull_requests={
                "alpha": [[{"number": 4, "updated_at": "2026-08-02T01:00:00Z", "title": "pr"}]]
            },
            reviews={
                ("alpha", 4): [
                    {"id": 1, "state": "APPROVED", "submitted_at": "2026-08-01T20:30:00Z"},
                    {"id": 2, "state": "COMMENTED", "submitted_at": "2026-07-20T01:00:00Z"},
                ]
            },
        )
        collector = GithubCollector(rest, FakeMirrors({}), archive, organization=ORG)

        result = collector.collect(
            window=Window.parse("2026-08-01", "2026-08-02"),
            kinds=("pull_request", "review"),
            include_commits=False,
            backfill=True,
        )

        assert result.rest_counts["review"] == 1
        assert result.days["2026-08-02"]["review"] == 1

    def test_asking_for_reviews_without_pull_requests_says_so(self, tmp_path: Path) -> None:
        archive = _archive(tmp_path)
        collector = GithubCollector(FakeRest(), FakeMirrors({}), archive, organization=ORG)

        collector.collect(
            window=Window.parse("2026-08-01"),
            kinds=("review",),
            include_commits=False,
            backfill=True,
        )

        assert any("reviews_require_pull_requests" in note for note in archive.coverage_notes)

    def test_a_pull_request_listed_as_an_issue_is_not_counted_twice(self, tmp_path: Path) -> None:
        archive = _archive(tmp_path)
        rest = FakeRest(
            issues={
                "alpha": [
                    {"number": 3, "updated_at": "2026-08-01T05:00:00Z", "title": "real issue"},
                    {
                        "number": 4,
                        "updated_at": "2026-08-01T06:00:00Z",
                        "title": "actually a pr",
                        "pull_request": {"url": "https://example.invalid/pulls/4"},
                    },
                ]
            }
        )
        collector = GithubCollector(rest, FakeMirrors({}), archive, organization=ORG)

        result = collector.collect(
            window=Window.parse("2026-08-01"),
            kinds=("issue",),
            include_commits=False,
            backfill=True,
        )

        assert result.rest_counts["issue"] == 1
        assert any(skip["kind"] == "issue_listing_pull_requests" for skip in archive.skips)

    def test_since_is_sent_as_utc_not_as_a_kst_literal(self, tmp_path: Path) -> None:
        archive = _archive(tmp_path)
        rest = FakeRest(issue_comments={"alpha": []})
        collector = GithubCollector(rest, FakeMirrors({}), archive, organization=ORG)

        collector.collect(
            window=Window.parse("2026-08-01"),
            kinds=("issue_comment",),
            include_commits=False,
            backfill=True,
        )

        # 2026-08-01 KST begins at 2026-07-31T15:00Z. The legacy collector sent
        # "2026-08-01T00:00:00+09:00" and then compared it as a string.
        assert rest.since_arguments == ["2026-07-31T15:00:00Z"]

    def test_one_failing_endpoint_does_not_lose_the_other_kinds(self, tmp_path: Path) -> None:
        archive = _archive(tmp_path)
        rest = FakeRest(
            issue_comments={"alpha": [{"id": 1, "created_at": "2026-08-01T05:00:00Z"}]},
            failing={"issue": GitHubApiError("forbidden", status=403)},
        )
        collector = GithubCollector(rest, FakeMirrors({}), archive, organization=ORG)

        result = collector.collect(
            window=Window.parse("2026-08-01"),
            kinds=("issue_comment", "issue"),
            include_commits=False,
            backfill=True,
        )

        assert result.rest_counts["issue_comment"] == 1
        assert result.rest_counts["issue"] == 0
        failures = [skip for skip in archive.skips if skip["kind"] == "rest_kind_failed"]
        assert failures and failures[0]["status"] == 403
        assert result.counters["per_repository"]["alpha"]["status"] == "partial"


class TestMirrorCoverage:
    """Only a branch-tip comparison can say a mirror is current."""

    def test_a_quiet_repository_needs_no_comparison(self, tmp_path: Path) -> None:
        # Nobody pushed during or after the window, so nothing in the window
        # can be missing. This is the one verdict reachable for free.
        archive = _archive(tmp_path)
        rest = FakeRest(repositories=[[repository("dormant", pushed_at="2025-11-21T05:00:00Z")]])
        mirrors = FakeMirrors({"dormant": []}, newest_at={"dormant": datetime(2025, 11, 21, tzinfo=timezone.utc)})
        collector = GithubCollector(rest, mirrors, archive, organization=ORG)

        result = collector.collect(window=Window.parse("2026-08-01", "2026-08-31"), kinds=(), backfill=True)

        summary = result.counters["per_repository"]["dormant"]
        assert summary["mirror_covers_window"] is True
        assert summary["coverage_basis"] == "no push in or after the window"

    def test_without_the_comparison_coverage_is_unknown_not_asserted(self, tmp_path: Path) -> None:
        """The push time cannot stand in for it.

        A push is always at least as new as the commits it carries, so
        comparing a push time with a commit date called 107 real mirrors
        behind when a branch comparison found 4 of the first 5 exactly in
        sync. Unknown costs a reader nothing; a wrong "behind" costs them
        their trust in the signal.
        """
        archive = _archive(tmp_path)
        rest = FakeRest(repositories=[[repository("busy", pushed_at="2026-08-31T05:00:00Z")]])
        mirrors = FakeMirrors(
            {"busy": [commit("b" * 40, "2026-08-20T05:00:00Z")]},
            newest_at={"busy": datetime(2026, 8, 20, 5, tzinfo=timezone.utc)},
        )
        collector = GithubCollector(rest, mirrors, archive, organization=ORG)

        result = collector.collect(window=Window.parse("2026-08-01", "2026-08-31"), kinds=(), backfill=True)

        summary = result.counters["per_repository"]["busy"]
        assert summary["mirror_covers_window"] is None
        assert summary["coverage_basis"] == "not verified"
        assert result.counters["mirrors_behind_remote"] == []
        assert result.counters["mirrors_with_unknown_coverage"] == ["busy"]

    def test_matching_branch_tips_prove_the_mirror_is_current(self, tmp_path: Path) -> None:
        archive = _archive(tmp_path)
        rest = FakeRest(repositories=[[repository("busy", pushed_at="2026-08-31T05:00:00Z")]])
        mirrors = FakeMirrors({"busy": []}, behind={"busy": set()})
        collector = GithubCollector(rest, mirrors, archive, organization=ORG)

        result = collector.collect(
            window=Window.parse("2026-08-01", "2026-08-31"), kinds=(), backfill=True,
            verify_mirror_refs=True,
        )

        summary = result.counters["per_repository"]["busy"]
        assert summary["mirror_covers_window"] is True
        assert summary["coverage_basis"] == "every remote branch matches"
        assert summary["mirror_branches_behind"] == 0

    def test_a_branch_the_mirror_lacks_is_a_real_gap(self, tmp_path: Path) -> None:
        # The 576-commit loss, stated as a test.
        archive = _archive(tmp_path)
        rest = FakeRest(repositories=[[repository("busy", pushed_at="2026-08-31T05:00:00Z")]])
        mirrors = FakeMirrors(
            {"busy": []},
            behind={"busy": {"main", "feature/x"}},
            fetched_at={"busy": datetime(2026, 9, 2, 7, 11, tzinfo=timezone.utc)},
        )
        collector = GithubCollector(rest, mirrors, archive, organization=ORG)

        result = collector.collect(
            window=Window.parse("2026-08-01", "2026-08-31"), kinds=(), backfill=True,
            verify_mirror_refs=True,
        )

        summary = result.counters["per_repository"]["busy"]
        assert summary["mirror_covers_window"] is False
        assert summary["mirror_branches_behind"] == 2
        # A fresh mtime must not rescue it: the mtime moves without the refs.
        assert summary["mirror_touched_at"].startswith("2026-09-02")
        assert result.counters["mirrors_behind_remote"] == ["busy"]
        skip = next(entry for entry in archive.skips if entry["kind"] == "mirror_behind_remote")
        assert skip["branches"] == ["feature/x", "main"]

    def test_a_comparison_that_cannot_run_is_unknown(self, tmp_path: Path) -> None:
        archive = _archive(tmp_path)
        rest = FakeRest(repositories=[[repository("busy", pushed_at="2026-08-31T05:00:00Z")]])
        mirrors = FakeMirrors({"busy": []}, behind={"busy": None})
        collector = GithubCollector(rest, mirrors, archive, organization=ORG)

        result = collector.collect(
            window=Window.parse("2026-08-01", "2026-08-31"), kinds=(), backfill=True,
            verify_mirror_refs=True,
        )

        summary = result.counters["per_repository"]["busy"]
        assert summary["mirror_covers_window"] is None
        assert summary["coverage_basis"] == "ref comparison unavailable"
        assert any(entry["kind"] == "mirror_coverage_unknown" for entry in archive.skips)


# ---------------------------------------------------------------- backfill


class TestRenamedRepositoryCounting:
    """One repository mirrored under two names is one repository's commits."""

    def test_a_commit_reached_through_two_names_is_counted_once(self, tmp_path: Path) -> None:
        # GitHub redirects an old repository name, so a renamed repository is
        # mirrored twice and every one of its commits arrives twice. On the
        # real August window that was 332 commits, 147 from a single pair.
        # The rows all belong in the archive; the count does not.
        shared = commit("d" * 40, "2026-08-05T05:00:00Z")
        archive = _archive(tmp_path)
        rest = FakeRest(repositories=[[repository("new-name"), repository("old-name")]])
        mirrors = FakeMirrors({"new-name": [shared], "old-name": [shared]})
        collector = GithubCollector(rest, mirrors, archive, organization=ORG)

        result = collector.collect(window=Window.parse("2026-08-01", "2026-08-31"), kinds=(), backfill=True)

        assert result.commits == 1
        assert result.commit_rows == 2
        assert result.counters["commits_seen_under_two_names"] == ["new-name", "old-name"]
        assert result.days["2026-08-05"]["commit"] == 1
        assert result.days["2026-08-05"]["commit_row"] == 2
        manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
        assert manifest["commits"] == 1
        assert manifest["commit_rows_archived"] == 2
        # Both observations reached the archive: nothing was dropped to make
        # the counts agree.
        pages = [b for b in read_archived(archive) if b.get("source") == "bare_mirror"]
        assert sorted(page["repository"] for page in pages) == ["new-name", "old-name"]

    def test_distinct_repositories_are_not_collapsed(self, tmp_path: Path) -> None:
        archive = _archive(tmp_path)
        rest = FakeRest(repositories=[[repository("alpha"), repository("beta")]])
        mirrors = FakeMirrors(
            {
                "alpha": [commit("a" * 40, "2026-08-05T05:00:00Z")],
                "beta": [commit("b" * 40, "2026-08-05T05:00:00Z")],
            }
        )
        collector = GithubCollector(rest, mirrors, archive, organization=ORG)

        result = collector.collect(window=Window.parse("2026-08-01", "2026-08-31"), kinds=(), backfill=True)

        assert result.commits == result.commit_rows == 2
        assert result.counters["commits_seen_under_two_names"] == []


class TestRepositoryDivergence:
    def test_both_directions_are_reported(self, tmp_path: Path) -> None:
        # A mirror the API no longer lists is either a deleted repository, and
        # therefore the only surviving evidence, or a renamed one. A
        # repository with no mirror loses commits unless they are read over
        # REST. Neither is visible from a per-repository fetch time.
        archive = _archive(tmp_path)
        rest = FakeRest(repositories=[[repository("shared"), repository("api-only")]])
        mirrors = FakeMirrors({"shared": []}, mirrored={"shared", "mirror-only"})
        collector = GithubCollector(rest, mirrors, archive, organization=ORG)

        result = collector.collect(window=Window.parse("2026-08-01"), kinds=(), backfill=True)

        assert result.counters["divergence_computed"] is True
        assert result.counters["repositories_mirror_only"] == ["mirror-only"]
        assert result.counters["repositories_api_only"] == ["api-only"]
        manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
        assert manifest["repositories_mirror_only"] == ["mirror-only"]
        assert manifest["repositories_api_only"] == ["api-only"]
        assert any("mirror_is_sole_evidence" in note for note in archive.coverage_notes)
        assert any(entry["kind"] == "repository_absent_from_api" for entry in archive.skips)
        assert any(entry["kind"] == "repository_without_mirror" for entry in archive.skips)

    def test_an_aligned_pair_reports_empty_sets_not_unknown(self, tmp_path: Path) -> None:
        archive = _archive(tmp_path)
        rest = FakeRest(repositories=[[repository("shared")]])
        mirrors = FakeMirrors({"shared": []}, mirrored={"shared"})
        collector = GithubCollector(rest, mirrors, archive, organization=ORG)

        result = collector.collect(window=Window.parse("2026-08-01"), kinds=(), backfill=True)

        assert result.counters["divergence_computed"] is True
        assert result.counters["repositories_mirror_only"] == []
        assert result.counters["repositories_api_only"] == []

    def test_one_missing_listing_is_unknown_not_empty(self, tmp_path: Path) -> None:
        # An empty divergence with only one listing available would read as
        # "nothing diverges", which is the quiet-loss shape this feature
        # exists to remove.
        class MirrorsWithoutListing(FakeMirrors):
            repositories = None  # type: ignore[assignment]

            def has_repository(self, repo: str) -> bool:
                return repo not in self.missing

        archive = _archive(tmp_path)
        collector = GithubCollector(
            FakeRest(), MirrorsWithoutListing({"alpha": []}), archive, organization=ORG
        )

        result = collector.collect(window=Window.parse("2026-08-01"), kinds=(), backfill=True)

        assert result.counters["divergence_computed"] is False
        assert result.counters["repositories_mirror_only"] is None
        assert any("divergence_not_computed" in note for note in archive.coverage_notes)


class TestRestCommitFallback:
    def test_a_repository_without_a_mirror_reads_commits_over_rest(self, tmp_path: Path) -> None:
        # groot17 and rrc.rlwrld.co lost 14 August commits to this gap.
        archive = _archive(tmp_path)
        rest = FakeRest(
            repositories=[[repository("unmirrored")]],
            commits={"unmirrored": [rest_commit("f" * 40, "2026-08-01T20:00:00Z")]},
        )
        mirrors = FakeMirrors({}, mirrored=set())
        collector = GithubCollector(rest, mirrors, archive, organization=ORG)

        result = collector.collect(
            window=Window.parse("2026-08-01", "2026-08-02"), kinds=(), backfill=True
        )

        assert result.commits == 1
        assert result.repositories_skipped == 0
        assert result.counters["per_repository"]["unmirrored"]["commit_source"] == "rest"
        # 20:00Z is the 2nd in Seoul: the day key matches the mirror path's.
        assert result.days["2026-08-02"]["commit"] == 1
        page = next(body for body in read_archived(archive) if body.get("source") == "rest")
        assert page["commits"][0]["commit"]["message"].endswith("with a body")
        assert any("commits_read_over_rest" in note for note in archive.coverage_notes)

    def test_a_rest_commit_outside_the_window_is_filtered(self, tmp_path: Path) -> None:
        archive = _archive(tmp_path)
        rest = FakeRest(
            repositories=[[repository("unmirrored")]],
            commits={
                "unmirrored": [
                    rest_commit("a" * 40, "2026-08-01T05:00:00Z"),
                    rest_commit("b" * 40, "2026-07-01T05:00:00Z"),
                ]
            },
        )
        collector = GithubCollector(rest, FakeMirrors({}), archive, organization=ORG)

        result = collector.collect(window=Window.parse("2026-08-01"), kinds=(), backfill=True)

        assert result.commits == 1

    def test_a_failing_commits_endpoint_becomes_a_skip_not_a_zero(self, tmp_path: Path) -> None:
        archive = _archive(tmp_path)
        rest = FakeRest(
            repositories=[[repository("unmirrored")]],
            failing={"commit": GitHubApiError("not found", status=404)},
        )
        collector = GithubCollector(rest, FakeMirrors({}), archive, organization=ORG)

        result = collector.collect(window=Window.parse("2026-08-01"), kinds=(), backfill=True)

        assert result.commits == 0
        assert result.repositories_skipped == 1
        assert any(entry["kind"] == "rest_commits_failed" for entry in archive.skips)
        assert any(entry["kind"] == "mirror_missing" for entry in archive.skips)

    def test_a_client_without_a_commits_endpoint_still_reports_the_gap(self, tmp_path: Path) -> None:
        archive = _archive(tmp_path)
        rest = RestWithoutCommits(repositories=[[repository("unmirrored")]])
        collector = GithubCollector(rest, FakeMirrors({}), archive, organization=ORG)

        result = collector.collect(window=Window.parse("2026-08-01"), kinds=(), backfill=True)

        assert result.repositories_skipped == 1
        assert result.counters["per_repository"]["unmirrored"]["status"] == "mirror_missing"


class TestCheckpoint:
    def test_a_backfill_neither_reads_nor_moves_the_watermark(self, tmp_path: Path) -> None:
        archive = _archive(tmp_path)
        archive.write_checkpoint(
            {"schema_version": 1, "source": GITHUB_SOURCE, "run_id": "earlier", "collected_through": "2026-08-20"}
        )
        later = _archive(tmp_path)
        collector = GithubCollector(FakeRest(), FakeMirrors({}), later, organization=ORG)

        result = collector.collect(window=Window.parse("2026-08-01"), kinds=(), backfill=True)

        assert result.checkpoint_advanced is False
        stored = json.loads(later.checkpoint_path.read_text(encoding="utf-8"))
        assert stored["collected_through"] == "2026-08-20"
        manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
        assert manifest["mode"] == "backfill"
        assert manifest["checkpoint_in"]["ignored_for_backfill"] is True

    def test_an_incremental_run_advances_the_watermark(self, tmp_path: Path) -> None:
        archive = _archive(tmp_path)
        collector = GithubCollector(FakeRest(), FakeMirrors({}), archive, organization=ORG)

        result = collector.collect(window=Window.parse("2026-08-01"), kinds=())

        assert result.checkpoint_advanced is True
        stored = json.loads(archive.checkpoint_path.read_text(encoding="utf-8"))
        assert stored["collected_through"] == "2026-08-01"

    def test_a_truncated_run_holds_the_watermark_back(self, tmp_path: Path) -> None:
        archive = _archive(tmp_path)
        rest = FakeRest(repositories=[[repository("alpha"), repository("beta")]])
        collector = GithubCollector(rest, FakeMirrors({}), archive, organization=ORG)

        result = collector.collect(window=Window.parse("2026-08-01"), kinds=(), max_repositories=1)

        assert result.checkpoint_advanced is False
        assert archive.truncated is True
        assert any("checkpoint_held_back_on_truncation" in note for note in archive.coverage_notes)

    def test_a_dry_run_never_advances_the_watermark(self, tmp_path: Path) -> None:
        archive = _archive(tmp_path, dry_run=True, capture_density="smoke")
        collector = GithubCollector(FakeRest(), FakeMirrors({}), archive, organization=ORG)

        result = collector.collect(window=Window.parse("2026-08-01"), kinds=())

        assert result.checkpoint_advanced is False


# ------------------------------------------------- legacy regression tests


class TestLegacyLossesAreNotPorted:
    """Each of these fails against the legacy collector's behaviour."""

    def test_the_window_edge_is_not_off_by_the_utc_offset(self, tmp_path: Path) -> None:
        # The legacy filter was `updated_at < "2026-08-01T00:00:00+09:00"` as a
        # string. '+' < 'Z', so a UTC timestamp inside the KST day compared as
        # if it were older and the walk stopped early.
        legacy_literal = "2026-08-01T00:00:00+09:00"
        inside_the_kst_day = "2026-08-01T02:00:00Z"
        assert inside_the_kst_day > legacy_literal  # what the legacy code saw
        window = Window.parse("2026-08-01")
        assert window.contains(parse_timestamp(inside_the_kst_day))

        archive = _archive(tmp_path)
        rest = FakeRest(
            pull_requests={
                "alpha": [[{"number": 1, "updated_at": inside_the_kst_day, "title": "edge"}]]
            }
        )
        collector = GithubCollector(rest, FakeMirrors({}), archive, organization=ORG)
        result = collector.collect(
            window=window, kinds=("pull_request",), include_commits=False, backfill=True
        )
        assert result.rest_counts["pull_request"] == 1

    def test_every_repository_is_listed_rather_than_capped_at_a_limit(self, tmp_path: Path) -> None:
        # `gh repo list --limit 300` truncated silently while 366 mirrors
        # existed on disk.
        pages = [[repository(f"repo-{index:03d}") for index in range(100)] for _ in range(4)]
        archive = _archive(tmp_path)
        rest = FakeRest(repositories=pages)
        collector = GithubCollector(rest, FakeMirrors({}), archive, organization=ORG)

        result = collector.collect(window=Window.parse("2026-08-01"), kinds=(), backfill=True)

        assert result.repositories_listed == 400
        manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
        assert manifest["counters"]["repositories_listed"] == 400
        assert manifest["truncated"] is False

    def test_an_archived_repository_still_contributes_its_activity(self, tmp_path: Path) -> None:
        # `--no-archived` dropped these entirely.
        archive = _archive(tmp_path)
        rest = FakeRest(repositories=[[repository("frozen", archived=True)]])
        mirrors = FakeMirrors({"frozen": [commit("b" * 40, "2026-08-01T05:00:00Z")]})
        collector = GithubCollector(rest, mirrors, archive, organization=ORG)

        result = collector.collect(window=Window.parse("2026-08-01"), kinds=(), backfill=True)

        assert result.commits == 1
        assert result.counters["per_repository"]["frozen"]["archived"] is True

    def test_a_merge_commit_is_kept(self, tmp_path: Path) -> None:
        # `--no-merges` dropped the commit a merged pull request lands as.
        archive = _archive(tmp_path)
        merge = commit(
            "c" * 40,
            "2026-08-01T05:00:00Z",
            parents=["p1", "p2"],
            parent_count=2,
            is_merge=True,
            subject="Merge pull request #12",
        )
        mirrors = FakeMirrors({"alpha": [merge]})
        collector = GithubCollector(FakeRest(), mirrors, archive, organization=ORG)

        result = collector.collect(window=Window.parse("2026-08-01"), kinds=(), backfill=True)

        assert result.commits == 1
        bodies = read_archived(archive)
        page = next(body for body in bodies if body.get("source") == "bare_mirror")
        assert page["commits"][0]["is_merge"] is True
        assert page["commits"][0]["parent_count"] == 2
