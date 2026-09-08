from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import IO, Sequence

from .collection_audit import DEFAULT_DAYS as COLLECTION_AUDIT_DEFAULT_DAYS
from .daily import DEFAULT_SINCE, MAX_WINDOW_HOURS, SOURCE_ORDER
from .models import Source, TimelineEvent
from .normalizers import normalize_records
from .search import MATCHERS as SEARCH_MATCHERS
from .slurm_collector import CLOUDS as SLURM_CLOUDS
from .work_cli import add_work_parser, run_work


FIXTURE_FILES = {
    Source.SLACK: "slack.json",
    Source.GOOGLE_CALENDAR: "google_calendar.json",
    Source.GITHUB: "github.json",
}

# One help string for both commands, because they mean the same thing and a
# divergence between them would be read as a difference in behaviour. The last
# sentence is not decoration: `github-collect --until` predates this flag and
# names the last day *inclusive*, which is the opposite convention.
UNTIL_HELP = (
    "Exclusive upper bound: a KST date (YYYY-MM-DD, meaning midnight KST that day) or an "
    "ISO 8601 instant. Captures one historical window instead of resuming, and never "
    "advances a checkpoint. Not accepted for google-calendar. Note this bound is exclusive, "
    "unlike github-collect/slurm-collect --until, which name the last day inclusive"
)

