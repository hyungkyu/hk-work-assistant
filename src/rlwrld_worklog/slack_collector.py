from __future__ import annotations

import copy
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .archive import RawArchive
from .models import TimelineEvent
from .normalizers import normalize_slack
from .slack_client import SlackApiError, SlackClient


FILE_LINK_FIELDS = ("id", "name", "title", "mimetype", "filetype", "size", "permalink", "url_private")
SKIPPABLE_CONVERSATION_ERRORS = {"channel_not_found", "not_in_channel", "no_permission", "is_archived"}


def parse_since(value: str, *, now: datetime | None = None) -> datetime:
    current = now or datetime.now(timezone.utc)
    lowered = value.strip().casefold()
    if lowered.endswith(("h", "d")) and lowered[:-1].isdigit():
        amount = int(lowered[:-1])
        delta = timedelta(hours=amount) if lowered.endswith("h") else timedelta(days=amount)
        return current - delta
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _archive_safe_slack_body(body: dict[str, Any]) -> dict[str, Any]:
    """Keep Slack message JSON while reducing attached files to metadata and links."""
    result = copy.deepcopy(body)

    def scrub(value: Any) -> None:
        if isinstance(value, dict):
            files = value.get("files")
            if isinstance(files, list):
                value["files"] = [
                    {key: item.get(key) for key in FILE_LINK_FIELDS if item.get(key) is not None}
                    for item in files
                    if isinstance(item, dict)
                ]
            for nested in value.values():
                scrub(nested)
        elif isinstance(value, list):
            for nested in value:
                scrub(nested)

    scrub(result)
    return result


def _is_expected_search_match(
    search_name: str,
    match: dict[str, Any],
    *,
    self_user_id: str,
) -> bool:
    text = str(match.get("text", ""))
    if search_name == "direct-mentions":
        return f"<@{self_user_id}>" in text
    if search_name == "messages-from-self":
        return match.get("user") == self_user_id
    if search_name == "direct-messages-to-self":
        return match.get("type") == "im"
    if search_name.startswith("broadcast-"):
        return f"<!{search_name.removeprefix('broadcast-')}>" in text
    if search_name.startswith("usergroup-"):
        return f"<!subteam^{search_name.removeprefix('usergroup-')}" in text
    return False


@dataclass(frozen=True)
class CollectionResult:
    run_id: str
    team_id: str
    self_user_id: str
    channels_seen: int
    channels_collected: int
    channels_skipped: int
    events: tuple[TimelineEvent, ...]
    truncated: bool
    manifest_path: Path


