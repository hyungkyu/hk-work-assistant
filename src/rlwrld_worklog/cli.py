from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Sequence

from .daily import DEFAULT_SINCE
from .models import Source, TimelineEvent
from .normalizers import normalize_records
from .work_cli import add_work_parser, run_work


FIXTURE_FILES = {
    Source.SLACK: "slack.json",
    Source.GOOGLE_CALENDAR: "google_calendar.json",
    Source.GITHUB: "github.json",
}


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
    collect.add_argument("--since", required=True, help="UTC/offset ISO time, or duration such as 24h or 730d")
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
    ledger_load.add_argument("source", choices=["slack", "notion", "google-calendar"])
    ledger_load.add_argument("--ledger-root", type=Path, required=True)
    ledger_load.add_argument("--database-url", default=None)
    ledger_load.add_argument("--apply", action="store_true", help="Commit; default is dry-run")
    ledger_load.add_argument("--batch-size", type=_positive_int, default=500)
    ledger_load.add_argument("--reload-unchanged", action="store_true")

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
    live_convert.add_argument("source", choices=["slack", "notion", "google-calendar"])
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
        choices=["slack", "google-calendar", "notion"],
        help="Repeatable. Default: all three, always run in link-queue order.",
    )
    daily.add_argument("--environment", choices=["test", "production"], default="production")
    daily.add_argument("--since", default=DEFAULT_SINCE, help="Floor for sources with no checkpoint")
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

    add_work_parser(subparsers)
    return parser


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
    _, archive, collector = make_slack_collector(archive_root=archive_root, environment=args.environment)
    try:
        result = collector.collect(
            since=parse_since(args.since),
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
    archive_root = _archive_root(args)
    archive, collector = make_notion_collector(
        token=token,
        archive_root=archive_root,
        environment=args.environment,
    )
    try:
        result = collector.collect(since=parse_since(args.since))
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


def daily_collect(args: argparse.Namespace) -> int:
    from .daily import DailyConfig, SOURCE_ORDER, config_as_dict, run_daily

    archive_root = _archive_root(args)
    config = DailyConfig(
        archive_root=archive_root,
        ledger_root=_ledger_root(args, archive_root),
        environment=args.environment,
        since=args.since,
        sources=tuple(args.source) if args.source else SOURCE_ORDER,
        database_url=args.database_url or os.environ.get("DATABASE_URL"),
        load_database=not args.no_database,
        dry_run=args.dry_run,
        smoke=args.smoke,
        config_root=args.config_root,
        lock_path=args.lock_path,
    )
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
    if args.command == "ledger-verify":
        return ledger_verify(args)
    if args.command == "ledger-schema":
        return ledger_schema(args)
    if args.command == "ledger-live-convert":
        return ledger_live_convert(args)
    if args.command == "daily-collect":
        return daily_collect(args)
    if args.command == "work":
        return run_work(args)
    raise AssertionError(f"Unhandled command: {args.command}")
