"""One daily incremental run across all five sources.

Shape of a run, per source and in this order:

    capture  official read-only API  ->  immutable raw archive + run manifest
    ledger   raw manifest            ->  validated standard v1 ledger JSONL
    load     ledger JSONL            ->  service PostgreSQL (optional)

The three stages are reported separately and fail separately. A capture that
succeeded stays successful and usable even when the ledger projection or the
database load afterwards fails: the raw bytes are already on disk and both
later stages are re-runnable from them.

Sources are isolated from each other. One source raising cannot stop another
from running, and cannot touch another source's checkpoint — each collector
owns its own checkpoint file under its own manifest directory.

Source order is fixed rather than taken from the command line: Slack and
Calendar feed the Notion link queue, so Notion runs last and drains what they
discovered in the same run. GitHub and Slurm discover no Notion URL today, but
they are ordered before Notion anyway, so the rule stays the single sentence
"Notion runs last" rather than a list of which sources happen to feed it.

Slack, Calendar and Notion are given a `since` instant. GitHub and Slurm are
given a window of KST calendar days instead, because that is the key their
records are filed under; `_kst_window` makes that conversion and is the only
place in the codebase that makes it.

A run may also carry an exclusive `until`, which turns it from "resume from
each checkpoint" into "capture this one historical window". Such a run moves no
checkpoint — see `_advance_checkpoint` — and is refused outright for a source
that cannot express an upper bound, rather than quietly collecting the live
head instead.
"""

from __future__ import annotations

import fcntl
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from .archive import RawArchive

if TYPE_CHECKING:  # collectors are imported lazily, inside each capture
    from .github_collector import Window

SOURCE_ORDER = ("slack", "google-calendar", "github", "slurm", "notion")

# Sources whose collectors accept an exclusive upper bound, and can therefore
# capture one historical slice instead of resuming from a checkpoint.
#
# Calendar is the one that cannot. Its incremental read is a per-calendar sync
# token and Google offers no way to ask for "events changed before X", so a
# bound cannot be expressed at all -- only ignored. A run given `--until` that
# quietly collected the live head instead would be the one outcome a backfill
# must never get, so the run is refused rather than widened.
UNTIL_CAPABLE = ("slack", "notion", "github", "slurm")

LEDGER_SOURCE = {
    "slack": "slack",
    "notion": "notion",
    "google-calendar": "google_calendar",
    "github": "github",
    "slurm": "slurm",
}

DEFAULT_SINCE = "26h"
DEFAULT_ARCHIVE_ROOT = "/data/rlwrld-worklog"

# A smoke run must cost a predictable, tiny number of API calls.
SMOKE_LIMITS = {
    "slack_max_channels": 2,
    "slack_max_messages": 25,
    "slack_use_search": False,
    "notion_max_objects": 5,
    "notion_recheck_limit": 0,
    "notion_comment_request_budget": 20,
    "calendar_max_calendars": 2,
    "github_max_repositories": 2,
    # No REST kind at all. GitHub's commits come from the local mirrors, so a
    # commit-only capture still proves the whole path -- listing, mirror read,
    # archive, manifest -- for the one API call the repository listing costs.
    # Any REST kind costs at least one more call per repository.
    "github_kinds": (),
    # One cloud, not three. The dump endpoint offers no time query and no
    # pagination, so the smallest thing Slurm can fetch is one cloud's entire
    # export; asking for all three would download what a full run downloads.
    # `clouds_attempted` in the manifest records which one was asked for, so
    # the bound is never mistaken for two quiet clouds.
    "slurm_clouds": ("kakao",),
}

EXIT_OK = 0
EXIT_CAPTURE_FAILED = 1
EXIT_DOWNSTREAM_FAILED = 2
EXIT_LOCKED = 3


# ------------------------------------------------------------- credentials


SECRET_FILENAMES = {
    "slack_token": "slack-token",
    "notion_token": "notion-token",
    "google_token": "google-token.json",
    "github_token": "github-token",
}

DEFAULT_GITHUB_ORGANIZATION = "rlwrld"


