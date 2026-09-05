"""GitHub REST access and bare-mirror reading for the GitHub collector.

Two concrete implementations of the protocols in `github_collector`:

  * `GhCliClient` shells out to `gh api` one page at a time. The legacy
    collector used `--paginate`, which fetches a repository's entire history
    before any window filter can look at it; paging here lets the collector
    stop at the window edge. The eight-attempt retry with a wait until the
    core rate limit resets is kept from the legacy collector, because that
    part was right: on a rate limit it waits rather than dropping data.
  * `GitMirrorReader` runs `git log` against the bare mirrors. Fields are
    separated by US (\\x1f) and records by RS (\\x1e) so a commit body with
    newlines and tabs survives intact -- the legacy collector split on tabs
    and kept only `%s`, so every commit body was lost.

The token comes from `<config_root>/credentials/github-token`, and the file
takes precedence over `GITHUB_TOKEN`, as with the other collectors. It is
passed to `gh` through the environment of the child process and is never
logged, never written to a manifest, and never returned by any function here.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .github_collector import GitHubApiError

GH_BIN = os.environ.get("GH_BIN") or "gh"
GIT_BIN = os.environ.get("GIT_BIN") or "git"

# Where the bare mirrors live when nobody says otherwise. `github-collect` and
# the daily batch have to agree on this: two answers would mean one of them
# reading a directory the other never fetched into, and reporting every
# repository it lacks as a skipped one.
DEFAULT_MIRROR_ROOT = "/data/rlwrld-worklog/legacy/claude/weekly/scripts/github_mirrors"

# One commit per record; fields in this order. `%aI`/`%cI` are strict ISO 8601
# with the offset, so they parse without guessing.
_COMMIT_FORMAT = (
    "%H%x1f%h%x1f%P%x1f%T%x1f"
    "%an%x1f%ae%x1f%aI%x1f"
    "%cn%x1f%ce%x1f%cI%x1f"
    "%s%x1f%B%x1e"
)
_COMMIT_FIELDS = (
    "sha",
    "sha_short",
    "parents_raw",
    "tree",
    "author_name",
    "author_email",
    "authored_at",
    "committer_name",
    "committer_email",
    "committed_at",
    "subject",
    "body",
)


def default_mirror_root() -> Path:
    """The mirror directory: `GITHUB_MIRROR_ROOT`, else the data disk."""
    return Path(os.environ.get("GITHUB_MIRROR_ROOT") or DEFAULT_MIRROR_ROOT)


def read_github_token(config_root: Path | None = None) -> str | None:
    """The token, file first. Returns None rather than raising."""
    root = config_root or Path(
        os.environ.get("APP_CONFIG_ROOT") or Path.home() / ".config/hk-work-assistant"
    )
    try:
        value = (root / "credentials" / "github-token").read_text(encoding="utf-8").strip()
    except (FileNotFoundError, NotADirectoryError, PermissionError, UnicodeDecodeError):
        value = ""
    return value or os.environ.get("GITHUB_TOKEN") or None


class GhCliClient:
    """`gh api`, one page per call, with rate-limit waits."""

    def __init__(
        self,
        organization: str,
        *,
        token: str | None = None,
        per_page: int = 100,
        max_attempts: int = 8,
        sleep: Any = time.sleep,
        config_root: Path | None = None,
    ) -> None:
        self.organization = organization
        self.per_page = per_page
        self.max_attempts = max_attempts
        self._sleep = sleep
        self._token = token if token is not None else read_github_token(config_root)
        self._rate_limit_waits = 0

    @property
    def rate_limit_waits(self) -> int:
        return self._rate_limit_waits

    # --------------------------------------------------------------- shell

    def _environment(self) -> dict[str, str]:
        environment = dict(os.environ)
        if self._token:
            # `gh` reads GH_TOKEN; the value never leaves this dict.
            environment["GH_TOKEN"] = self._token
            environment.pop("GITHUB_TOKEN", None)
        return environment

    def _run(self, arguments: list[str], *, timeout: int = 120) -> tuple[int, str, str]:
        try:
            completed = subprocess.run(
                arguments,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=self._environment(),
            )
        except subprocess.TimeoutExpired:
            return -1, "", "timeout"
        except FileNotFoundError:
            return -1, "", f"command not found: {arguments[0]}"
        return completed.returncode, completed.stdout, completed.stderr

    def _reset_wait_seconds(self, *, secondary: bool) -> int:
        if secondary:
            return 60
        code, out, _ = self._run([GH_BIN, "api", "rate_limit"], timeout=30)
        if code == 0 and out.strip():
            try:
                core = json.loads(out)["resources"]["core"]
                wait = int(core["reset"] - time.time()) + 5
                return max(5, min(wait, 3700))
            except (KeyError, TypeError, ValueError):
                pass
        return 120

    def _api(self, path: str, parameters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        arguments = [GH_BIN, "api", "--method", "GET", "-H", "Accept: application/vnd.github+json"]
        query = dict(parameters or {})
        query.setdefault("per_page", self.per_page)
        for key, value in query.items():
            if value is None:
                continue
            arguments.extend(["-f", f"{key}={value}"])
        arguments.append(path)

        for attempt in range(1, self.max_attempts + 1):
            code, out, err = self._run(arguments)
            if code == 0:
                return _parse_json_items(out)
            lowered = (err or "").lower()
            secondary = "secondary rate limit" in lowered or "abuse" in lowered
            primary = "rate limit exceeded" in lowered and not secondary
            if not (primary or secondary):
                raise GitHubApiError(f"{path}: {(err or '').strip()[:200]}")
            if attempt == self.max_attempts:
                raise GitHubApiError(f"{path}: rate limit persisted for {attempt} attempts")
            self._rate_limit_waits += 1
            self._sleep(self._reset_wait_seconds(secondary=secondary))
        raise GitHubApiError(f"{path}: exhausted attempts")

    def _paged(
        self, path: str, parameters: dict[str, Any], page: str | None
    ) -> tuple[list[dict[str, Any]], str | None]:
        number = int(page or 1)
        items = self._api(path, {**parameters, "page": number})
        # GitHub signals the end of a listing with a short page. Asking for one
        # page past the end costs a request and returns [], so a full page is
        # the only reason to continue.
        next_page = str(number + 1) if len(items) >= self.per_page else None
        return items, next_page

    # -------------------------------------------------------------- calls

    def list_repositories(self, *, page: str | None = None) -> tuple[list[dict[str, Any]], str | None]:
        return self._paged(
            f"/orgs/{self.organization}/repos", {"type": "all", "sort": "full_name"}, page
        )

    def list_pull_requests(self, repo: str, *, page: str | None = None):
        return self._paged(
            f"/repos/{self.organization}/{repo}/pulls",
            {"state": "all", "sort": "updated", "direction": "desc"},
            page,
        )

    def list_reviews(self, repo: str, number: int, *, page: str | None = None):
        return self._paged(f"/repos/{self.organization}/{repo}/pulls/{number}/reviews", {}, page)

    def list_review_comments(self, repo: str, *, since: str, page: str | None = None):
        return self._paged(
            f"/repos/{self.organization}/{repo}/pulls/comments",
            {"since": since, "sort": "created", "direction": "asc"},
            page,
        )

    def list_issue_comments(self, repo: str, *, since: str, page: str | None = None):
        return self._paged(
            f"/repos/{self.organization}/{repo}/issues/comments",
            {"since": since, "sort": "created", "direction": "asc"},
            page,
        )

    def list_commits(self, repo: str, *, since: str, until: str, page: str | None = None):
        return self._paged(
            f"/repos/{self.organization}/{repo}/commits", {"since": since, "until": until}, page
        )

    def list_issues(self, repo: str, *, since: str, page: str | None = None):
        return self._paged(
            f"/repos/{self.organization}/{repo}/issues",
            {"state": "all", "since": since, "sort": "updated", "direction": "asc"},
            page,
        )


def _parse_json_items(text: str) -> list[dict[str, Any]]:
    body = (text or "").strip()
    if not body:
        return []
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as error:
        raise GitHubApiError(f"unparseable response: {error}") from error
    if isinstance(parsed, list):
        return [item for item in parsed if isinstance(item, dict)]
    if isinstance(parsed, dict):
        # Search-style endpoints wrap the list; the collector does not use them
        # today, but a wrapped body should not be read as a single item.
        items = parsed.get("items")
        if isinstance(items, list):
            return [item for item in items if isinstance(item, dict)]
        return [parsed]
    return []


class GitMirrorReader:
    """Commits out of `<root>/<repo>.git`, with no network and no API budget."""

    def __init__(self, root: Path, *, timeout: int = 300) -> None:
        self.root = Path(root)
        self.timeout = timeout

    def path_for(self, repo: str) -> Path:
        return self.root / f"{repo}.git"

    def repositories(self) -> list[str]:
        """Every mirrored repository name, from the directory itself."""
        return sorted(path.name[: -len(".git")] for path in self.root.glob("*.git"))

    def has_repository(self, repo: str) -> bool:
        return self.path_for(repo).is_dir()

    def branches_behind(self, repo: str) -> set[str] | None:
        """Remote branch names whose tip this mirror does not hold.

        `git ls-remote` against the mirror's own origin, compared with the
        refs on disk. An empty set means the mirror is current; None means the
        comparison could not be made and the caller must not read that as
        either answer.
        """
        directory = self.path_for(repo)
        if not directory.is_dir():
            return None
        try:
            remote = subprocess.run(
                [GIT_BIN, "ls-remote", "--heads", "origin"],
                cwd=str(directory), capture_output=True, text=True, timeout=self.timeout,
            )
            local = subprocess.run(
                [GIT_BIN, "for-each-ref", "--format=%(objectname) %(refname:strip=3)",
                 "refs/remotes/origin", "refs/heads"],
                cwd=str(directory), capture_output=True, text=True, timeout=self.timeout,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if remote.returncode != 0:
            return None
        held: set[str] = set()
        for line in local.stdout.splitlines():
            parts = line.split(" ", 1)
            if len(parts) == 2:
                held.add(parts[0])
        behind: set[str] = set()
        for line in remote.stdout.splitlines():
            parts = line.split("\t", 1)
            if len(parts) != 2:
                continue
            sha, ref = parts[0].strip(), parts[1].strip()
            if sha not in held:
                behind.add(ref.removeprefix("refs/heads/"))
        return behind

    def newest_commit_at(self, repo: str) -> datetime | None:
        """The newest commit the mirror actually holds, across every ref.

        This is what the refs can be asked; the filesystem's fetch time cannot
        answer it, because a fetch with no refspec updates FETCH_HEAD without
        moving a ref.
        """
        directory = self.path_for(repo)
        if not directory.is_dir():
            return None
        try:
            completed = subprocess.run(
                [GIT_BIN, "log", "--all", "-1", "--format=%cI"],
                cwd=str(directory), capture_output=True, text=True, timeout=self.timeout,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if completed.returncode != 0 or not completed.stdout.strip():
            return None
        text = completed.stdout.strip()
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

    def last_fetch_at(self, repo: str) -> datetime | None:
        """When git last touched this mirror. NOT a measure of ref freshness.

        `git fetch` rewrites FETCH_HEAD even when it brings nothing, and these
        clones carry no fetch refspec, so this timestamp moves while `refs/`
        stands still. Use `newest_commit_at` to ask what the mirror holds.
        """
        directory = self.path_for(repo)
        stamps = []
        for name in ("FETCH_HEAD", "packed-refs", "HEAD"):
            candidate = directory / name
            try:
                stamps.append(candidate.stat().st_mtime)
            except (FileNotFoundError, NotADirectoryError, PermissionError):
                continue
        if not stamps:
            return None
        return datetime.fromtimestamp(max(stamps), timezone.utc)

    def log(
        self, repo: str, *, since: datetime, until: datetime, include_diffstat: bool
    ) -> list[dict[str, Any]]:
        directory = self.path_for(repo)
        if not directory.is_dir():
            raise FileNotFoundError(str(directory))
        window = [f"--since={since.isoformat()}", f"--until={until.isoformat()}"]
        commits = list(
            _parse_commit_records(
                self._git(directory, ["log", "--all", *window, f"--pretty=format:{_COMMIT_FORMAT}"], repo),
                repository=repo,
            )
        )
        if not include_diffstat or not commits:
            return commits
        # Deliberately a second pass. `--numstat` prints its lines *after* the
        # pretty-format record, so a single call puts each commit's statistics
        # at the head of the next record and they are lost on the split. Two
        # local git calls are cheap; a silently empty diffstat is not.
        statistics = _parse_numstat_pass(
            self._git(directory, ["log", "--all", *window, "--format=%x1e%H", "--numstat"], repo)
        )
        for record in commits:
            files = statistics.get(record["sha"])
            if files is None:
                # A merge commit produces no numstat lines without `-m`, so its
                # statistics are absent rather than zero. Saying "0 files" for
                # a merge would be a quiet lie.
                record["diffstat_status"] = "absent_for_merge" if record["is_merge"] else "absent"
                continue
            record["files"] = files
            record["files_changed"] = len(files)
            record["insertions"] = sum(entry["additions"] for entry in files)
            record["deletions"] = sum(entry["deletions"] for entry in files)
            record["diffstat_status"] = "recorded"
        return commits

    def _git(self, directory: Path, arguments: list[str], repo: str) -> str:
        completed = subprocess.run(
            [GIT_BIN, *arguments],
            cwd=str(directory),
            capture_output=True,
            text=True,
            timeout=self.timeout,
        )
        if completed.returncode != 0:
            raise GitHubApiError(f"git {arguments[0]} failed for {repo}: {completed.stderr.strip()[:200]}")
        return completed.stdout


def _parse_commit_records(text: str, *, repository: str):
    for chunk in (text or "").split("\x1e"):
        if not chunk.strip():
            continue
        # `git log --pretty=format:` joins records with a newline, so every
        # record after the first arrives with that separator still attached to
        # its first field. Leaving it there put a newline in front of the sha
        # and made the commit unjoinable with its own diff statistics.
        parts = chunk.lstrip("\n").split("\x1f")
        if len(parts) < len(_COMMIT_FIELDS):
            continue
        record = dict(zip(_COMMIT_FIELDS, parts))
        record["sha"] = record["sha"].strip()
        record["sha_short"] = record["sha_short"].strip()
        parents = [value for value in (record.pop("parents_raw", "") or "").split() if value]
        record["parents"] = parents
        # A merge commit has more than one parent. The legacy collector passed
        # --no-merges and lost exactly these, which is where a merged pull
        # request lands.
        record["parent_count"] = len(parents)
        record["is_merge"] = len(parents) > 1
        record["repository"] = repository
        yield record


def _parse_numstat_pass(text: str) -> dict[str, list[dict[str, Any]]]:
    """`%x1e<sha>` followed by that commit's numstat lines, per record."""
    statistics: dict[str, list[dict[str, Any]]] = {}
    for chunk in (text or "").split("\x1e"):
        lines = [line for line in chunk.strip("\n").split("\n") if line.strip()]
        if not lines:
            continue
        sha = lines[0].strip()
        if not sha:
            continue
        statistics[sha] = _parse_numstat("\n".join(lines[1:])) or []
    return statistics


