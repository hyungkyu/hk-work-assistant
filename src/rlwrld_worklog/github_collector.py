"""GitHub capture: commits from local bare mirrors, everything else over REST.

This is a port of the legacy `github_daily_collect.py`, and the port exists to
drop four ways that collector lost data without saying so:

  1. **Window filters compared timestamp strings across offsets.** The legacy
     code built `since` as a KST string (`...T00:00:00+09:00`) and compared it
     lexicographically against GitHub's UTC `...Z` values. `'+'` sorts before
     `'Z'`, so the comparison was not a time comparison at all, and the first
     and last day of any window were wrong by up to nine hours. Every window
     decision here is made on timezone-aware datetimes.
  2. **`gh repo list --limit 300` truncated silently** while the mirror
     directory already held 366 repositories. Repository listing here is fully
     paginated and the count is recorded in the manifest, so a truncation is
     visible instead of implied.
  3. **`--no-archived` dropped archived repositories** together with the
     activity they held before they were archived. Archived state is captured
     as a field, never as a filter.
  4. **`--no-merges` dropped merge commits**, which is where a merged pull
     request lands. Merge commits are captured with `parent_count` so a
     consumer can decide; the collector does not decide for it.

A fifth difference is structural rather than a bug: the legacy collector
projected each API object onto a hand-written dict of a dozen fields, so a
commit kept its subject but lost its body, and author and committer collapsed
into one identity. Here the API response is archived verbatim and projection is
left to the ledger, which is what the immutable raw archive is for.

Commits come from the bare mirrors under `<mirror_root>/<repo>.git`, read with
`git log`, so a month-long backfill needs no API budget and has no window
limit. Pull requests, reviews, review comments, issue comments and issues come
from REST, which is why the run reports its rate-limit waits.

The credential is read from `<config_root>/credentials/github-token` and the
file takes precedence over `GITHUB_TOKEN`, matching the other collectors. The
legacy collector called `/usr/bin/security` (the macOS keychain) directly and
`_backfill_range` unset `GH_TOKEN` to force that path, which cannot work on
Linux; nothing here shells out to a keychain, so that defect is not ported and
the legacy tree needs no patch.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence

from .archive import RawArchive

GITHUB_SOURCE = "github"
GITHUB_CAPTURE_PROFILE = "live-github-api/v1"
# Per-record profiles. A commit read from a mirror is still an official-API
# observation -- the mirror was cloned from GitHub and a commit object is
# immutable -- so it loads at live priority and a legacy re-run cannot demote
# it.
COMMIT_CAPTURE_PROFILE = "live-github-commit-from-mirror/v1"
REST_CAPTURE_PROFILE = "live-github-rest/v1"
CHECKPOINT_SCHEMA_VERSION = 1

KST = timezone(timedelta(hours=9))

# The six object kinds the legacy collector captured, in the order they are
# collected. Releases, deployments, Actions runs and permission changes are
# deliberately out of scope for this port and are named in `remaining` on the
# work item rather than half-captured here.
REST_KINDS = ("pull_request", "review", "review_comment", "issue_comment", "issue")

COVERAGE_NOTES = (
    "github.commits_come_from_local_mirrors: commits are read with `git log --all` from bare "
    "mirrors rather than from the commits API, so the window is not bounded by API retention "
    "and no request budget is spent. A repository with no mirror is reported as a skip, never "
    "as a day with no commits.",
    "github.merge_commits_are_kept: merge commits are captured with parent_count, unlike the "
    "legacy collector's --no-merges. A merged pull request is visible on the commit side.",
    "github.archived_repositories_are_captured: archived state is a field, not a filter. A "
    "repository archived after the window still contributes the activity it held during it.",
    "github.pull_requests_have_no_since_parameter: the pulls endpoint cannot be filtered by "
    "time, so it is paginated newest-updated-first and stopped at the window edge. A pull "
    "request whose last update predates the window is not re-observed even if it was open.",
    "github.review_bodies_follow_their_pull_request: reviews are fetched per pull request "
    "found in the window. A review on a pull request untouched during the window cannot "
    "occur, because submitting a review updates the pull request.",
    "github.private_repository_content_is_metadata_only: no blob, no file body and no source "
    "tree is fetched. Commit file statistics come from the local mirror's diff, not from the "
    "API.",
    "github.repos_absent_from_api_mirror_is_sole_evidence: a repository with a mirror the API "
    "no longer lists was either deleted or renamed, and only the first makes its mirror the "
    "sole evidence. A renamed repository still answers under its old name because GitHub "
    "redirects, so its commits arrive twice -- once under each mirrored name -- and are "
    "counted once. The names are in counters.repositories_mirror_only, and the repositories "
    "whose commits were seen under two names in counters.commits_seen_under_two_names.",
    "github.repos_absent_from_mirror_lose_commits: a repository the API lists with no mirror "
    "has no local commit history, so its commits are read over REST instead. If neither is "
    "available the run reports it rather than showing a repository with no commits. The names "
    "are listed in counters.repositories_api_only.",
    "github.commit_coverage_is_bounded_by_mirror_freshness: whether a mirror holds everything "
    "the remote has is answered by comparing their branch tips, which costs a network round "
    "trip per repository and is therefore opt-in. Neither the filesystem's fetch time nor the "
    "remote's push time can answer it: these clones carry no fetch refspec so a fetch moves "
    "the mtime without moving a ref, and a push is always at least as new as the commits it "
    "carries. Without the comparison a repository not pushed during the window is covered and "
    "every other mirror's coverage is reported as unknown rather than guessed.",
)


# --------------------------------------------------------------- protocols


class GitHubRestClient(Protocol):
    """One page per call, so a window scan can stop instead of pre-fetching.

    Each method returns `(items, next_page_token)`; `None` for the token means
    the listing is exhausted. `gh api --paginate` cannot express that, which is
    why the legacy collector always walked a repository's entire history.
    """

    def list_repositories(self, *, page: str | None = None) -> tuple[list[dict[str, Any]], str | None]: ...

    def list_pull_requests(
        self, repo: str, *, page: str | None = None
    ) -> tuple[list[dict[str, Any]], str | None]: ...

    def list_reviews(
        self, repo: str, number: int, *, page: str | None = None
    ) -> tuple[list[dict[str, Any]], str | None]: ...

    def list_review_comments(
        self, repo: str, *, since: str, page: str | None = None
    ) -> tuple[list[dict[str, Any]], str | None]: ...

    def list_issue_comments(
        self, repo: str, *, since: str, page: str | None = None
    ) -> tuple[list[dict[str, Any]], str | None]: ...

    def list_issues(
        self, repo: str, *, since: str, page: str | None = None
    ) -> tuple[list[dict[str, Any]], str | None]: ...

    def list_commits(
        self, repo: str, *, since: str, until: str, page: str | None = None
    ) -> tuple[list[dict[str, Any]], str | None]: ...

    @property
    def rate_limit_waits(self) -> int: ...


class MirrorReader(Protocol):
    """Reads commits out of the bare mirrors. No network, no API budget."""

    def has_repository(self, repo: str) -> bool: ...

    def last_fetch_at(self, repo: str) -> datetime | None: ...

    def repositories(self) -> list[str]: ...

    def log(
        self, repo: str, *, since: datetime, until: datetime, include_diffstat: bool
    ) -> list[dict[str, Any]]: ...


class GitHubApiError(RuntimeError):
    """A REST call failed for a reason that is not a rate limit."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