@dataclass(frozen=True)
class Credentials:
    """Read-only view of the admin-managed credentials under APP_CONFIG_ROOT.

    Files win over environment variables: the backoffice is the place an
    operator rotates a token, and a stale shell export must not override it.
    Nothing here is ever printed.
    """

    config_root: Path
    slack_token: str | None
    notion_token: str | None
    github_token: str | None
    google_token_path: Path | None
    settings: dict[str, Any]

    @property
    def slack_expected_team_id(self) -> str | None:
        value = self.settings.get("slack_expected_team_id") or os.environ.get("SLACK_EXPECTED_TEAM_ID")
        return str(value) if value else None

    @property
    def github_organization(self) -> str:
        value = self.settings.get("github_organization") or os.environ.get("GITHUB_ORG")
        return str(value) if value else DEFAULT_GITHUB_ORGANIZATION

    def availability(self) -> dict[str, bool]:
        return {
            "slack": bool(self.slack_token),
            "notion": bool(self.notion_token),
            "google-calendar": bool(self.google_token_path and self.google_token_path.is_file()),
            "github": bool(self.github_token),
            # Slurm is listed with the others and is always true rather than
            # left out. Its dump endpoint is an unauthenticated request on the
            # tailnet, so there is no credential that can be missing; a
            # five-source map with four entries would read as a lost token.
            "slurm": True,
        }


def _read_secret(path: Path) -> str | None:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, NotADirectoryError, PermissionError, UnicodeDecodeError):
        return None
    return value or None


def load_credentials(config_root: Path | None = None) -> Credentials:
    root = config_root or Path(
        os.environ.get("APP_CONFIG_ROOT") or Path.home() / ".config/hk-work-assistant"
    )
    credentials_dir = root / "credentials"
    settings: dict[str, Any] = {}
    try:
        parsed = json.loads((root / "settings.json").read_text(encoding="utf-8"))
        if isinstance(parsed, dict):
            settings = parsed
    except (FileNotFoundError, NotADirectoryError, PermissionError, json.JSONDecodeError):
        settings = {}

    google_path: Path | None = credentials_dir / SECRET_FILENAMES["google_token"]
    if google_path is not None and not google_path.is_file():
        configured = os.environ.get("GOOGLE_TOKEN_PATH")
        google_path = Path(configured) if configured else Path("secrets/google-token.json")
        if not google_path.is_file():
            google_path = None

    return Credentials(
        config_root=root,
        slack_token=_read_secret(credentials_dir / SECRET_FILENAMES["slack_token"])
        or os.environ.get("SLACK_USER_TOKEN"),
        notion_token=_read_secret(credentials_dir / SECRET_FILENAMES["notion_token"])
        or os.environ.get("NOTION_TOKEN"),
        github_token=_read_secret(credentials_dir / SECRET_FILENAMES["github_token"])
        or os.environ.get("GITHUB_TOKEN"),
        google_token_path=google_path,
        settings=settings,
    )


# ------------------------------------------------------------------ config