class SlackCollector:
    def __init__(self, client: SlackClient, archive: RawArchive) -> None:
        self.client = client
        self.archive = archive

    def _pages(self, method: str, result_key: str, kind: str, **params: Any) -> Iterable[dict[str, Any]]:
        for page in self.client.iter_pages(method, result_key=result_key, **params):
            self.archive.write_page(kind, _archive_safe_slack_body(page))
            yield page

    def collect(
        self,
        *,
        since: datetime,
        expected_team_id: str | None,
        channel_ids: set[str] | None = None,
        max_channels: int | None = None,
        max_messages: int | None = None,
    ) -> CollectionResult:
        auth = self.client.call("auth.test")
        self.archive.write_page("auth-test", _archive_safe_slack_body(auth))
        team_id = str(auth["team_id"])
        self_user_id = str(auth["user_id"])
        if expected_team_id and team_id != expected_team_id:
            raise SlackApiError(f"Authenticated Slack team {team_id} does not match expected team")

        for _ in self._pages("users.list", "members", "users"):
            pass
        usergroups = self.client.call("usergroups.list", include_users=True, include_disabled=True)
        self.archive.write_page("usergroups", _archive_safe_slack_body(usergroups))
        self_group_ids = [
            str(group["id"])
            for group in usergroups.get("usergroups", [])
            if self_user_id in group.get("users", []) and group.get("id")
        ]

        channels: list[dict[str, Any]] = []
        for page in self._pages(
            "conversations.list",
            "channels",
            "conversations",
            types="public_channel,private_channel,mpim,im",
            exclude_archived=False,
        ):
            channels.extend(page["channels"])
        channels_seen = len(channels)
        if channel_ids:
            channels = [channel for channel in channels if channel.get("id") in channel_ids]
            missing = channel_ids - {str(channel.get("id")) for channel in channels}
            if missing:
                raise SlackApiError(f"Requested Slack conversations were not visible: {', '.join(sorted(missing))}")
        if max_channels is not None:
            channels = channels[:max_channels]

        events_by_key: dict[tuple[str, str], TimelineEvent] = {}
        high_watermarks: dict[str, str] = {}
        truncated = bool(channel_ids) or (max_channels is not None and channels_seen > len(channels))
        workspace_url = str(auth.get("url", "")).rstrip("/")

        def at_limit() -> bool:
            return max_messages is not None and len(events_by_key) >= max_messages

        def add_message(channel_id: str, message: dict[str, Any]) -> None:
            if at_limit():
                return
            timestamp = message.get("ts") or message.get("deleted_ts")
            if not timestamp:
                return
            enriched = dict(message)
            enriched["channel"] = channel_id
            if workspace_url:
                enriched["permalink"] = f"{workspace_url}/archives/{channel_id}/p{str(timestamp).replace('.', '')}"
            event = normalize_slack(enriched, self_user_id=self_user_id)
            events_by_key[(channel_id, str(timestamp))] = event
            previous = high_watermarks.get(channel_id)
            if previous is None or float(timestamp) > float(previous):
                high_watermarks[channel_id] = str(timestamp)

        skipped_channels: list[dict[str, str]] = []

        def collect_channel(channel_id: str) -> None:
            nonlocal truncated
            for page in self._pages(
                "conversations.history",
                "messages",
                f"history-{channel_id}",
                channel=channel_id,
                oldest=f"{since.timestamp():.6f}",
                inclusive=True,
            ):
                for message in page["messages"]:
                    add_message(channel_id, message)
                    if at_limit():
                        truncated = True
                        break
                    if message.get("thread_ts") or not message.get("reply_count"):
                        continue
                    for reply_page in self._pages(
                        "conversations.replies",
                        "messages",
                        f"replies-{channel_id}-{message['ts']}",
                        channel=channel_id,
                        ts=message["ts"],
                        oldest=f"{since.timestamp():.6f}",
                        inclusive=True,
                    ):
                        for reply in reply_page["messages"]:
                            add_message(channel_id, reply)
                            if at_limit():
                                truncated = True
                                break
                        if at_limit():
                            break
                    if at_limit():
                        break
                if at_limit():
                    break

        for channel in channels:
            channel_id = str(channel["id"])
            if at_limit():
                truncated = True
                break
            try:
                collect_channel(channel_id)
            except SlackApiError as error:
                if error.code not in SKIPPABLE_CONVERSATION_ERRORS:
                    raise
                skipped_channels.append({"channel_id": channel_id, "error": str(error.code)})

        # A reply posted today can belong to a thread whose parent is older than
        # `since`, so conversations.history alone cannot discover it. Search the
        # user-critical paths independently and deduplicate by channel + ts. The
        # live smoke test also verifies how this workspace indexes encoded mentions.
        if not channel_ids and max_channels is None and not at_limit():
            after_date = since.date().isoformat()
            searches = [
                ("direct-mentions", f'"<@{self_user_id}>" after:{after_date}'),
                ("direct-messages-to-self", f"to:me after:{after_date}"),
                ("messages-from-self", f"from:me after:{after_date}"),
                ("broadcast-channel", f'"<!channel>" after:{after_date}'),
                ("broadcast-here", f'"<!here>" after:{after_date}'),
                ("broadcast-everyone", f'"<!everyone>" after:{after_date}'),
            ]
            searches.extend(
                (f"usergroup-{group_id}", f'"<!subteam^{group_id}>" after:{after_date}')
                for group_id in self_group_ids
            )
            for search_name, query in searches:
                for page in self.client.iter_search_messages(query):
                    self.archive.write_page(search_name, _archive_safe_slack_body(page))
                    for match in page.get("messages", {}).get("matches", []):
                        if not _is_expected_search_match(search_name, match, self_user_id=self_user_id):
                            continue
                        channel = match.get("channel") or {}
                        channel_id = channel.get("id") if isinstance(channel, dict) else channel
                        timestamp = match.get("ts")
                        if not channel_id or not timestamp or float(timestamp) < since.timestamp():
                            continue
                        add_message(str(channel_id), match)
                        if at_limit():
                            truncated = True
                            break
                    if at_limit():
                        break

        events = tuple(sorted(events_by_key.values(), key=lambda item: (item.occurred_at, item.event_id)))
        channels_collected = len(channels) - len(skipped_channels)
        manifest_path = self.archive.finish(
            {
                "status": "success_with_skips" if skipped_channels else "success",
                "team_id": team_id,
                "self_user_id": self_user_id,
                "since": since.isoformat(),
                "channels_seen": channels_seen,
                "channels_attempted": len(channels),
                "channels_collected": channels_collected,
                "events": len(events),
                "truncated": truncated,
                "skipped_channels": skipped_channels,
                "high_watermarks": high_watermarks,
            }
        )
        if not truncated:
            self.archive.write_checkpoint(
                {
                    "schema_version": 1,
                    "source": "slack",
                    "run_id": self.archive.run_id,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    "high_watermarks": high_watermarks,
                    "skipped_channels": skipped_channels,
                }
            )
        return CollectionResult(
            run_id=self.archive.run_id,
            team_id=team_id,
            self_user_id=self_user_id,
            channels_seen=channels_seen,
            channels_collected=channels_collected,
            channels_skipped=len(skipped_channels),
            events=events,
            truncated=truncated,
            manifest_path=manifest_path,
        )


def make_slack_collector(*, archive_root: Path, environment: str) -> tuple[SlackClient, RawArchive, SlackCollector]:
    token = os.environ.get("SLACK_USER_TOKEN")
    if not token:
        raise SystemExit("SLACK_USER_TOKEN is required; run scripts/install-slack-secret.sh")
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:10]
    client = SlackClient(token)
    archive = RawArchive(archive_root, "slack", run_id, environment)
    return client, archive, SlackCollector(client, archive)
