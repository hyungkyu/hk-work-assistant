from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Sequence

from .models import Source, TimelineEvent
from .normalizers import normalize_records


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
    collect.add_argument("source", choices=["slack"])
    collect.add_argument("--since", required=True, help="UTC/offset ISO time, or duration such as 24h or 730d")
    collect.add_argument("--environment", choices=["test", "production"], default="test")
    collect.add_argument("--archive-root", type=Path, default=None)
    collect.add_argument("--database-url", default=None)
    collect.add_argument("--no-database", action="store_true")
    collect.add_argument("--expected-team-id", default=None)
    collect.add_argument("--channel-id", action="append", default=[])
    collect.add_argument("--max-channels", type=_positive_int, default=None)
    collect.add_argument("--max-messages", type=_positive_int, default=None)
    google_auth = subparsers.add_parser("google-auth", help="Authorize read-only Google access locally")
    google_auth.add_argument("--client-secrets", type=Path, default=Path("secrets/google-client.json"))
    google_auth.add_argument("--token", type=Path, default=Path("secrets/google-token.json"))
    legacy = subparsers.add_parser("legacy-drive-download", help="Mirror legacy Slack/GCal JSON from Drive")
    legacy.add_argument("--folder-id", default="1oLHCQpKfYTJPC_TKzSJU8_Fi1Xv_cCpa")
    legacy.add_argument("--source", action="append", choices=["slack", "gcal"], default=[])
    legacy.add_argument("--archive-root", type=Path, default=None)
    legacy.add_argument("--token", type=Path, default=Path("secrets/google-token.json"))
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
        f"events={len(result.events)} imported={imported} truncated={str(result.truncated).lower()} "
        f"manifest={result.manifest_path}"
    )
    return 0


def run_google_auth(args: argparse.Namespace) -> int:
    from .google_auth import DRIVE_READONLY_SCOPE, authorize_installed_app, token_summary

    authorize_installed_app(args.client_secrets, args.token, [DRIVE_READONLY_SCOPE])
    print("google_auth=ok " + json.dumps(token_summary(args.token), ensure_ascii=False, sort_keys=True))
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
    if args.command == "google-auth":
        return run_google_auth(args)
    if args.command == "legacy-drive-download":
        return download_legacy_drive(args)
    raise AssertionError(f"Unhandled command: {args.command}")