@dataclass
class DailyConfig:
    archive_root: Path
    ledger_root: Path
    environment: str = "production"
    since: str = DEFAULT_SINCE
    # Exclusive upper bound, as a KST date or an ISO 8601 instant. Set, the run
    # is a historical slice: it reads one bounded window and moves no
    # checkpoint. Unset, the run resumes from each source's checkpoint.
    until: str | None = None
    sources: tuple[str, ...] = SOURCE_ORDER
    database_url: str | None = None
    load_database: bool = True
    dry_run: bool = False
    smoke: bool = False
    config_root: Path | None = None
    lock_path: Path | None = None

    def __post_init__(self) -> None:
        # A smoke run is bounded by construction and must never move a
        # production checkpoint, so it always implies a dry run.
        if self.smoke:
            self.dry_run = True
        if self.until is not None:
            self._validate_until()

    def _validate_until(self) -> None:
        """Refuse an unusable slice here, before the lock and the first API call.

        Every one of these is a request that cannot be honoured as asked, and
        the alternative to refusing is a run that quietly collects a different
        window than the operator named -- which is indistinguishable, in the
        archive and on the dashboard, from a window that was genuinely empty.
        """
        from .slack_collector import parse_since, parse_until

        refused = [
            source
            for source in self.sources
            if source in SOURCE_ORDER and source not in UNTIL_CAPABLE
        ]
        if refused:
            raise ValueError(
                f"--until cannot be honoured for {', '.join(sorted(refused))}: that source "
                "resumes from a sync token rather than a time window, so a bounded slice "
                "cannot be expressed. Name the sources that can take one with --source."
            )
        until = parse_until(self.until)
        if until <= parse_since(self.since):
            raise ValueError(
                f"--until {self.until} is not after --since {self.since}; that window holds "
                "nothing, and an empty run is reported the same way a quiet day is."
            )

    @property
    def capture_density(self) -> str:
        if self.smoke:
            return "smoke"
        return "dry-run" if self.dry_run else "full"

    def resolved_lock_path(self) -> Path:
        return self.lock_path or self.archive_root / "locks" / f"daily-collect-{self.environment}.lock"


@dataclass
class StageResult:
    stage: str
    status: str  # ok | failed | skipped
    detail: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"stage": self.stage, "status": self.status, "detail": self.detail, "error": self.error}


@dataclass
class SourceResult:
    source: str
    status: str = "pending"  # ok | degraded | failed | skipped
    run_id: str | None = None
    manifest_path: str | None = None
    checkpoint_advanced: bool = False
    stages: list[StageResult] = field(default_factory=list)
    reason: str | None = None

    def add(self, stage: StageResult) -> StageResult:
        self.stages.append(stage)
        return stage

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "status": self.status,
            "run_id": self.run_id,
            "manifest": self.manifest_path,
            "checkpoint_advanced": self.checkpoint_advanced,
            "reason": self.reason,
            "stages": [stage.as_dict() for stage in self.stages],
        }


@dataclass
class CaptureOutcome:
    archive: RawArchive
    manifest_path: Path
    summary: dict[str, Any]
    checkpoint_advanced: bool
    # What this capture saw that Notion still has to resolve. The runner, not
    # the capture, writes the link queue, so there is exactly one place where
    # a discovered Notion URL enters the pipeline.
    events: tuple[Any, ...] = ()
    notion_urls: tuple[str, ...] = ()
    # Set when the capture finished but did not see everything it set out to.
    # The stage still ran, so it stays `ok`; the source is reported `degraded`
    # so a run summary can never look cleaner than the manifest behind it.
    degraded_reason: str | None = None


CaptureFn = Callable[[DailyConfig, Credentials], CaptureOutcome]


# ----------------------------------------------------------------- capture


def _kst_window(config: DailyConfig, *, now: datetime | None = None) -> Window:
    """The window of KST calendar days that `config.since` covers.

    Slack, Calendar and Notion take an instant. GitHub and Slurm take a closed
    interval of KST calendar days (`github_collector.py:175-219`), because a
    KST date is the key their records are filed under and every window decision
    they make is a comparison against that day's boundaries. This builds the
    same `Window` `github-collect` builds (`cli.py:396`); the only difference
    is that the two dates are read off the daily run's instant instead of off
    the command line, so the codebase keeps one window convention, not two.

    Both edges round outwards, and that rounding is the point of the
    conversion:

      * the start is the whole KST day that *contains* the since instant. A day
        is the smallest unit these two sources can express, so the alternative
        to re-reading that day's earlier hours is never reading them at all.
        Re-reading costs nothing: a commit sha and a job id are stable
        identities, the ledger is keyed by them, and the archive refuses to
        rewrite a page it already holds.
      * the end is today's KST date, not the moment the run started, because a
        day still in progress is still the day its records are filed under. The
        next run re-reads it and picks up whatever arrived after this one.

    `config.until` replaces that end. It is an exclusive instant, so the window
    ends on the KST day holding the last moment the bound admits: a bound at
    midnight KST ends the window on the previous day, which is what makes
    `--until 2026-09-01` cover exactly August. A bound mid-day rounds up to
    that whole day, the same direction the start edge rounds.
    """
    from .github_collector import KST, Window
    from .slack_collector import parse_since, parse_until

    current = now or datetime.now(timezone.utc)
    start = parse_since(config.since, now=current).astimezone(KST).date()
    if config.until is not None:
        end = (parse_until(config.until) - timedelta(microseconds=1)).astimezone(KST).date()
    else:
        end = current.astimezone(KST).date()
    # A `--since` in the future would otherwise describe an interval holding no
    # days, which a collector reports as a clean empty run.
    return Window(start, max(start, end))