# Both bounds read a bare date the same way. They did not always: `--since`
# read one as UTC and `--until` as KST, so a window written with two bare dates
# was fifteen hours long and filed as a whole day. Saying so in both help
# strings is what makes the shared convention checkable from the command line.
SINCE_HELP = (
    "Inclusive lower bound: a KST date (YYYY-MM-DD, meaning midnight KST that day), an "
    "ISO 8601 instant (no offset means UTC), or a duration such as 24h or 730d"
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="worklog")
    subparsers = parser.add_subparsers(dest="command", required=True)
    fixtures = subparsers.add_parser("fixtures", help="Normalize local fixture files")
    fixtures.add_argument("--input", type=Path, required=True)
    fixtures.add_argument("--output", type=Path, required=True)
    fixtures.add_argument("--self-slack-user-id", default="U0SELF")
    import_jsonl = subparsers.add_parser("import-jsonl", help="Upsert normalized JSONL into PostgreSQL")
    import_jsonl.add_argument("--input", default="-", help="JSONL path, or - for standard input")
    import_jsonl.add_argument("--database-url", default=None)
    doctor = subparsers.add_parser("doctor", help="Validate a source token and read permissions")
    doctor.add_argument("source", choices=["slack"])
    doctor.add_argument("--expected-team-id", default=None)
    collect = subparsers.add_parser("collect", help="Collect source data into the local archive")
    collect.add_argument("source", choices=["slack", "google-calendar", "notion"])
    collect.add_argument("--since", required=True, help=SINCE_HELP)
    collect.add_argument("--until", default=None, help=UNTIL_HELP)
    collect.add_argument("--environment", choices=["test", "production"], default="test")
    collect.add_argument("--archive-root", type=Path, default=None)
    collect.add_argument("--database-url", default=None)
    collect.add_argument("--no-database", action="store_true")
    collect.add_argument("--expected-team-id", default=None)
    collect.add_argument("--channel-id", action="append", default=[])
    collect.add_argument("--max-channels", type=_positive_int, default=None)
    collect.add_argument("--max-messages", type=_positive_int, default=None)
    collect.add_argument("--google-token", type=Path, default=None)
    collect.add_argument("--calendar-id", action="append", default=[])
    collect.add_argument("--calendar-id-file", type=Path, default=None)
    # GitHub keeps its own subcommand rather than joining `collect`: its window
    # is a pair of KST calendar dates, while `collect --since` is an instant or
    # a duration. Folding them together would make one of the two lie.
    github = subparsers.add_parser(
        "github-collect", help="Capture GitHub activity for a window of KST days"
    )
    github.add_argument("--since", required=True, help="First KST day, YYYY-MM-DD")
    github.add_argument("--until", default=None, help="Last KST day, inclusive. Defaults to --since")
    github.add_argument(
        "--organization",
        default=None,
        help="Defaults to settings.json github_organization, then $GITHUB_ORG, then rlwrld",
    )
    github.add_argument("--mirror-root", type=Path, default=None, help="Bare mirror directory")
    github.add_argument("--environment", choices=["test", "production"], default="test")
    github.add_argument("--archive-root", type=Path, default=None)
    github.add_argument("--config-root", type=Path, default=None)
    github.add_argument("--repo", action="append", default=[])
    github.add_argument("--max-repositories", type=_positive_int, default=None)
    github.add_argument(
        "--kinds", default=None, help="Comma-separated REST kinds. Defaults to all six"
    )
    github.add_argument("--no-commits", action="store_true")
    github.add_argument(
        "--repos-from-mirrors",
        action="store_true",
        help="List repositories from the mirror directory, not the API. Commit-only runs then need no network",
    )
    github.add_argument("--no-diffstat", action="store_true")
    github.add_argument(
        "--verify-mirror-refs",
        action="store_true",
        help="Compare each mirror's branch tips with the remote. One network round trip per repository, about four minutes across the org, and the only way to know a mirror is current",
    )
    github.add_argument(
        "--backfill",
        action="store_true",
        help="Ignore the checkpoint and do not advance it. Required for historical windows",
    )
    github.add_argument("--dry-run", action="store_true")
    github.add_argument(
        "--lock-path",
        type=Path,
        default=None,
        help="Defaults to <archive-root>/locks/github-collect-<environment>.lock. Never the daily lock",
    )
    slurm = subparsers.add_parser(
        "slurm-collect", help="Capture Slurm accounting for a window of KST days"
    )
    slurm.add_argument("--since", required=True, help="First KST day, YYYY-MM-DD")
    slurm.add_argument("--until", default=None, help="Last KST day, inclusive. Defaults to --since")
    slurm.add_argument(
        "--cloud",
        action="append",
        default=[],
        choices=list(SLURM_CLOUDS),
        help="Repeatable. Defaults to every cloud",
    )
    slurm.add_argument("--base-url", default=None, help="Defaults to $SLURM_DUMP_BASE_URL, then http://infra-node:8888")
    slurm.add_argument("--environment", choices=["test", "production"], default="test")
    slurm.add_argument("--archive-root", type=Path, default=None)
    slurm.add_argument("--config-root", type=Path, default=None)
    slurm.add_argument("--staging-root", type=Path, default=None)
    slurm.add_argument("--no-steps", action="store_true", help="Exclude .batch/.extern rows. Not recommended")
    slurm.add_argument(
        "--backfill",
        action="store_true",
        help="Ignore the checkpoint and do not advance it. Required for historical windows",
    )
    slurm.add_argument("--dry-run", action="store_true")
    slurm.add_argument(
        "--lock-path",
        type=Path,
        default=None,
        help="Defaults to <archive-root>/locks/slurm-collect-<environment>.lock. Never the daily lock",
    )
    google_auth = subparsers.add_parser("google-auth", help="Authorize read-only Google access locally")
    google_auth.add_argument("--client-secrets", type=Path, default=Path("secrets/google-client.json"))
    google_auth.add_argument("--token", type=Path, default=Path("secrets/google-token.json"))
    legacy = subparsers.add_parser("legacy-drive-download", help="Mirror legacy Slack/GCal JSON from Drive")
    legacy.add_argument("--folder-id", default="1oLHCQpKfYTJPC_TKzSJU8_Fi1Xv_cCpa")
    legacy.add_argument("--source", action="append", choices=["slack", "gcal"], default=[])
    legacy.add_argument("--archive-root", type=Path, default=None)
    legacy.add_argument("--token", type=Path, default=Path("secrets/google-token.json"))
    legacy_import = subparsers.add_parser("legacy-import", help="Import local legacy daily_raw into service origin")
    legacy_import.add_argument("source", choices=["slack", "google-calendar", "notion"])
    legacy_import.add_argument("--daily-raw-root", type=Path, required=True)
    legacy_import.add_argument("--database-url", default=None)
    legacy_import.add_argument("--batch-size", type=_positive_int, default=500)

    convert = subparsers.add_parser(
        "ledger-convert", help="Convert legacy files into standard v1 ledger JSONL"
    )
    convert.add_argument("source", choices=["slack", "notion", "google-calendar"])
    convert.add_argument("--legacy-root", type=Path, required=True)
    convert.add_argument("--out-root", type=Path, required=True)
    convert.add_argument("--dry-run", action="store_true", help="Count only; write nothing")
    convert.add_argument("--no-validate", action="store_true", help="Skip JSON Schema validation")
    convert.add_argument(
        "--root",
        action="append",
        default=[],
        choices=["shared", "personal", "hk_private"],
        help="Visibility roots to read (default: shared and personal)",
    )
    convert.add_argument(
        "--salvage-comments",
        action="store_true",
        help="Notion only: lift comment objects out of attribution files",
    )
    convert.add_argument("--limit", type=_positive_int, default=None)

    ledger_migrate = subparsers.add_parser(
        "ledger-migrate", help="Apply sql/migrations (dry-run unless --apply)"
    )
    ledger_migrate.add_argument("--database-url", default=None)
    ledger_migrate.add_argument("--migrations-dir", type=Path, default=Path("sql/migrations"))
    ledger_migrate.add_argument("--apply", action="store_true")

    ledger_load = subparsers.add_parser(
        "ledger-load", help="Load standard ledger JSONL into the service database"
    )
    ledger_load.add_argument(
        "source", choices=["slack", "notion", "google-calendar", "github", "slurm"]
    )
    ledger_load.add_argument("--ledger-root", type=Path, required=True)
    ledger_load.add_argument("--database-url", default=None)
    ledger_load.add_argument("--apply", action="store_true", help="Commit; default is dry-run")
    ledger_load.add_argument("--batch-size", type=_positive_int, default=500)
    ledger_load.add_argument("--reload-unchanged", action="store_true")

    search_cmd = subparsers.add_parser(
        "search", help="Search collected text in the service database"
    )
    search_cmd.add_argument("query", help="What to look for")
    search_cmd.add_argument(
        "--matcher",
        choices=list(SEARCH_MATCHERS),
        default="auto",
        help="auto: whole words first, substrings only if that found nothing. "
        "Postgres has no Korean stemmer, so a word search does not match "
        "across agglutination and a substring search does",
    )
    search_cmd.add_argument(
        "--source",
        action="append",
        default=[],
        choices=["slack", "notion", "google_calendar", "github", "slurm"],
        help="Repeatable. Without any, every source is searched",
    )
    search_cmd.add_argument("--since", default=None, help="Ingested on or after: a KST date or an instant")
    search_cmd.add_argument("--until", default=None, help="Ingested before: a KST date or an instant")
    search_cmd.add_argument("--limit", type=_positive_int, default=25)
    search_cmd.add_argument("--database-url", default=None)
    search_cmd.add_argument(
        "--status",
        action="store_true",
        help="Report what the corpus holds instead of searching, so an empty "
        "result can be told from an empty index",
    )

    ledger_verify = subparsers.add_parser(
        "ledger-verify", help="Verify counts, duplicates, and provenance"
    )
    ledger_verify.add_argument("source", choices=["slack", "notion", "google-calendar"])
    ledger_verify.add_argument("--ledger-root", type=Path, required=True)
    ledger_verify.add_argument("--legacy-root", type=Path, default=None)
    ledger_verify.add_argument("--database-url", default=None)
    ledger_verify.add_argument("--report", type=Path, default=None)
    ledger_verify.add_argument("--provenance-sample", type=int, default=200)

    ledger_schema = subparsers.add_parser(
        "ledger-schema", help="Print the standard v1 JSON Schema"
    )
    ledger_schema.add_argument("--output", type=Path, default=None)

    live_convert = subparsers.add_parser(
        "ledger-live-convert",
        help="Convert one archived live capture run into standard v1 ledger JSONL",
    )
    live_convert.add_argument(
        "source", choices=["slack", "notion", "google-calendar", "github", "slurm"]
    )
    live_convert.add_argument("--manifest", type=Path, required=True)
    live_convert.add_argument("--archive-root", type=Path, default=None)
    live_convert.add_argument("--out-root", type=Path, default=None)
    live_convert.add_argument("--dry-run", action="store_true", help="Count only; write nothing")

    daily = subparsers.add_parser(
        "daily-collect",
        help="Daily incremental capture -> ledger -> database, for cron/systemd/Docker",
    )
    daily.add_argument(
        "--source",
        action="append",
        default=[],
        choices=list(SOURCE_ORDER),
        help="Repeatable. Default: all five, always run in link-queue order.",
    )
    daily.add_argument("--environment", choices=["test", "production"], default="production")
    daily.add_argument(
        "--since", default=DEFAULT_SINCE, help=f"Floor for sources with no checkpoint. {SINCE_HELP}"
    )
    daily.add_argument("--until", default=None, help=UNTIL_HELP)
    daily.add_argument("--archive-root", type=Path, default=None)
    daily.add_argument("--ledger-root", type=Path, default=None)
    daily.add_argument("--config-root", type=Path, default=None, help="APP_CONFIG_ROOT override")
    daily.add_argument("--database-url", default=None)
    daily.add_argument("--no-database", action="store_true")
    daily.add_argument("--lock-path", type=Path, default=None)
    daily.add_argument(
        "--dry-run",
        action="store_true",
        help="Capture and convert, but never advance a checkpoint and never commit to the database",
    )
    daily.add_argument(
        "--smoke",
        action="store_true",
        help="Strictly bounded API work for a connectivity check. Implies --dry-run.",
    )
    daily.add_argument(
        "--allow-wide-window",
        action="store_true",
        help=(
            f"Run an unbounded window wider than {MAX_WINDOW_HOURS} hours as one run anyway. "
            "What it costs: the run banks nothing until it finishes, so a failure part way "
            "through loses every day it had already read, advances no checkpoint, and leaves "
            "the next run starting where the dead one did. scripts/backfill-days.sh does the "
            "same work one KST day per run and keeps every day that finished"
        ),
    )

    # `collection` is a group rather than a top-level `collection-audit`,
    # because reading the collection record is a family of questions and the
    # board already has the same shape in `work audit`.
    collection = subparsers.add_parser(
        "collection", help="Read the record of what has been collected"
    )
    collection_commands = collection.add_subparsers(dest="collection_command", required=True)
    collection_audit_parser = collection_commands.add_parser(
        "audit",
        help="Report every source-day the archive does not show as collected",
    )
    collection_audit_parser.add_argument(
        "--days",
        type=_positive_int,
        default=COLLECTION_AUDIT_DEFAULT_DAYS,
        help="How many finished KST days to check, ending yesterday. Today is never "
        "audited: it is not over, and a day in progress is incomplete for a reason "
        "that is not a defect",
    )
    collection_audit_parser.add_argument(
        "--source",
        action="append",
        default=[],
        choices=list(SOURCE_ORDER),
        help="Repeatable. Default: all five",
    )
    collection_audit_parser.add_argument(
        "--environment",
        default=None,
        help="Defaults to production. `all` widens to every environment, which lets a "
        "test capture answer a question about production data",
    )
    collection_audit_parser.add_argument(
        "--summary",
        action="store_true",
        help="Print only the one-line summary, short enough for next_action",
    )

    add_work_parser(subparsers)
    return parser