def _parse_numstat(text: str) -> list[dict[str, Any]] | None:
    body = (text or "").strip("\x00\n ")
    if not body:
        return None
    files: list[dict[str, Any]] = []
    for line in body.replace("\x00", "\n").split("\n"):
        columns = line.split("\t")
        if len(columns) < 3:
            continue
        added, deleted, path = columns[0], columns[1], columns[2]
        files.append(
            {
                "path": path,
                # A binary file reports "-" for both counts; it is recorded as
                # binary rather than as a zero-line change.
                "additions": int(added) if added.isdigit() else 0,
                "deletions": int(deleted) if deleted.isdigit() else 0,
                "binary": not (added.isdigit() and deleted.isdigit()),
            }
        )
    return files


class MirrorRepositoryLister:
    """Lists repositories from the mirror directory instead of the API.

    A commit-only backfill then needs no API budget, no token and no network
    at all: the mirrors are already on disk. It also removes the failure mode
    where a rate limit on the repository listing stops a run that was never
    going to call the API again.

    Every other call is delegated to the wrapped REST client, so a run that
    does want pull requests still gets them.
    """

    repository_endpoint = "git-mirror-directory"

    def __init__(self, reader: "GitMirrorReader", rest: Any | None = None) -> None:
        self.reader = reader
        self.rest = rest

    @property
    def rate_limit_waits(self) -> int:
        return int(getattr(self.rest, "rate_limit_waits", 0) or 0)

    def list_repositories(self, *, page: str | None = None) -> tuple[list[dict[str, Any]], str | None]:
        if page:
            return [], None
        names = self.reader.repositories()
        return (
            [
                {
                    "name": name,
                    # Archived state and visibility are API facts the mirror
                    # directory does not hold. Reported as unknown rather than
                    # guessed as False, which would read as "not archived".
                    "archived": None,
                    "visibility": None,
                    "listed_from": "mirror_directory",
                }
                for name in names
            ],
            None,
        )

    def __getattr__(self, name: str) -> Any:
        if self.rest is None:
            raise AttributeError(
                f"{name} needs a REST client; this run was built for mirrors only"
            )
        return getattr(self.rest, name)