def _advance_checkpoint(config: DailyConfig) -> bool:
    """Whether this run is allowed to move a checkpoint at all.

    A dry run is not, and neither is a slice: a watermark moved to a slice's
    end would assert that everything before that date had been read, and the
    months between it and the previous watermark would never be fetched again.
    Slack and Notion force this themselves the moment they see an `until`, and
    GitHub and Slurm are given `backfill=True` for the same reason -- but a
    guarantee that depends on four collectors each remembering it is not a
    guarantee, so the answer is decided here as well.
    """
    return not config.dry_run and config.until is None


def capture_slack(config: DailyConfig, credentials: Credentials) -> CaptureOutcome:
    from .slack_collector import make_slack_collector, parse_since, parse_until

    if not credentials.slack_token:
        raise RuntimeError("no Slack user token is available; add it in the backoffice first")
    _, archive, collector = make_slack_collector(
        archive_root=config.archive_root,
        environment=config.environment,
        token=credentials.slack_token,
        capture_density=config.capture_density,
        dry_run=config.dry_run,
        config_root=config.config_root,
    )
    try:
        result = collector.collect(
            since=parse_since(config.since),
            until=parse_until(config.until) if config.until else None,
            expected_team_id=credentials.slack_expected_team_id,
            max_channels=SMOKE_LIMITS["slack_max_channels"] if config.smoke else None,
            max_messages=SMOKE_LIMITS["slack_max_messages"] if config.smoke else None,
            use_search=not config.smoke,
            advance_checkpoint=_advance_checkpoint(config),
        )
    except Exception as error:
        raise CaptureFailed(archive, error) from error
    return CaptureOutcome(
        archive=archive,
        manifest_path=result.manifest_path,
        checkpoint_advanced=result.checkpoint_advanced,
        events=result.events,
        summary={
            "run_id": result.run_id,
            "channels_seen": result.channels_seen,
            "channels_collected": result.channels_collected,
            "channels_skipped": result.channels_skipped,
            "messages_seen": result.messages_seen,
            "threads_repolled": result.threads_repolled,
            "search_matches_kept": result.search_matches_kept,
            "search_matches_context_filtered": result.search_matches_context_filtered,
            "search_matches_before_window": result.search_matches_before_window,
            "events": len(result.events),
            "truncated": result.truncated,
        },
    )


def capture_google_calendar(config: DailyConfig, credentials: Credentials) -> CaptureOutcome:
    from .calendar_collector import make_calendar_collector
    from .google_auth import CALENDAR_READONLY_SCOPE, load_credentials as load_google_credentials
    from .slack_collector import parse_since

    if not credentials.google_token_path:
        raise RuntimeError("no Google token is available; authorize it in the backoffice first")
    google = load_google_credentials(credentials.google_token_path, [CALENDAR_READONLY_SCOPE])
    archive, collector = make_calendar_collector(
        credentials=google,
        archive_root=config.archive_root,
        environment=config.environment,
        capture_density=config.capture_density,
        dry_run=config.dry_run,
        config_root=config.config_root,
    )
    try:
        # No `until` here: Calendar cannot express one, which is why a run that
        # names it is refused when the config is built (`DailyConfig._validate_until`).
        result = collector.collect(
            since=parse_since(config.since),
            max_calendars=SMOKE_LIMITS["calendar_max_calendars"] if config.smoke else None,
            advance_checkpoint=_advance_checkpoint(config),
        )
    except Exception as error:
        raise CaptureFailed(archive, error) from error
    return CaptureOutcome(
        archive=archive,
        manifest_path=result.manifest_path,
        checkpoint_advanced=result.checkpoint_advanced,
        events=result.events,
        notion_urls=result.notion_urls,
        summary={
            "run_id": result.run_id,
            "calendars_seen": result.calendars_seen,
            "calendars_collected": result.calendars_collected,
            "calendars_skipped": result.calendars_skipped,
            "reset_calendars": list(result.reset_calendars),
            "events_archived": result.events_archived,
            "cancelled_events": result.cancelled_events,
            "events": len(result.events),
        },
    )