def _resolve_until(args: argparse.Namespace) -> datetime | None:
    """The exclusive upper bound for one `collect` run, or None.

    Refuses rather than returns for a source that cannot take one. Silently
    ignoring the flag would collect the live head and file it as the requested
    window, which is indistinguishable afterwards from a window that was
    genuinely empty.
    """
    from .daily import UNTIL_CAPABLE
    from .slack_collector import parse_until

    if not getattr(args, "until", None):
        return None
    if args.source not in UNTIL_CAPABLE:
        raise SystemExit(
            f"--until cannot be honoured for {args.source}: that source resumes from a sync "
            "token rather than a time window, so a bounded slice cannot be expressed"
        )
    try:
        return parse_until(args.until)
    except ValueError as error:
        raise SystemExit(f"--until must be a KST date or an ISO 8601 instant: {error}")


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def run_fixtures(input_dir: Path, output: Path, self_user_id: str) -> int:
    events = []
    for source, filename in FIXTURE_FILES.items():
        records = json.loads((input_dir / filename).read_text(encoding="utf-8"))
        events.extend(normalize_records(source, records, self_user_id=self_user_id))
    events.sort(key=lambda event: (event.occurred_at, event.event_id))
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as stream:
        for event in events:
            stream.write(json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True) + "\n")
    print(f"normalized_events={len(events)} output={output}")
    return 0