# ------------------------------------------------------------------ window


@dataclass(frozen=True)
class Window:
    """A closed interval of KST calendar days, held as UTC instants.

    KST has no daylight saving, so a KST day is always the same nine-hour
    shift. The dates are kept alongside the instants because the day key a
    record is filed under is a KST date, while every comparison is done on the
    instants.
    """

    start_date: date
    end_date: date

    @classmethod
    def parse(cls, since: str, until: str | None = None) -> "Window":
        start = date.fromisoformat(since)
        end = date.fromisoformat(until) if until else start
        if end < start:
            raise ValueError(f"window end {end} precedes start {start}")
        return cls(start, end)

    @property
    def start_at(self) -> datetime:
        return datetime.combine(self.start_date, datetime.min.time(), tzinfo=KST)

    @property
    def end_at(self) -> datetime:
        return datetime.combine(self.end_date + timedelta(days=1), datetime.min.time(), tzinfo=KST)

    @property
    def days(self) -> tuple[str, ...]:
        span = (self.end_date - self.start_date).days + 1
        return tuple((self.start_date + timedelta(days=offset)).isoformat() for offset in range(span))

    def contains(self, moment: datetime | None) -> bool:
        if moment is None:
            return False
        return self.start_at <= moment < self.end_at

    def as_dict(self) -> dict[str, Any]:
        return {
            "since": self.start_date.isoformat(),
            "until": self.end_date.isoformat(),
            "timezone": "Asia/Seoul",
            "start_at": self.start_at.isoformat(),
            "end_at": self.end_at.isoformat(),
            "days": len(self.days),
        }