def capture_github(config: DailyConfig, credentials: Credentials) -> CaptureOutcome:
    from .github_client import GhCliClient, GitMirrorReader, default_mirror_root
    from .github_collector import REST_KINDS, make_github_collector

    if not credentials.github_token:
        raise RuntimeError("no GitHub token is available; add it in the backoffice first")
    organization = credentials.github_organization
    archive, collector = make_github_collector(
        client=GhCliClient(organization, config_root=config.config_root),
        mirrors=GitMirrorReader(default_mirror_root()),
        archive_root=config.archive_root,
        environment=config.environment,
        organization=organization,
        capture_density=config.capture_density,
        dry_run=config.dry_run,
        config_root=config.config_root,
    )
    try:
        result = collector.collect(
            window=_kst_window(config),
            kinds=SMOKE_LIMITS["github_kinds"] if config.smoke else REST_KINDS,
            max_repositories=SMOKE_LIMITS["github_max_repositories"] if config.smoke else None,
            # The diffstat is a second `git log` pass per repository and costs
            # no API budget, so a full run always takes it. A smoke run is
            # about proving the path, not about measuring the change.
            include_diffstat=not config.smoke,
            # A slice is a backfill: `backfill=True` makes the run independent
            # of the checkpoint in both directions, which is what the window
            # already is once an upper bound is named.
            backfill=config.until is not None,
            advance_checkpoint=_advance_checkpoint(config),
        )
    except Exception as error:
        raise CaptureFailed(archive, error) from error
    return CaptureOutcome(
        archive=archive,
        manifest_path=result.manifest_path,
        checkpoint_advanced=result.checkpoint_advanced,
        summary={
            "run_id": result.run_id,
            "window": result.window.as_dict(),
            "organization": organization,
            "repositories_listed": result.repositories_listed,
            "repositories_collected": result.repositories_collected,
            "repositories_skipped": result.repositories_skipped,
            "commits": result.commits,
            "commit_rows_archived": result.commit_rows,
            "rest_counts": dict(result.rest_counts),
        },
    )


def capture_slurm(config: DailyConfig, credentials: Credentials) -> CaptureOutcome:
    # Slurm takes no credential: the dump endpoint is an unauthenticated
    # request on the tailnet. The presigned S3 URL it redirects to is treated
    # as one, and never reaches a manifest, a log or this summary.
    from .slurm_client import SlurmDumpFetcher
    from .slurm_collector import CLOUDS, make_slurm_collector

    archive, collector = make_slurm_collector(
        fetcher=SlurmDumpFetcher(),
        archive_root=config.archive_root,
        environment=config.environment,
        capture_density=config.capture_density,
        dry_run=config.dry_run,
        config_root=config.config_root,
    )
    try:
        result = collector.collect(
            window=_kst_window(config),
            clouds=SMOKE_LIMITS["slurm_clouds"] if config.smoke else CLOUDS,
            backfill=config.until is not None,
            advance_checkpoint=_advance_checkpoint(config),
        )
    except Exception as error:
        raise CaptureFailed(archive, error) from error
    return CaptureOutcome(
        archive=archive,
        manifest_path=result.manifest_path,
        checkpoint_advanced=result.checkpoint_advanced,
        summary={
            "run_id": result.run_id,
            "window": result.window.as_dict(),
            "clouds_attempted": list(result.clouds_attempted),
            "clouds_collected": list(result.clouds_collected),
            "jobs": result.jobs,
            "parent_rows_archived": result.parent_rows,
            "step_rows": result.step_rows,
        },
    )