def import_jsonl(input_value: str, database_url: str | None) -> int:
    resolved_url = database_url or os.environ.get("DATABASE_URL")
    if not resolved_url:
        raise SystemExit("DATABASE_URL or --database-url is required")
    stream: IO[str]
    close_stream = False
    if input_value == "-":
        stream = sys.stdin
    else:
        stream = Path(input_value).open("r", encoding="utf-8")
        close_stream = True
    try:
        events = [TimelineEvent.from_dict(json.loads(line)) for line in stream if line.strip()]
    finally:
        if close_stream:
            stream.close()
    from .storage import write_events

    count = write_events(resolved_url, events)
    print(f"imported_events={count}")
    return 0


def doctor_slack(expected_team_id: str | None) -> int:
    from .slack_client import SlackClient

    token = os.environ.get("SLACK_USER_TOKEN")
    if not token:
        raise SystemExit("SLACK_USER_TOKEN is required; run scripts/install-slack-secret.sh")
    client = SlackClient(token)
    auth = client.call("auth.test")
    expected = expected_team_id or os.environ.get("SLACK_EXPECTED_TEAM_ID")
    if expected and auth.get("team_id") != expected:
        raise SystemExit(f"Slack team mismatch: received {auth.get('team_id')}, expected {expected}")
    users = client.call("users.list", limit=1)
    channels = client.call(
        "conversations.list",
        limit=1,
        types="public_channel,private_channel,mpim,im",
        exclude_archived=False,
    )
    groups = client.call("usergroups.list", include_users=False, include_disabled=True)
    search = next(client.iter_search_messages(f"to:me after:{datetime.now(timezone.utc).date().isoformat()}"))
    print(
        "slack_doctor=ok "
        f"team_id={auth.get('team_id')} self_user_id={auth.get('user_id')} "
        f"users_probe={len(users.get('members', []))} "
        f"conversations_probe={len(channels.get('channels', []))} "
        f"usergroups={len(groups.get('usergroups', []))} "
        f"mention_search_probe={len(search.get('messages', {}).get('matches', []))}"
    )
    return 0


def collect_slack(args: argparse.Namespace) -> int:
    from .slack_collector import make_slack_collector, parse_since

    archive_root = args.archive_root or Path(os.environ.get("RAW_ARCHIVE_ROOT", "/data/rlwrld-worklog"))
    expected = args.expected_team_id or os.environ.get("SLACK_EXPECTED_TEAM_ID")
    # Resolved before the archive is opened, so a refused bound leaves no
    # empty run directory behind.
    until = _resolve_until(args)
    _, archive, collector = make_slack_collector(archive_root=archive_root, environment=args.environment)
    try:
        result = collector.collect(
            since=parse_since(args.since),
            until=until,
            expected_team_id=expected,
            channel_ids=set(args.channel_id) or None,
            max_channels=args.max_channels,
            max_messages=args.max_messages,
        )
    except Exception as error:
        failure_manifest = archive.finish(
            {
                "status": "failed",
                "error_type": type(error).__name__,
                "error": str(error),
            }
        )
        print(f"slack_collection=failed manifest={failure_manifest}", file=sys.stderr)
        raise
    from .link_queue import NotionLinkQueue

    linked = NotionLinkQueue(archive_root, args.environment).add_events(result.events)
    imported = 0
    if not args.no_database:
        database_url = args.database_url or os.environ.get("DATABASE_URL")
        if not database_url:
            raise SystemExit("DATABASE_URL is required unless --no-database is set")
        from .storage import write_events

        imported = write_events(database_url, result.events)
    print(
        f"slack_collection=ok run_id={result.run_id} team_id={result.team_id} "
        f"channels_seen={result.channels_seen} channels_collected={result.channels_collected} "
        f"channels_skipped={result.channels_skipped} "
        f"events={len(result.events)} imported={imported} notion_links_added={linked} "
        f"truncated={str(result.truncated).lower()} "
        f"manifest={result.manifest_path}"
    )
    return 0