def parse_timestamp(value: Any) -> datetime | None:
    """Parse a GitHub timestamp into an aware datetime, or None.

    GitHub returns `2026-08-01T04:05:06Z`. `date.fromisoformat` in older
    Pythons rejects the `Z`, and every legacy comparison here was done on the
    string, so this is the one place the conversion happens.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("z", "Z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def kst_day(moment: datetime | None) -> str | None:
    """The KST calendar day a moment belongs to."""
    if moment is None:
        return None
    return moment.astimezone(KST).date().isoformat()


# ------------------------------------------------------------------ result


@dataclass(frozen=True)
class GithubCollectionResult:
    run_id: str
    window: Window
    repositories_listed: int
    repositories_collected: int
    repositories_skipped: int
    commits: int
    commit_rows: int
    rest_counts: dict[str, int]
    manifest_path: Path
    checkpoint_advanced: bool = False
    days: dict[str, dict[str, int]] = field(default_factory=dict)
    counters: dict[str, Any] = field(default_factory=dict)


def _committed_at(record: dict[str, Any]) -> Any:
    """The commit date, from either shape.

    A mirror record carries `committed_at` at the top level; a REST commit
    nests it under `commit.committer.date`. One accessor keeps the day-keying
    identical for both, so a repository read over REST is not filed on a
    different day than the same repository read from a mirror.
    """
    if record.get("committed_at"):
        return record["committed_at"]
    commit = record.get("commit")
    if isinstance(commit, dict):
        for role in ("committer", "author"):
            person = commit.get(role)
            if isinstance(person, dict) and person.get("date"):
                return person["date"]
    return None


def _repo_name(entry: Any) -> str | None:
    if isinstance(entry, str):
        return entry or None
    if isinstance(entry, dict):
        for key in ("name", "full_name"):
            value = entry.get(key)
            if isinstance(value, str) and value:
                return value.split("/")[-1]
    return None


class GithubCollector:
    def __init__(
        self,
        client: GitHubRestClient,
        mirrors: MirrorReader,
        archive: RawArchive,
        *,
        organization: str,
    ) -> None:
        self.client = client
        self.mirrors = mirrors
        self.archive = archive
        self.organization = organization

    # ------------------------------------------------------------- driver

    def collect(
        self,
        *,
        window: Window,
        repositories: Sequence[str] | None = None,
        kinds: Sequence[str] = REST_KINDS,
        include_commits: bool = True,
        include_diffstat: bool = True,
        max_repositories: int | None = None,
        advance_checkpoint: bool = True,
        backfill: bool = False,
        verify_mirror_refs: bool = False,
    ) -> GithubCollectionResult:
        """Capture one window.

        `backfill=True` makes the run independent of the checkpoint in both
        directions: it neither starts from the stored watermark nor moves it.
        Slack taught this the hard way -- a per-channel high watermark silently
        overrode `--since` and made history unreachable -- so the backfill path
        exists from the first version rather than being retrofitted.
        """
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
                "organization": self.organization,
                "kinds": list(kinds),
                "include_commits": include_commits,
                "include_diffstat": include_diffstat,
                "verify_mirror_refs": verify_mirror_refs,
                "requested_repositories": sorted(repositories or ()),
            }
        )
        for note in COVERAGE_NOTES:
            archive.note_coverage(note)

        listed = self._list_repositories()
        selected = self._select(listed, requested=repositories, limit=max_repositories)
        # The remote's last push time, which is what a mirror has to keep up
        # with. A mirror-backed listing does not carry it, so the API listing
        # is consulted when one is available.
        pushed_at_by_repo = self._pushed_at_by_repository(listed)

        days: dict[str, dict[str, int]] = {day: {} for day in window.days}
        rest_counts: dict[str, int] = {kind: 0 for kind in kinds}
        per_repository: dict[str, dict[str, Any]] = {}
        commit_total = 0
        # A commit sha reached through two repository names is one commit. A
        # renamed repository stays reachable under its old name because GitHub
        # redirects, so it is mirrored twice and 332 August commits arrived
        # twice with it.
        commit_shas: set[str] = set()
        sha_repositories: dict[str, set[str]] = {}
        skipped: list[str] = []

        for entry in selected:
            name = _repo_name(entry)
            if not name:
                archive.note_error("repository_without_name", entry=str(entry)[:200])
                continue
            summary: dict[str, Any] = {
                "archived": bool(entry.get("archived")) if isinstance(entry, dict) else None,
                "visibility": entry.get("visibility") if isinstance(entry, dict) else None,
            }
            if include_commits:
                freshness = self._mirror_freshness(
                    name,
                    window=window,
                    pushed_at=parse_timestamp(pushed_at_by_repo.get(name)),
                    verify_refs=verify_mirror_refs,
                )
                try:
                    commits = self._collect_commits(
                        name, window=window, include_diffstat=include_diffstat
                    )
                    summary["commit_source"] = "mirror"
                    summary.update(freshness)
                except FileNotFoundError:
                    # No mirror. The commits still exist upstream, so they are
                    # read over REST rather than reported as a quiet zero --
                    # groot17 and rrc.rlwrld.co lost 14 August commits to
                    # exactly this gap.
                    commits = self._collect_commits_over_rest(name, window=window)
                    if commits is None:
                        skipped.append(name)
                        archive.note_skip("mirror_missing", repository=name)
                        summary["commits"] = None
                        summary["commit_source"] = None
                        summary["status"] = "mirror_missing"
                        per_repository[name] = summary
                        continue
                    summary["commit_source"] = "rest"
                commit_total += len(commits)
                summary["commits"] = len(commits)
                for record in commits:
                    sha = str(record.get("sha") or "")
                    first_sighting = bool(sha) and sha not in commit_shas
                    if sha:
                        commit_shas.add(sha)
                        sha_repositories.setdefault(sha, set()).add(name)
                    day = kst_day(parse_timestamp(_committed_at(record)))
                    if day in days:
                        days[day]["commit_row"] = days[day].get("commit_row", 0) + 1
                        # The per-day figure a reader takes for "commits"
                        # counts each commit once, on the day it was made.
                        if first_sighting:
                            days[day]["commit"] = days[day].get("commit", 0) + 1

            failures = 0
            # Left empty on purpose: the pull-request pass creates the key, so
            # asking for reviews without pull requests is detectable.
            state: dict[str, Any] = {}
            for kind in kinds:
                try:
                    counted = self._collect_rest_kind(kind, name, window=window, state=state)
                except GitHubApiError as error:
                    failures += 1
                    archive.note_skip(
                        "rest_kind_failed",
                        repository=name,
                        # `kind` is the skip's own kind in the manifest, so the
                        # object kind travels under its own name.
                        object_kind=kind,
                        status=error.status,
                        error=str(error)[:200],
                    )
                    summary[kind] = None
                    continue
                rest_counts[kind] = rest_counts.get(kind, 0) + sum(counted.values())
                summary[kind] = sum(counted.values())
                for day, count in counted.items():
                    if day in days:
                        days[day][kind] = days[day].get(kind, 0) + count

            summary["status"] = "ok" if not failures else "partial"
            per_repository[name] = summary

        archive.note_rate_limit(int(self.client.rate_limit_waits or 0))

        behind = sorted(
            name
            for name, summary in per_repository.items()
            if summary.get("mirror_covers_window") is False
        )
        unknown_coverage = sorted(
            name
            for name, summary in per_repository.items()
            if summary.get("mirror_covers_window") is None
            and summary.get("commit_source") == "mirror"
        )
        divergence = self._repository_divergence(listed)
        counters = {
            "repositories_listed": len(listed),
            "repositories_attempted": len(selected),
            "repositories_skipped": len(skipped),
            "repositories_with_mirror_behind_remote": len(behind),
            "mirrors_behind_remote": behind,
            "mirrors_with_unknown_coverage": unknown_coverage,
            **divergence,
            # Distinct commits. `commit_rows_archived` counts every archived
            # row, which is larger whenever one repository is mirrored under
            # two names.
            "commits": len(commit_shas),
            "commit_rows_archived": commit_total,
            "commits_seen_under_two_names": sorted(
                {
                    name
                    for repositories in sha_repositories.values()
                    if len(repositories) > 1
                    for name in repositories
                }
            ),
            "rest": dict(rest_counts),
            "days": {day: dict(counts) for day, counts in days.items()},
            "per_repository": per_repository,
            "capture_profiles_by_kind": {
                "commit": COMMIT_CAPTURE_PROFILE,
                **{kind: REST_CAPTURE_PROFILE for kind in kinds},
            },
        }
        status = "success_with_skips" if archive.skips or archive.errors else "success"

        checkpoint_advanced = False
        if advance_checkpoint and not backfill and not archive.dry_run and not archive.truncated:
            archive.write_checkpoint(
                {
                    "schema_version": CHECKPOINT_SCHEMA_VERSION,
                    "source": GITHUB_SOURCE,
                    "run_id": archive.run_id,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    "collected_through": window.end_date.isoformat(),
                    "organization": self.organization,
                    "repositories": len(selected),
                }
            )
            checkpoint_advanced = True
        elif advance_checkpoint and archive.truncated:
            archive.note_coverage(
                "github.checkpoint_held_back_on_truncation: the run was truncated, so the "
                "watermark stays where it was and the next run repeats this window."
            )

        manifest_path = archive.finish(
            {
                "status": status,
                "organization": self.organization,
                "mode": "backfill" if backfill else "incremental",
                "repositories_listed": len(listed),
                "repositories_collected": len(selected) - len(skipped),
                "skipped_repositories": sorted(skipped),
                "mirrors_behind_remote": behind,
                "mirrors_with_unknown_coverage": unknown_coverage,
                "repositories_mirror_only": divergence["repositories_mirror_only"],
                "repositories_api_only": divergence["repositories_api_only"],
                "commits": len(commit_shas),
                "commit_rows_archived": commit_total,
                "rest_counts": dict(rest_counts),
                "counters": counters,
            }
        )
        return GithubCollectionResult(
            run_id=archive.run_id,
            window=window,
            repositories_listed=len(listed),
            repositories_collected=len(selected) - len(skipped),
            repositories_skipped=len(skipped),
            commits=len(commit_shas),
            commit_rows=commit_total,
            rest_counts=dict(rest_counts),
            manifest_path=manifest_path,
            checkpoint_advanced=checkpoint_advanced,
            days={day: dict(counts) for day, counts in days.items()},
            counters=counters,
        )

    # ------------------------------------------------------- repositories

    def _repository_divergence(self, listed: list[dict[str, Any]]) -> dict[str, Any]:
        """Compare the mirror directory with the API listing, both directions.

        Three answers matter, and a per-repository last-fetch time gives only
        the third:

          mirror_only   a mirror whose repository the API no longer lists. Its
                        history exists nowhere else, so it must not be tidied
                        away.
          api_only      a repository with no mirror. Commits have to come over
                        REST or they are missed entirely.
          stale_mirror  present both sides, mirror older than the window.

        Either side can be unavailable -- a mirror-listed run may hold no REST
        client -- and that is reported as unavailable rather than as an empty
        set, which would read as "nothing diverges".
        """
        mirror_names = self._mirror_repository_names()
        api_names = self._api_repository_names(listed)
        if mirror_names is None or api_names is None:
            self.archive.note_coverage(
                "github.mirror_api_divergence_not_computed: this run saw only one of the two "
                "repository listings, so mirror-only and api-only repositories could not be "
                "identified. An empty divergence here means unknown, not none."
            )
            return {
                "divergence_computed": False,
                "repositories_mirror_only": None,
                "repositories_api_only": None,
            }
        mirror_only = sorted(mirror_names - api_names)
        api_only = sorted(api_names - mirror_names)
        if api_only:
            self.archive.note_skip("repository_without_mirror", repositories=api_only)
        if mirror_only:
            self.archive.note_skip("repository_absent_from_api", repositories=mirror_only)
        return {
            "divergence_computed": True,
            "repositories_mirror_only": mirror_only,
            "repositories_api_only": api_only,
        }

    def _mirror_repository_names(self) -> set[str] | None:
        reader = getattr(self.mirrors, "repositories", None)
        if not callable(reader):
            return None
        try:
            return {str(name) for name in reader()}
        except OSError:
            return None

    def _api_repository_names(self, listed: list[dict[str, Any]]) -> set[str] | None:
        """The API's own listing, however this run happened to be wired.

        A run that selected repositories from the API already has it. A run
        that selected them from the mirror directory has to ask, which is why
        the mirror-backed lister keeps its REST client.
        """
        if not getattr(self.client, "repository_endpoint", None):
            return {name for entry in listed if (name := _repo_name(entry))}
        rest = getattr(self.client, "rest", None)
        if rest is None:
            return None
        names: set[str] = set()
        page: str | None = None
        try:
            while True:
                items, page = rest.list_repositories(page=page)
                names.update(name for item in items if (name := _repo_name(item)))
                if not page:
                    break
        except Exception as error:
            self.archive.note_skip("api_listing_failed", error=f"{type(error).__name__}: {error}"[:200])
            return None
        return names

    def _collect_commits_over_rest(
        self, repo: str, *, window: Window
    ) -> list[dict[str, Any]] | None:
        """Commits for a repository with no mirror. None when unavailable."""
        lister = getattr(self.client, "list_commits", None)
        if not callable(lister):
            return None
        since = window.start_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        until = window.end_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        collected: list[dict[str, Any]] = []
        page: str | None = None
        try:
            while True:
                items, page = lister(repo, since=since, until=until, page=page)
                keep = [
                    item
                    for item in items
                    if window.contains(parse_timestamp(_committed_at(item)))
                ]
                if keep:
                    self.archive.write_page(
                        f"commits-rest-{repo}",
                        {
                            "repository": repo,
                            "organization": self.organization,
                            "source": "rest",
                            "capture_profile": REST_CAPTURE_PROFILE,
                            "window": window.as_dict(),
                            "commits": keep,
                        },
                        endpoint=f"/repos/{self.organization}/{repo}/commits",
                        request={"since": since, "until": until},
                        item_count=len(keep),
                    )
                    collected.extend(keep)
                if not page:
                    break
        except Exception as error:
            self.archive.note_skip(
                "rest_commits_failed", repository=repo, error=f"{type(error).__name__}: {error}"[:200]
            )
            return None
        self.archive.note_coverage(
            "github.commits_read_over_rest_for_unmirrored_repository: at least one repository "
            "had no mirror, so its commits came from the commits API. Those records carry the "
            "REST capture profile and no local diff statistics."
        )
        return collected

    def _list_repositories(self) -> list[dict[str, Any]]:
        """Every repository in the org, archived ones included.

        Fully paginated on purpose: the legacy `--limit 300` was already below
        the 366 mirrors on disk, and `gh` truncates a listing without saying
        so.
        """
        entries: list[dict[str, Any]] = []
        page: str | None = None
        pages = 0
        while True:
            items, page = self.client.list_repositories(page=page)
            pages += 1
            # A mirror-backed listing says so in the manifest rather than
            # claiming an endpoint it never called.
            endpoint = getattr(self.client, "repository_endpoint", None) or f"/orgs/{self.organization}/repos"
            self.archive.write_page(
                "repositories",
                {"organization": self.organization, "page": pages, "items": items},
                endpoint=endpoint,
                request={"page": pages, "type": "all", "archived": "included"},
                item_count=len(items),
            )
            entries.extend(item for item in items if isinstance(item, dict))
            if not page:
                break
        return entries

    def _select(
        self,
        listed: list[dict[str, Any]],
        *,
        requested: Sequence[str] | None,
        limit: int | None,
    ) -> list[dict[str, Any]]:
        entries = listed
        if requested:
            wanted = {name.lower() for name in requested}
            entries = [entry for entry in listed if (_repo_name(entry) or "").lower() in wanted]
            found = {(_repo_name(entry) or "").lower() for entry in entries}
            for name in sorted(wanted - found):
                # ext4 is case-sensitive where the mirrors were built on a
                # case-insensitive APFS volume, so a name that used to match
                # may not any more. Say so instead of reporting an empty day.
                self.archive.note_skip("repository_not_listed", repository=name)
        entries = sorted(entries, key=lambda entry: (_repo_name(entry) or "").lower())
        if limit is not None and len(entries) > limit:
            self.archive.note_truncation(
                "max_repositories", limit=limit, repositories_listed=len(entries)
            )
            entries = entries[:limit]
        return entries

    # ------------------------------------------------------------ commits

    def _mirror_freshness(
        self, repo: str, *, window: Window, pushed_at: datetime | None, verify_refs: bool
    ) -> dict[str, Any]:
        """Whether the mirror holds everything the remote has been pushed.

        Two earlier versions of this were wrong in opposite directions, and
        both mistakes are worth keeping written down.

        The first asked the filesystem when the mirror was last fetched. These
        bare clones carry no `remote.origin.fetch` refspec, so `git fetch`
        rewrites FETCH_HEAD -- moving the mtime -- while leaving `refs/` where
        it was. Mirrors looked fetched minutes ago and were a week behind;
        576 August commits went uncollected under a clean-looking signal.

        The second asked whether the mirror held a commit dated after the
        window. That is true but nearly useless: a repository with no commits
        in the first days of September is not behind, it is quiet, and the
        rule flagged 326 of 366 repositories -- 75 of which had in fact
        contributed August commits. A signal that fires on nine repositories
        in ten tells a reader nothing.

        What actually answers the question is the remote's own `pushed_at`,
        which the repository listing already carries. If the newest commit in
        the mirror is at least as new as the last push, the mirror is not
        behind. If the remote was pushed after what the mirror holds, and that
        push falls inside or after the window, commits in the window are
        genuinely missing -- that one can be stated as `False`, not merely
        unknown. And a repository whose last push predates the window has
        nothing in the window to miss.

        `pushed_at` is unavailable for a mirror the API no longer lists, and
        there coverage stays unknown rather than being guessed either way.
        """
        newest_at = self._mirror_newest_commit(repo)
        fetched_at = self._mirror_fetch_mtime(repo)
        details: dict[str, Any] = {
            "mirror_newest_commit_at": newest_at.isoformat() if newest_at else None,
            "remote_pushed_at": pushed_at.isoformat() if pushed_at else None,
            # Kept because it is the only signal for a mirror with no commits
            # at all, and labelled so no reader mistakes it for ref freshness.
            "mirror_touched_at": fetched_at.isoformat() if fetched_at else None,
        }
        if pushed_at is not None and pushed_at < window.start_at:
            # Nothing was pushed during or after the window, so there is
            # nothing in the window this mirror could be missing. This is the
            # one verdict the cheap signals can reach on their own.
            details["mirror_covers_window"] = True
            details["coverage_basis"] = "no push in or after the window"
            return details

        if verify_refs:
            behind = self._branches_behind(repo)
            if behind is None:
                details["mirror_covers_window"] = None
                details["coverage_basis"] = "ref comparison unavailable"
                self.archive.note_skip(
                    "mirror_coverage_unknown", repository=repo, reason="ref comparison failed"
                )
                return details
            details["mirror_branches_behind"] = len(behind)
            if behind:
                details["mirror_covers_window"] = False
                details["coverage_basis"] = "remote branches the mirror does not hold"
                self.archive.note_skip(
                    "mirror_behind_remote", repository=repo, branches=sorted(behind)[:20]
                )
            else:
                details["mirror_covers_window"] = True
                details["coverage_basis"] = "every remote branch matches"
            return details

        # Without the ref comparison there is no honest verdict left. The
        # remote's push time cannot be compared against a commit date -- a push
        # is always at least as new as the commits it carries, so that test
        # called 107 mirrors behind when a ref comparison found 4 of the first
        # 5 exactly in sync. Saying unknown costs a reader nothing; saying
        # "behind" wrongly costs them their trust in the signal.
        details["mirror_covers_window"] = None
        details["coverage_basis"] = "not verified"
        return details

    def _branches_behind(self, repo: str) -> set[str] | None:
        """Remote branches whose tip the mirror does not hold. None if unknown.

        This is the only comparison that answers the question, and it costs a
        network round trip per repository -- about 0.7 seconds, so roughly
        four minutes across the whole organisation. It is opt-in for that
        reason.
        """
        reader = getattr(self.mirrors, "branches_behind", None)
        if not callable(reader):
            return None
        try:
            return reader(repo)
        except Exception as error:
            self.archive.note_error(
                "ref_comparison_failed", repository=repo, error=f"{type(error).__name__}: {error}"[:200]
            )
            return None

    def _pushed_at_by_repository(self, listed: list[dict[str, Any]]) -> dict[str, Any]:
        entries = listed
        if getattr(self.client, "repository_endpoint", None):
            rest = getattr(self.client, "rest", None)
            if rest is None:
                return {}
            entries = []
            page: str | None = None
            try:
                while True:
                    items, page = rest.list_repositories(page=page)
                    entries.extend(items)
                    if not page:
                        break
            except Exception:
                # Already reported by the divergence pass; coverage simply
                # stays unknown rather than being guessed from the mirror.
                return {}
        return {
            name: entry.get("pushed_at")
            for entry in entries
            if isinstance(entry, dict) and (name := _repo_name(entry))
        }

    def _mirror_newest_commit(self, repo: str) -> datetime | None:
        reader = getattr(self.mirrors, "newest_commit_at", None)
        return reader(repo) if callable(reader) else None

    def _mirror_fetch_mtime(self, repo: str) -> datetime | None:
        reader = getattr(self.mirrors, "last_fetch_at", None)
        return reader(repo) if callable(reader) else None

    def _collect_commits(
        self, repo: str, *, window: Window, include_diffstat: bool
    ) -> list[dict[str, Any]]:
        if not self.mirrors.has_repository(repo):
            raise FileNotFoundError(repo)
        commits = self.mirrors.log(
            repo,
            since=window.start_at,
            until=window.end_at,
            include_diffstat=include_diffstat,
        )
        kept = [record for record in commits if window.contains(parse_timestamp(record.get("committed_at")))]
        dropped = len(commits) - len(kept)
        if dropped:
            # `git log --since/--until` is inclusive at second granularity and
            # works on commit dates; the exact window is enforced here so the
            # day keys and the manifest agree.
            self.archive.note_skip("commit_outside_window", repository=repo, commits=dropped)
        if kept:
            self.archive.write_page(
                f"commits-{repo}",
                {
                    "repository": repo,
                    "organization": self.organization,
                    "source": "bare_mirror",
                    "capture_profile": COMMIT_CAPTURE_PROFILE,
                    "window": window.as_dict(),
                    "include_diffstat": include_diffstat,
                    "commits": kept,
                },
                endpoint="git-log",
                request={"repository": repo, "since": window.start_at.isoformat(), "until": window.end_at.isoformat()},
                item_count=len(kept),
            )
        return kept

    # --------------------------------------------------------------- REST

    def _collect_rest_kind(
        self, kind: str, repo: str, *, window: Window, state: dict[str, Any]
    ) -> dict[str, int]:
        if kind == "pull_request":
            return self._collect_pull_requests(repo, window=window, state=state)
        if kind == "review":
            return self._collect_reviews(repo, window=window, state=state)
        if kind == "review_comment":
            return self._collect_since_listing(
                kind, repo, window=window, method=self.client.list_review_comments,
                endpoint=f"/repos/{self.organization}/{repo}/pulls/comments", date_field="created_at",
            )
        if kind == "issue_comment":
            return self._collect_since_listing(
                kind, repo, window=window, method=self.client.list_issue_comments,
                endpoint=f"/repos/{self.organization}/{repo}/issues/comments", date_field="created_at",
            )
        if kind == "issue":
            return self._collect_issues(repo, window=window)
        raise ValueError(f"unknown kind {kind}")

    def _write_rest_page(
        self, kind: str, repo: str, items: list[dict[str, Any]], *, endpoint: str, request: dict[str, Any]
    ) -> None:
        self.archive.write_page(
            f"{kind}-{repo}",
            {
                "repository": repo,
                "organization": self.organization,
                "kind": kind,
                "capture_profile": REST_CAPTURE_PROFILE,
                "items": items,
            },
            endpoint=endpoint,
            request=request,
            item_count=len(items),
        )

    def _collect_pull_requests(
        self, repo: str, *, window: Window, state: dict[str, Any]
    ) -> dict[str, int]:
        """Walk `/pulls` newest-updated-first and stop at the window edge.

        The endpoint has no `since`, so the walk itself is the filter. The stop
        decision is made on parsed instants: the legacy string comparison
        against a `+09:00` literal was off by the whole offset.
        """
        endpoint = f"/repos/{self.organization}/{repo}/pulls"
        counted: dict[str, int] = {}
        page: str | None = None
        pages = 0
        numbers: list[int] = state.setdefault("pull_request_numbers", [])
        while True:
            items, page = self.client.list_pull_requests(repo, page=page)
            pages += 1
            keep: list[dict[str, Any]] = []
            exhausted = False
            for item in items:
                updated = parse_timestamp(item.get("updated_at"))
                if updated is not None and updated < window.start_at:
                    exhausted = True
                    break
                if updated is not None and updated >= window.end_at:
                    continue
                keep.append(item)
            if keep:
                self._write_rest_page(
                    "pull_request", repo, keep,
                    endpoint=endpoint,
                    request={"state": "all", "sort": "updated", "direction": "desc", "page": pages},
                )
                for item in keep:
                    day = kst_day(parse_timestamp(item.get("updated_at")))
                    if day:
                        counted[day] = counted.get(day, 0) + 1
                    number = item.get("number")
                    if isinstance(number, int):
                        numbers.append(number)
            if exhausted or not page:
                break
        return counted

    def _collect_reviews(
        self, repo: str, *, window: Window, state: dict[str, Any]
    ) -> dict[str, int]:
        endpoint_template = f"/repos/{self.organization}/{repo}/pulls/{{number}}/reviews"
        counted: dict[str, int] = {}
        numbers = state.get("pull_request_numbers")
        if numbers is None:
            # Reviews are reachable only through the pull requests the window
            # turned up. Asking for reviews without pull requests would report
            # zero reviews, which reads as "nobody reviewed" rather than "we
            # never looked".
            self.archive.note_coverage(
                "github.reviews_require_pull_requests: this run requested reviews without "
                "pull requests, so no pull request was known to fetch reviews for."
            )
            return counted
        for number in sorted(set(numbers)):
            page: str | None = None
            while True:
                items, page = self.client.list_reviews(repo, number, page=page)
                keep = [
                    item
                    for item in items
                    if window.contains(parse_timestamp(item.get("submitted_at")))
                ]
                if keep:
                    self._write_rest_page(
                        "review", repo, keep,
                        endpoint=endpoint_template.format(number=number),
                        request={"pull_number": number},
                    )
                    for item in keep:
                        day = kst_day(parse_timestamp(item.get("submitted_at")))
                        if day:
                            counted[day] = counted.get(day, 0) + 1
                if not page:
                    break
        return counted

    def _collect_since_listing(
        self,
        kind: str,
        repo: str,
        *,
        window: Window,
        method: Callable[..., tuple[list[dict[str, Any]], str | None]],
        endpoint: str,
        date_field: str,
    ) -> dict[str, int]:
        counted: dict[str, int] = {}
        since = window.start_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        page: str | None = None
        pages = 0
        while True:
            items, page = method(repo, since=since, page=page)
            pages += 1
            keep = [item for item in items if window.contains(parse_timestamp(item.get(date_field)))]
            if keep:
                self._write_rest_page(
                    kind, repo, keep,
                    endpoint=endpoint,
                    request={"since": since, "page": pages},
                )
                for item in keep:
                    day = kst_day(parse_timestamp(item.get(date_field)))
                    if day:
                        counted[day] = counted.get(day, 0) + 1
            if not page:
                break
        return counted

    def _collect_issues(self, repo: str, *, window: Window) -> dict[str, int]:
        """Issues updated in the window, with pull requests excluded.

        GitHub lists pull requests as issues; they are captured by
        `_collect_pull_requests` with their pull-request fields intact, so
        keeping them here would double-count them.
        """
        endpoint = f"/repos/{self.organization}/{repo}/issues"
        counted: dict[str, int] = {}
        since = window.start_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        page: str | None = None
        pages = 0
        pull_requests_filtered = 0
        while True:
            items, page = self.client.list_issues(repo, since=since, page=page)
            pages += 1
            keep: list[dict[str, Any]] = []
            for item in items:
                if "pull_request" in item:
                    pull_requests_filtered += 1
                    continue
                if window.contains(parse_timestamp(item.get("updated_at"))):
                    keep.append(item)
            if keep:
                self._write_rest_page(
                    "issue", repo, keep,
                    endpoint=endpoint,
                    request={"state": "all", "since": since, "sort": "updated", "page": pages},
                )
                for item in keep:
                    day = kst_day(parse_timestamp(item.get("updated_at")))
                    if day:
                        counted[day] = counted.get(day, 0) + 1
            if not page:
                break
        if pull_requests_filtered:
            self.archive.note_skip(
                "issue_listing_pull_requests", repository=repo, filtered=pull_requests_filtered
            )
        return counted


# ------------------------------------------------------------------ wiring


def make_github_collector(
    *,
    client: GitHubRestClient,
    mirrors: MirrorReader,
    archive_root: Path,
    environment: str,
    organization: str,
    capture_density: str = "full",
    dry_run: bool = False,
    config_root: Path | None = None,
) -> tuple[RawArchive, GithubCollector]:
    run_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:12]}"
    archive = RawArchive(
        archive_root,
        GITHUB_SOURCE,
        run_id,
        environment,
        capture_profile=GITHUB_CAPTURE_PROFILE,
        capture_density=capture_density,
        dry_run=dry_run,
        config_root=config_root,
    )
    return archive, GithubCollector(client, mirrors, archive, organization=organization)