def capture_notion(config: DailyConfig, credentials: Credentials) -> CaptureOutcome:
    from .notion_collector import make_notion_collector
    from .slack_collector import parse_since, parse_until

    if not credentials.notion_token:
        raise RuntimeError("no Notion token is available; add it in the backoffice first")
    archive, collector = make_notion_collector(
        token=credentials.notion_token,
        archive_root=config.archive_root,
        environment=config.environment,
        capture_density=config.capture_density,
        dry_run=config.dry_run,
        config_root=config.config_root,
    )
    try:
        result = collector.collect(
            since=parse_since(config.since),
            until=parse_until(config.until) if config.until else None,
            max_objects=SMOKE_LIMITS["notion_max_objects"] if config.smoke else None,
            recheck_limit=SMOKE_LIMITS["notion_recheck_limit"] if config.smoke else 100,
            # None means exhaustive: a full run must not cap its own comment
            # sweep, or it would truncate itself and never advance.
            comment_request_budget=(
                SMOKE_LIMITS["notion_comment_request_budget"] if config.smoke else None
            ),
            advance_checkpoint=_advance_checkpoint(config),
        )
    except Exception as error:
        raise CaptureFailed(archive, error) from error
    return CaptureOutcome(
        archive=archive,
        manifest_path=result.manifest_path,
        checkpoint_advanced=result.checkpoint_advanced,
        degraded_reason=_notion_degraded_reason(result),
        summary={
            "run_id": result.run_id,
            "capture_status": result.status,
            "coverage_complete": result.coverage_complete,
            "objects_discovered": result.pages_discovered,
            "objects_completed": result.objects_completed,
            "pages_collected": result.pages_collected,
            "pages_skipped": result.pages_skipped,
            # Objects the API never gave a final answer for. Non-zero means the
            # run is degraded and its checkpoint is held below them.
            "objects_failed_unresolved": result.objects_failed_unresolved,
            "databases_collected": result.databases_collected,
            "blocks_collected": result.blocks_collected,
            "comments_collected": result.comments_collected,
            "users_seen": result.users_seen,
            "archived_observed": result.archived_observed,
            "link_queue_fetched": result.link_queue_fetched,
            "link_queue_unresolved": result.link_queue_unresolved,
            "events": len(result.events),
        },
    )


def _notion_degraded_reason(result: Any) -> str | None:
    """Why the Notion source is degraded, or None if the capture was complete.

    Only an *unresolved* failure degrades the source: the API never answered
    for those objects, so the run genuinely does not know what it missed. A
    permanent skip (a deleted page, a lost share) is a complete observation and
    stays an ordinary success-with-skips.
    """
    if result.status != "degraded":
        return None
    if result.objects_failed_unresolved:
        return (
            f"{result.objects_failed_unresolved} object(s) unresolved after the client "
            "exhausted its retries; the checkpoint is held below them"
        )
    return "the capture did not cover everything it set out to; see the manifest"


class CaptureFailed(RuntimeError):
    """A capture that failed after its archive was opened.

    Carrying the archive lets the runner write a `failed` manifest for the run
    so a failure is as traceable on disk as a success.
    """

    def __init__(self, archive: RawArchive, error: BaseException) -> None:
        super().__init__(str(error))
        self.archive = archive
        self.error = error


DEFAULT_CAPTURES: dict[str, CaptureFn] = {
    "slack": capture_slack,
    "google-calendar": capture_google_calendar,
    "github": capture_github,
    "slurm": capture_slurm,
    "notion": capture_notion,
}


# ------------------------------------------------------------------ runner