def run_google_auth(args: argparse.Namespace) -> int:
    from .google_auth import CALENDAR_READONLY_SCOPE, DRIVE_READONLY_SCOPE, authorize_installed_app, token_summary

    authorize_installed_app(args.client_secrets, args.token, [DRIVE_READONLY_SCOPE, CALENDAR_READONLY_SCOPE])
    print("google_auth=ok " + json.dumps(token_summary(args.token), ensure_ascii=False, sort_keys=True))
    return 0


def collect_github(args: argparse.Namespace) -> int:
    """Capture one GitHub window under its own lock.

    The lock is deliberately its own file. Taking
    `daily-collect-<environment>.lock` would make the 03:10 daily batch exit 3
    and be read the next morning as a failed collection, when in fact a
    backfill was simply still running.
    """
    from .daily import _acquire_lock, _release_lock, load_credentials
    from .github_client import (
        GhCliClient,
        GitMirrorReader,
        MirrorRepositoryLister,
        default_mirror_root,
    )
    from .github_collector import REST_KINDS, Window, make_github_collector

    archive_root = _archive_root(args)
    # The organization is resolved exactly as the daily batch resolves it, so
    # a backfill run by hand cannot target a different org than the nightly
    # run does on the same host.
    organization = args.organization or load_credentials(args.config_root).github_organization
    mirror_root = args.mirror_root or default_mirror_root()
    window = Window.parse(args.since, args.until)
    if args.kinds is None:
        kinds: tuple[str, ...] = REST_KINDS
    elif args.kinds.strip().lower() in {"", "none"}:
        # An explicit empty set means commits only. Falling back to "all six"
        # here would quietly spend an API budget the caller declined.
        kinds = ()
    else:
        kinds = tuple(part.strip() for part in args.kinds.split(",") if part.strip())
    unknown = [kind for kind in kinds if kind not in REST_KINDS]
    if unknown:
        raise SystemExit(f"unknown kinds: {', '.join(unknown)}")
    if args.repos_from_mirrors and kinds:
        raise SystemExit("--repos-from-mirrors covers commit-only runs; pass --kinds none")

    lock_path = args.lock_path or archive_root / "locks" / f"github-collect-{args.environment}.lock"
    handle = _acquire_lock(lock_path)
    if handle is None:
        print(
            json.dumps(
                {"github_collection": "locked", "lock_path": str(lock_path)},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 3

    reader = GitMirrorReader(mirror_root)
    # The REST client is built even for a mirror-selected run. Selecting
    # repositories from the mirror directory is about not depending on the API
    # to find them, not about refusing to look: without the client the run
    # cannot compare the two listings and reports the divergence as unknown,
    # which is exactly the blind spot that lost 14 commits. If the API is
    # unreachable the comparison degrades to unknown on its own.
    rest = GhCliClient(organization, config_root=args.config_root)
    archive, collector = make_github_collector(
        client=MirrorRepositoryLister(reader, rest) if args.repos_from_mirrors else rest,
        mirrors=reader,
        archive_root=archive_root,
        environment=args.environment,
        organization=organization,
        capture_density="dry-run" if args.dry_run else "full",
        dry_run=args.dry_run,
        config_root=args.config_root,
    )
    try:
        result = collector.collect(
            window=window,
            repositories=args.repo or None,
            kinds=kinds,
            include_commits=not args.no_commits,
            include_diffstat=not args.no_diffstat,
            max_repositories=args.max_repositories,
            backfill=args.backfill,
            verify_mirror_refs=args.verify_mirror_refs,
        )
    except Exception as error:
        failure_manifest = archive.finish(
            {"status": "failed", "error_type": type(error).__name__, "error": str(error)}
        )
        print(f"github_collection=failed manifest={failure_manifest}", file=sys.stderr)
        raise
    finally:
        _release_lock(handle)

    print(
        json.dumps(
            {
                "github_collection": "ok",
                "run_id": result.run_id,
                "window": result.window.as_dict(),
                "mode": "backfill" if args.backfill else "incremental",
                "repositories_listed": result.repositories_listed,
                "repositories_collected": result.repositories_collected,
                "repositories_skipped": result.repositories_skipped,
                "commits": result.commits,
                "commit_rows_archived": result.commit_rows,
                "rest_counts": result.rest_counts,
                "checkpoint_advanced": result.checkpoint_advanced,
                "manifest": str(result.manifest_path),
                "lock_path": str(lock_path),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


def collect_slurm(args: argparse.Namespace) -> int:
    """Capture one Slurm window under its own lock.

    The dump is fetched once per cloud and projected onto KST days locally,
    because the endpoint offers neither a time window nor pagination.
    """
    from .daily import _acquire_lock, _release_lock
    from .slurm_client import SlurmDumpFetcher
    from .slurm_collector import CLOUDS, Window, make_slurm_collector

    archive_root = _archive_root(args)
    window = Window.parse(args.since, args.until)
    clouds = tuple(args.cloud) if args.cloud else CLOUDS

    lock_path = args.lock_path or archive_root / "locks" / f"slurm-collect-{args.environment}.lock"
    handle = _acquire_lock(lock_path)
    if handle is None:
        print(
            json.dumps(
                {"slurm_collection": "locked", "lock_path": str(lock_path)},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 3

    fetcher_options = {"base_url": args.base_url} if args.base_url else {}
    archive, collector = make_slurm_collector(
        fetcher=SlurmDumpFetcher(**fetcher_options),
        archive_root=archive_root,
        environment=args.environment,
        staging_root=args.staging_root,
        capture_density="dry-run" if args.dry_run else "full",
        dry_run=args.dry_run,
        config_root=args.config_root,
    )
    try:
        result = collector.collect(
            window=window,
            clouds=clouds,
            backfill=args.backfill,
            include_steps=not args.no_steps,
        )
    except Exception as error:
        failure_manifest = archive.finish(
            {"status": "failed", "error_type": type(error).__name__, "error": str(error)}
        )
        print(f"slurm_collection=failed manifest={failure_manifest}", file=sys.stderr)
        raise
    finally:
        _release_lock(handle)

    print(
        json.dumps(
            {
                "slurm_collection": "ok",
                "run_id": result.run_id,
                "window": result.window.as_dict(),
                "mode": "backfill" if args.backfill else "incremental",
                "clouds_attempted": list(result.clouds_attempted),
                "clouds_collected": list(result.clouds_collected),
                "jobs": result.jobs,
                "parent_rows_archived": result.parent_rows,
                "step_rows": result.step_rows,
                "checkpoint_advanced": result.checkpoint_advanced,
                "manifest": str(result.manifest_path),
                "lock_path": str(lock_path),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


def _archive_root(args: argparse.Namespace) -> Path:
    return args.archive_root or Path(os.environ.get("RAW_ARCHIVE_ROOT", "/data/rlwrld-worklog"))


def _write_collected_events(args: argparse.Namespace, events: Sequence[TimelineEvent]) -> int:
    if args.no_database:
        return 0
    database_url = args.database_url or os.environ.get("DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL is required unless --no-database is set")
    from .storage import write_events

    return write_events(database_url, events)


def collect_google_calendar(args: argparse.Namespace) -> int:
    from .calendar_collector import make_calendar_collector
    from .google_auth import CALENDAR_READONLY_SCOPE, load_credentials
    from .link_queue import NotionLinkQueue
    from .slack_collector import parse_since

    # Calendar cannot take an upper bound, so this call only ever returns None
    # or refuses the run. It is here so that `--until` is never silently
    # dropped: the flag reaching an unbounded capture is the failure.
    _resolve_until(args)
    archive_root = _archive_root(args)
    token_path = args.google_token or Path(
        os.environ.get("GOOGLE_TOKEN_PATH", "secrets/google-token.json")
    )
    credentials = load_credentials(token_path, [CALENDAR_READONLY_SCOPE])
    calendar_ids = set(args.calendar_id)
    if args.calendar_id_file:
        calendar_ids.update(
            line.strip()
            for line in args.calendar_id_file.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
    archive, collector = make_calendar_collector(
        credentials=credentials,
        archive_root=archive_root,
        environment=args.environment,
    )
    try:
        result = collector.collect(since=parse_since(args.since), calendar_ids=calendar_ids)
    except Exception as error:
        failure_manifest = archive.finish(
            {"status": "failed", "error_type": type(error).__name__, "error": str(error)}
        )
        print(f"google_calendar_collection=failed manifest={failure_manifest}", file=sys.stderr)
        raise
    queue = NotionLinkQueue(archive_root, args.environment)
    linked = queue.add_events(result.events)
    linked += queue.add_urls(
        result.notion_urls,
        source="google_calendar",
        run_id=result.run_id,
    )
    imported = _write_collected_events(args, result.events)
    print(
        f"google_calendar_collection=ok run_id={result.run_id} "
        f"calendars_seen={result.calendars_seen} calendars_collected={result.calendars_collected} "
        f"calendars_skipped={result.calendars_skipped} events={len(result.events)} "
        f"imported={imported} notion_links_added={linked} manifest={result.manifest_path}"
    )
    return 0


def collect_notion(args: argparse.Namespace) -> int:
    from .notion_collector import make_notion_collector
    from .slack_collector import parse_since

    token = os.environ.get("NOTION_TOKEN")
    if not token:
        raise SystemExit("NOTION_TOKEN is required")
    until = _resolve_until(args)
    archive_root = _archive_root(args)
    archive, collector = make_notion_collector(
        token=token,
        archive_root=archive_root,
        environment=args.environment,
    )
    try:
        result = collector.collect(since=parse_since(args.since), until=until)
    except Exception as error:
        failure_manifest = archive.finish(
            {"status": "failed", "error_type": type(error).__name__, "error": str(error)}
        )
        print(f"notion_collection=failed manifest={failure_manifest}", file=sys.stderr)
        raise
    imported = _write_collected_events(args, result.events)
    print(
        f"notion_collection=ok run_id={result.run_id} pages_discovered={result.pages_discovered} "
        f"pages_collected={result.pages_collected} pages_skipped={result.pages_skipped} "
        f"events={len(result.events)} imported={imported} manifest={result.manifest_path}"
    )
    return 0


def download_legacy_drive(args: argparse.Namespace) -> int:
    from .google_auth import DRIVE_READONLY_SCOPE, load_credentials
    from .legacy_drive import GoogleDriveFiles, LegacyDriveDownloader

    archive_root = args.archive_root or Path(os.environ.get("RAW_ARCHIVE_ROOT", "/data/rlwrld-worklog"))
    credentials = load_credentials(args.token, [DRIVE_READONLY_SCOPE])
    sources = args.source or ["slack", "gcal"]
    result = LegacyDriveDownloader(GoogleDriveFiles(credentials), archive_root).download(args.folder_id, sources)
    print(
        f"legacy_drive_download=ok source_roots={result.source_roots} downloaded={result.downloaded} "
        f"unchanged={result.unchanged} bytes_downloaded={result.bytes_downloaded} "
        f"manifest={result.manifest_path}"
    )
    return 0


def import_legacy(args: argparse.Namespace) -> int:
    from .legacy_import import LegacyImportStats, iter_legacy_events
    from .storage import write_events

    database_url = args.database_url or os.environ.get("DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL or --database-url is required")
    stats = LegacyImportStats()
    imported = 0
    batch: list[TimelineEvent] = []
    for event in iter_legacy_events(args.daily_raw_root, args.source, stats):
        batch.append(event)
        if len(batch) >= args.batch_size:
            imported += write_events(database_url, batch, origin="legacy")
            batch.clear()
    if batch:
        imported += write_events(database_url, batch, origin="legacy")
    print(
        f"legacy_import=ok source={args.source} imported={imported} "
        f"files_seen={stats.files_seen} files_invalid={stats.files_invalid} "
        f"records_seen={stats.records_seen} records_normalized={stats.records_normalized} "
        f"records_skipped={stats.records_skipped}"
    )
    return 0


SOURCE_ARG_TO_LEDGER = {
    "slack": "slack",
    "notion": "notion",
    "google-calendar": "google_calendar",
    "github": "github",
    "slurm": "slurm",
}


def _print_json(label: str, payload: dict) -> None:
    print(f"{label}={json.dumps(payload, ensure_ascii=False, sort_keys=True)}")


def ledger_convert(args: argparse.Namespace) -> int:
    from .ledger.convert import convert_source

    roots = tuple(args.root) if args.root else ("shared", "personal")
    try:
        result = convert_source(
            legacy_root=args.legacy_root,
            out_root=args.out_root,
            source=SOURCE_ARG_TO_LEDGER[args.source],
            dry_run=args.dry_run,
            validate=not args.no_validate,
            roots=roots,
            salvage_comments=args.salvage_comments,
            limit=args.limit,
        )
    except NotImplementedError as error:
        raise SystemExit(str(error))
    _print_json("ledger_convert", result.as_dict())
    return 1 if result.schema_errors else 0


def ledger_migrate(args: argparse.Namespace) -> int:
    from .ledger.load import apply_migrations

    database_url = args.database_url or os.environ.get("DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL or --database-url is required")
    result = apply_migrations(
        database_url=database_url,
        migrations_dir=args.migrations_dir,
        dry_run=not args.apply,
    )
    _print_json("ledger_migrate", result)
    return 1 if result["checksum_mismatch"] else 0


def search_command(args: argparse.Namespace) -> int:
    from .search import search_status, search_text
    from .slack_collector import parse_since, parse_until

    database_url = args.database_url or os.environ.get("DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL or --database-url is required")
    if args.status:
        _print_json("search_status", search_status(database_url))
        return 0
    try:
        result = search_text(
            database_url,
            args.query,
            matcher=args.matcher,
            sources=args.source,
            since=parse_since(args.since) if args.since else None,
            until=parse_until(args.until) if args.until else None,
            limit=args.limit,
        )
    except ValueError as error:
        raise SystemExit(str(error)) from error
    _print_json("search", result.as_dict())
    # No hits is not a failure. It is an answer, and the exit code says so:
    # a caller that treats "nothing matched" as an error cannot tell it from
    # "the database was unreachable", which is the distinction that matters.
    return 0


def ledger_load(args: argparse.Namespace) -> int:
    from .ledger.load import load_source

    database_url = args.database_url or os.environ.get("DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL or --database-url is required")
    result = load_source(
        database_url=database_url,
        ledger_root=args.ledger_root,
        source=SOURCE_ARG_TO_LEDGER[args.source],
        dry_run=not args.apply,
        batch_size=args.batch_size,
        skip_unchanged=not args.reload_unchanged,
    )
    _print_json("ledger_load", result.as_dict())
    return 1 if result.errors else 0


def ledger_verify(args: argparse.Namespace) -> int:
    from .ledger.verify import verify_ledger, write_report

    report = verify_ledger(
        ledger_root=args.ledger_root,
        source=SOURCE_ARG_TO_LEDGER[args.source],
        legacy_root=args.legacy_root,
        database_url=args.database_url or os.environ.get("DATABASE_URL"),
        provenance_sample=args.provenance_sample,
    )
    if args.report:
        write_report(report, args.report)
    _print_json("ledger_verify", report.as_dict())
    return 0 if report.ok else 1


def ledger_schema(args: argparse.Namespace) -> int:
    from .ledger.schema import ledger_json_schema

    body = json.dumps(ledger_json_schema(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(body, encoding="utf-8")
        print(f"ledger_schema=written path={args.output}")
    else:
        print(body, end="")
    return 0


def _ledger_root(args: argparse.Namespace, archive_root: Path) -> Path:
    if getattr(args, "ledger_root", None):
        return args.ledger_root
    configured = os.environ.get("LEDGER_ROOT")
    return Path(configured) if configured else archive_root / "staging" / "ledger"


def ledger_live_convert(args: argparse.Namespace) -> int:
    from .ledger.live import convert_live_run

    archive_root = _archive_root(args)
    result = convert_live_run(
        archive_root=archive_root,
        manifest_path=args.manifest,
        out_root=args.out_root or _ledger_root(args, archive_root),
        source=SOURCE_ARG_TO_LEDGER[args.source],
        dry_run=args.dry_run,
    )
    _print_json("ledger_live_convert", result.as_dict())
    return 1 if result.schema_errors else 0


def collection_audit(args: argparse.Namespace) -> int:
    """Report every source-day the archive does not show as collected.

    The window ends yesterday, in KST. Today is deliberately outside it: the
    day is not over, so its cells are incomplete for a reason that is not a
    defect, and an audit that reported them would cry wolf once a day forever.

    Exit code is 0 even with findings, as `work audit` is: this is a report,
    and the batch that runs it reads `ok` from the payload. A non-zero exit
    would make the systemd unit fail on the days the audit is doing its job.
    """
    from . import collection_audit as audit_module
    from . import collection_status
    from .collection_rules import COLLECTOR_TO_SOURCE

    # Production unless asked otherwise, and only the explicit sentinel widens
    # it. Somebody reads this to decide whether real data was collected, and a
    # test capture answering that question is a lie — the same rule the 수집
    # 현황 API applies (`collection_web.DEFAULT_ENVIRONMENT`).
    environment: str | None = args.environment or "production"
    if environment == "all":
        environment = None

    now = datetime.now(timezone.utc)
    end = now.astimezone(audit_module.KST).date() - timedelta(days=1)
    start = end - timedelta(days=args.days - 1)
    grid = collection_status.coverage(
        collection_status.paths_from_environment(),
        start=start,
        end=end,
        # The grid speaks ledger source names; the command line speaks the
        # collector names every other subcommand takes.
        sources=[COLLECTOR_TO_SOURCE[source] for source in args.source] or None,
        environment=environment,
        now=now,
    )
    report = audit_module.audit(grid, now=now)
    if args.summary:
        print(report["summary"])
        return 0
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def daily_collect(args: argparse.Namespace) -> int:
    from .daily import DailyConfig, config_as_dict, run_daily

    archive_root = _archive_root(args)
    try:
        config = DailyConfig(
            archive_root=archive_root,
            ledger_root=_ledger_root(args, archive_root),
            environment=args.environment,
            since=args.since,
            until=args.until,
            sources=tuple(args.source) if args.source else SOURCE_ORDER,
            database_url=args.database_url or os.environ.get("DATABASE_URL"),
            load_database=not args.no_database,
            dry_run=args.dry_run,
            smoke=args.smoke,
            allow_wide_window=args.allow_wide_window,
            config_root=args.config_root,
            lock_path=args.lock_path,
        )
    except ValueError as error:
        # A window that cannot be honoured as asked, or one too wide to be
        # honoured as a single run. Refused before the lock is taken, so the
        # run leaves nothing behind to interpret.
        raise SystemExit(str(error))
    _print_json("daily_collect_config", config_as_dict(config))
    summary = run_daily(
        config,
        on_source=lambda result: _print_json("daily_collect_source", result.as_dict()),
    )
    _print_json("daily_collect", summary)
    return int(summary["exit_code"])


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "fixtures":
        return run_fixtures(args.input, args.output, args.self_slack_user_id)
    if args.command == "import-jsonl":
        return import_jsonl(args.input, args.database_url)
    if args.command == "doctor" and args.source == "slack":
        return doctor_slack(args.expected_team_id)
    if args.command == "collect" and args.source == "slack":
        return collect_slack(args)
    if args.command == "collect" and args.source == "google-calendar":
        return collect_google_calendar(args)
    if args.command == "collect" and args.source == "notion":
        return collect_notion(args)
    if args.command == "slurm-collect":
        return collect_slurm(args)
    if args.command == "github-collect":
        return collect_github(args)
    if args.command == "google-auth":
        return run_google_auth(args)
    if args.command == "legacy-drive-download":
        return download_legacy_drive(args)
    if args.command == "legacy-import":
        return import_legacy(args)
    if args.command == "ledger-convert":
        return ledger_convert(args)
    if args.command == "ledger-migrate":
        return ledger_migrate(args)
    if args.command == "ledger-load":
        return ledger_load(args)
    if args.command == "search":
        return search_command(args)
    if args.command == "ledger-verify":
        return ledger_verify(args)
    if args.command == "ledger-schema":
        return ledger_schema(args)
    if args.command == "ledger-live-convert":
        return ledger_live_convert(args)
    if args.command == "daily-collect":
        return daily_collect(args)
    if args.command == "collection" and args.collection_command == "audit":
        return collection_audit(args)
    if args.command == "work":
        return run_work(args)
    raise AssertionError(f"Unhandled command: {args.command}")