def _redact(error: BaseException) -> str:
    """Error text for a manifest and for stdout, with no credential material."""
    text = f"{type(error).__name__}: {error}"
    for marker in ("xoxp-", "xoxb-", "ntn_", "secret_", "ya29."):
        index = text.find(marker)
        while index != -1:
            end = index + len(marker)
            while end < len(text) and (text[end].isalnum() or text[end] in "-_."):
                end += 1
            text = text[:index] + marker + "<redacted>" + text[end:]
            index = text.find(marker, index + len(marker) + len("<redacted>"))
    return text[:500]


def _run_source(
    source: str,
    config: DailyConfig,
    credentials: Credentials,
    captures: dict[str, CaptureFn],
) -> SourceResult:
    outcome = SourceResult(source=source)
    capture_fn = captures.get(source)
    if capture_fn is None:
        outcome.status = "skipped"
        outcome.reason = "no capture is implemented for this source"
        return outcome

    capture_stage = outcome.add(StageResult(stage="capture", status="failed"))
    try:
        captured = capture_fn(config, credentials)
    except CaptureFailed as failure:
        manifest = failure.archive.finish(
            {
                "status": "failed",
                "error_type": type(failure.error).__name__,
                "error": _redact(failure.error),
            }
        )
        capture_stage.error = _redact(failure.error)
        capture_stage.detail = {"manifest": str(manifest)}
        outcome.manifest_path = str(manifest)
        outcome.status = "failed"
        return outcome
    except (Exception, SystemExit) as error:
        # SystemExit is caught deliberately: a collector factory raises it for
        # a missing credential, and one source's missing token must never
        # abort the other sources' runs.
        capture_stage.error = _redact(error)
        outcome.status = "failed"
        return outcome

    capture_stage.status = "ok"
    capture_stage.detail = dict(captured.summary)
    if captured.degraded_reason:
        outcome.reason = captured.degraded_reason
    # Slack and Calendar discover Notion URLs; the queue is the hand-off that
    # lets the Notion capture later in this same run resolve every one of them.
    if source != "notion" and (captured.events or captured.notion_urls):
        from .link_queue import NotionLinkQueue

        # A dry or smoke run reports what it would have queued but writes
        # nothing: the queue is persistent production state, and marking a real
        # pending URL from a bounded run would hide it from the next real one.
        queue = NotionLinkQueue(
            config.archive_root, config.environment, read_only=config.dry_run
        )
        queued = queue.add_events(captured.events)
        queued += queue.add_urls(
            captured.notion_urls,
            source=LEDGER_SOURCE[source],
            run_id=captured.archive.run_id,
        )
        capture_stage.detail["notion_links_queued"] = queued
        capture_stage.detail["notion_links_persisted"] = not queue.read_only
    outcome.run_id = captured.archive.run_id
    outcome.manifest_path = str(captured.manifest_path)
    outcome.checkpoint_advanced = captured.checkpoint_advanced

    ledger_stage = outcome.add(StageResult(stage="ledger", status="failed"))
    ledger_source = LEDGER_SOURCE[source]
    try:
        from .ledger.live import convert_live_run

        converted = convert_live_run(
            archive_root=config.archive_root,
            manifest_path=Path(captured.manifest_path),
            out_root=config.ledger_root,
            source=ledger_source,
        )
        ledger_stage.detail = converted.as_dict()
        ledger_stage.status = "ok" if not converted.schema_errors else "failed"
        if converted.schema_errors:
            ledger_stage.error = f"{converted.schema_errors} record(s) failed schema validation"
        # The manifest is finished and immutable by now, so the run's ledger
        # counts are recorded in the derived progress snapshot instead. The
        # 수집 현황 dashboard falls back to counting the JSONL when a run
        # predates this, or when no snapshot was configured. A dashboard file
        # is never allowed to change the outcome of a stage.
        progress = getattr(captured.archive, "progress", None)
        if progress is not None:
            progress.note_ledger(ledger_stage.detail)
    except Exception as error:
        ledger_stage.error = _redact(error)
        outcome.status = "degraded"
        return outcome
    if ledger_stage.status != "ok":
        outcome.status = "degraded"
        return outcome

    load_stage = outcome.add(StageResult(stage="load", status="skipped"))
    if not config.load_database:
        load_stage.detail = {"reason": "database loading disabled"}
        outcome.status = "degraded" if captured.degraded_reason else "ok"
        return outcome
    if not config.database_url:
        load_stage.detail = {"reason": "no database url configured"}
        outcome.status = "degraded" if captured.degraded_reason else "ok"
        return outcome
    try:
        from .ledger.load import load_source

        # The whole ledger root for this source is offered, not just this
        # run's file. Unchanged files are skipped by sha256, so a file whose
        # load failed yesterday is retried today instead of being stranded.
        loaded = load_source(
            database_url=config.database_url,
            ledger_root=config.ledger_root,
            source=ledger_source,
            dry_run=config.dry_run or config.smoke,
            skip_unchanged=True,
        )
        load_stage.detail = loaded.as_dict()
        load_stage.status = "ok" if not loaded.errors else "failed"
        if loaded.errors:
            load_stage.error = "; ".join(loaded.errors[:3])
    except Exception as error:
        load_stage.status = "failed"
        load_stage.error = _redact(error)

    if captured.degraded_reason or load_stage.status not in {"ok", "skipped"}:
        outcome.status = "degraded"
    else:
        outcome.status = "ok"
    return outcome


def run_daily(
    config: DailyConfig,
    *,
    credentials: Credentials | None = None,
    captures: dict[str, CaptureFn] | None = None,
    on_source: Callable[[SourceResult], None] | None = None,
) -> dict[str, Any]:
    resolved_credentials = credentials or load_credentials(config.config_root)
    resolved_captures = captures if captures is not None else DEFAULT_CAPTURES
    started_at = datetime.now(timezone.utc)
    lock_path = config.resolved_lock_path()
    ordered = tuple(source for source in SOURCE_ORDER if source in config.sources)
    unknown = sorted(set(config.sources) - set(SOURCE_ORDER))

    summary: dict[str, Any] = {
        "schema_version": 1,
        "started_at": started_at.isoformat(),
        "environment": config.environment,
        "archive_root": str(config.archive_root),
        "ledger_root": str(config.ledger_root),
        "capture_density": config.capture_density,
        "dry_run": config.dry_run,
        "smoke": config.smoke,
        "since": config.since,
        "until": config.until,
        "mode": "date_slice" if config.until else "incremental",
        "sources_requested": list(ordered),
        "unknown_sources": unknown,
        "credentials_available": resolved_credentials.availability(),
        "lock_path": str(lock_path),
    }

    handle = _acquire_lock(lock_path)
    if handle is None:
        summary.update(
            {
                "status": "locked",
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "sources": [],
                "exit_code": EXIT_LOCKED,
            }
        )
        return summary

    results: list[SourceResult] = []
    try:
        for source in ordered:
            result = _run_source(source, config, resolved_credentials, resolved_captures)
            results.append(result)
            if on_source is not None:
                on_source(result)
    finally:
        _release_lock(handle)

    statuses = {result.status for result in results}
    if "failed" in statuses:
        exit_code = EXIT_CAPTURE_FAILED
        status = "failed"
    elif "degraded" in statuses:
        exit_code = EXIT_DOWNSTREAM_FAILED
        status = "degraded"
    else:
        exit_code = EXIT_OK
        status = "ok"
    summary.update(
        {
            "status": status,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "sources": [result.as_dict() for result in results],
            "exit_code": exit_code,
        }
    )
    return summary


def _acquire_lock(path: Path):
    """Non-blocking exclusive lock, so two cron runs never overlap."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    handle.seek(0)
    handle.truncate()
    handle.write(json.dumps({"pid": os.getpid(), "acquired_at": datetime.now(timezone.utc).isoformat()}) + "\n")
    handle.flush()
    return handle


def _release_lock(handle) -> None:
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def config_as_dict(config: DailyConfig) -> dict[str, Any]:
    value = asdict(config)
    for key in ("archive_root", "ledger_root", "config_root", "lock_path"):
        if value.get(key) is not None:
            value[key] = str(value[key])
    value.pop("database_url", None)
    return value
