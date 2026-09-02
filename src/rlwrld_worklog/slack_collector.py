"""Daily incremental Slack capture over the official read-only Web API.

Coverage design, and the honest limits of it:

  * Every conversation the authenticated user can see (public, private, MPIM,
    DM) is listed with `exclude_archived=false`, so archived channels stay
    visible and their metadata is preserved.
  * `conversations.history` is resumed per channel from the checkpoint's own
    high watermark, so a daily run reads only what is new in that channel.
  * Slack does not return thread replies in `conversations.history`. A reply
    posted today to a thread whose parent is older than the window is
    therefore invisible to history alone. Two independent mitigations run:
    a bounded re-poll of watched threads carried in the checkpoint, and the
    critical `search.messages` queries below. Both are recorded in coverage.
  * `search.messages` covers direct mentions, DMs to self, self-authored
    messages, `<!channel>`/`<!here>`/`<!everyone>` broadcasts and every
    usergroup the user belongs to. Matches that the context filter drops, and
    matches older than the requested window, are counted in the manifest so a
    dropped mention can never be silent.
  * Message deletion is not exposed by the Web API. `conversations.history`
    simply stops returning a deleted message; only the Events API emits
    `message_deleted`. Tombstones are preserved when Slack does expose them
    (`subtype: tombstone`), and the gap is declared in coverage rather than
    papered over.

Files are captured as metadata and links only. Binary bodies are never fetched.
"""

from __future__ import annotations

import copy
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .archive import RawArchive
from .models import TimelineEvent
from .normalizers import normalize_slack
from .slack_client import SlackApiError, SlackClient

SLACK_CAPTURE_PROFILE = "live-slack-web-api/v1"
CHECKPOINT_SCHEMA_VERSION = 2
DEFAULT_THREAD_LOOKBACK_DAYS = 30

FILE_LINK_FIELDS = ("id", "name", "title", "mimetype", "filetype", "size", "permalink", "url_private")
SKIPPABLE_CONVERSATION_ERRORS = {
    "channel_not_found",
    "not_in_channel",
    "no_permission",
    "is_archived",
    "missing_scope",
    "restricted_action",
    "user_is_restricted",
}

COVERAGE_NOTES = (
    "slack.message_deletion_not_exposed: the Web API has no deleted-message feed; "
    "conversations.history stops returning a deleted message instead of tombstoning it. "
    "Tombstones are preserved only where Slack exposes them (subtype=tombstone).",
    "slack.thread_replies_need_supplements: conversations.history omits thread replies, "
    "so replies to older threads are covered by the bounded watched-thread re-poll and "
    "by search.messages, not by history alone.",
    "slack.search_index_lag: search.messages is an index and can lag the live channel, "
    "so a same-minute mention may first appear on the following run.",
)


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


def _mention_pattern(user_id: str) -> re.Pattern[str]:
    # Slack encodes a mention as <@U123> or <@U123|display-name>.
    return re.compile(rf"<@{re.escape(user_id)}(\||>)")


def _is_expected_search_match(
    search_name: str,
    match: dict[str, Any],
    *,
    self_user_id: str,
) -> bool:
    text = str(match.get("text", ""))
    if search_name == "direct-mentions":
        return bool(_mention_pattern(self_user_id).search(text))
    if search_name == "messages-from-self":
        return match.get("user") == self_user_id
    if search_name == "direct-messages-to-self":
        return match.get("type") == "im"
    if search_name.startswith("broadcast-"):
        return f"<!{search_name.removeprefix('broadcast-')}>" in text
    if search_name.startswith("usergroup-"):
        return f"<!subteam^{search_name.removeprefix('usergroup-')}" in text
    return False


def _as_float(value: Any) -> float | None:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


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
    checkpoint_advanced: bool = False
    messages_seen: int = 0
    threads_repolled: int = 0
    search_matches_kept: int = 0
    search_matches_context_filtered: int = 0
    search_matches_before_window: int = 0
    counters: dict[str, Any] = field(default_factory=dict)


class SlackCollector:
    def __init__(self, client: SlackClient, archive: RawArchive) -> None:
        self.client = client
        self.archive = archive

    def _pages(
        self,
        method: str,
        result_key: str,
        kind: str,
        **params: Any,
    ) -> Iterable[dict[str, Any]]:
        for page in self.client.iter_pages(method, result_key=result_key, **params):
            items = page.get(result_key)
            self.archive.write_page(
                kind,
                _archive_safe_slack_body(page),
                endpoint=method,
                request=params,
                item_count=len(items) if isinstance(items, list) else None,
            )
            yield page

    def collect(
        self,
        *,
        since: datetime,
        expected_team_id: str | None,
        channel_ids: set[str] | None = None,
        max_channels: int | None = None,
        max_messages: int | None = None,
        advance_checkpoint: bool = True,
        use_search: bool = True,
        thread_lookback_days: int = DEFAULT_THREAD_LOOKBACK_DAYS,
    ) -> CollectionResult:
        archive = self.archive
        checkpoint = archive.read_checkpoint()
        archive.set_checkpoint_in(checkpoint)
        previous_watermarks: dict[str, str] = {
            str(key): str(value)
            for key, value in (checkpoint.get("high_watermarks") or {}).items()
            if value
        }
        previous_threads: dict[str, dict[str, str]] = {
            str(channel): {str(thread): str(latest) for thread, latest in (threads or {}).items()}
            for channel, threads in (checkpoint.get("thread_watch") or {}).items()
        }
        since_ts = since.timestamp()
        bounded = bool(channel_ids) or max_channels is not None or max_messages is not None
        archive.set_requested_window(
            {
                "since": since.isoformat(),
                "mode": "bounded" if bounded else "incremental",
                "resumed_channels": len(previous_watermarks),
                "watched_threads": sum(len(value) for value in previous_threads.values()),
                "thread_lookback_days": thread_lookback_days,
                "use_search": use_search and not bounded,
            }
        )
        for note in COVERAGE_NOTES:
            archive.note_coverage(note)

        auth = self.client.call("auth.test")
        archive.write_page("auth-test", _archive_safe_slack_body(auth), endpoint="auth.test", item_count=1)
        team_id = str(auth["team_id"])
        self_user_id = str(auth["user_id"])
        if expected_team_id and team_id != expected_team_id:
            raise SlackApiError(f"Authenticated Slack team {team_id} does not match expected team")

        users_seen = 0
        for page in self._pages("users.list", "members", "users"):
            users_seen += len(page.get("members") or [])
        usergroups = self.client.call("usergroups.list", include_users=True, include_disabled=True)
        archive.write_page(
            "usergroups",
            _archive_safe_slack_body(usergroups),
            endpoint="usergroups.list",
            item_count=len(usergroups.get("usergroups") or []),
        )
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
            archive.note_truncation("channel_filter", requested=sorted(channel_ids))
        if max_channels is not None and len(channels) > max_channels:
            channels = channels[:max_channels]
            archive.note_truncation("max_channels", limit=max_channels, channels_seen=channels_seen)

        events_by_key: dict[tuple[str, str], TimelineEvent] = {}
        high_watermarks: dict[str, str] = dict(previous_watermarks)
        thread_watch: dict[str, dict[str, str]] = {
            channel: dict(threads) for channel, threads in previous_threads.items()
        }
        workspace_url = str(auth.get("url", "")).rstrip("/")
        messages_seen = 0

        def at_limit() -> bool:
            return max_messages is not None and len(events_by_key) >= max_messages

        def remember_thread(channel_id: str, message: dict[str, Any], timestamp: str) -> None:
            thread_ts = message.get("thread_ts")
            reply_count = message.get("reply_count")
            if thread_ts and str(thread_ts) != timestamp:
                parent = str(thread_ts)
                latest = timestamp
            elif thread_ts or (isinstance(reply_count, int) and reply_count > 0):
                parent = str(thread_ts or timestamp)
                latest = str(message.get("latest_reply") or timestamp)
            else:
                return
            channel_threads = thread_watch.setdefault(channel_id, {})
            current = channel_threads.get(parent)
            if current is None or (_as_float(latest) or 0.0) > (_as_float(current) or 0.0):
                channel_threads[parent] = latest

        def add_message(channel_id: str, message: dict[str, Any]) -> None:
            nonlocal messages_seen
            if at_limit():
                return
            timestamp = message.get("ts") or message.get("deleted_ts")
            if not timestamp:
                return
            messages_seen += 1
            enriched = dict(message)
            enriched["channel"] = channel_id
            enriched["team_id"] = team_id
            if workspace_url:
                enriched["permalink"] = f"{workspace_url}/archives/{channel_id}/p{str(timestamp).replace('.', '')}"
            event = normalize_slack(enriched, self_user_id=self_user_id)
            events_by_key[(channel_id, str(timestamp))] = event
            remember_thread(channel_id, message, str(timestamp))
            previous = high_watermarks.get(channel_id)
            current = _as_float(timestamp)
            if current is not None and (previous is None or current > (_as_float(previous) or 0.0)):
                high_watermarks[channel_id] = str(timestamp)

        skipped_channels: list[dict[str, str]] = []
        threads_repolled = 0

        def collect_replies(channel_id: str, thread_ts: str, oldest: str) -> None:
            nonlocal threads_repolled
            threads_repolled += 1
            for reply_page in self._pages(
                "conversations.replies",
                "messages",
                f"replies-{channel_id}-{thread_ts}",
                channel=channel_id,
                ts=thread_ts,
                oldest=oldest,
                inclusive=True,
            ):
                for reply in reply_page["messages"]:
                    add_message(channel_id, reply)
                    if at_limit():
                        archive.note_truncation("max_messages", limit=max_messages)
                        return

        def collect_channel(channel_id: str) -> None:
            oldest = previous_watermarks.get(channel_id) or f"{since_ts:.6f}"
            for page in self._pages(
                "conversations.history",
                "messages",
                f"history-{channel_id}",
                channel=channel_id,
                oldest=oldest,
                inclusive=True,
            ):
                for message in page["messages"]:
                    add_message(channel_id, message)
                    if at_limit():
                        archive.note_truncation("max_messages", limit=max_messages)
                        return
                    if message.get("thread_ts") or not message.get("reply_count"):
                        continue
                    collect_replies(channel_id, str(message["ts"]), oldest)
                    if at_limit():
                        return

        for channel in channels:
            channel_id = str(channel["id"])
            if at_limit():
                archive.note_truncation("max_messages", limit=max_messages)
                break
            try:
                collect_channel(channel_id)
            except SlackApiError as error:
                if error.code not in SKIPPABLE_CONVERSATION_ERRORS:
                    raise
                detail = {"channel_id": channel_id, "error": str(error.code)}
                skipped_channels.append(detail)
                archive.note_skip("conversation_inaccessible", **detail)

        # Replies to threads whose parent predates the window are unreachable
        # through conversations.history, so watched threads are re-polled from
        # their own last observed reply. The lookback bounds the daily cost.
        if not bounded:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=thread_lookback_days)).timestamp()
            collected_channel_ids = {str(channel["id"]) for channel in channels}
            already_read = set(events_by_key)
            for channel_id in sorted(thread_watch):
                if channel_id not in collected_channel_ids:
                    continue
                for thread_ts in sorted(dict(thread_watch[channel_id])):
                    latest = thread_watch[channel_id][thread_ts]
                    latest_value = _as_float(latest) or 0.0
                    if latest_value < cutoff:
                        continue
                    if (channel_id, thread_ts) in already_read:
                        # The parent came back through history this run, so its
                        # replies were already re-read above.
                        continue
                    try:
                        collect_replies(channel_id, thread_ts, latest)
                    except SlackApiError as error:
                        if error.code not in SKIPPABLE_CONVERSATION_ERRORS:
                            raise
                        archive.note_skip(
                            "thread_inaccessible",
                            channel_id=channel_id,
                            thread_ts=thread_ts,
                            error=str(error.code),
                        )

        search_kept = 0
        search_filtered = 0
        search_before_window = 0
        searches_run: list[str] = []
        if use_search and not bounded and not at_limit():
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
                searches_run.append(search_name)
                try:
                    pages = list(self.client.iter_search_messages(query))
                except SlackApiError as error:
                    archive.note_skip("search_unavailable", search=search_name, error=str(error.code))
                    continue
                for page in pages:
                    matches = page.get("messages", {}).get("matches", [])
                    archive.write_page(
                        search_name,
                        _archive_safe_slack_body(page),
                        endpoint="search.messages",
                        request={"search": search_name},
                        item_count=len(matches) if isinstance(matches, list) else None,
                    )
                    for match in matches:
                        if not _is_expected_search_match(search_name, match, self_user_id=self_user_id):
                            search_filtered += 1
                            continue
                        channel = match.get("channel") or {}
                        channel_id = channel.get("id") if isinstance(channel, dict) else channel
                        timestamp = match.get("ts")
                        if not channel_id or not timestamp:
                            search_filtered += 1
                            continue
                        if (_as_float(timestamp) or 0.0) < since_ts:
                            search_before_window += 1
                            continue
                        search_kept += 1
                        add_message(str(channel_id), match)
                        if at_limit():
                            archive.note_truncation("max_messages", limit=max_messages)
                            break
                    if at_limit():
                        break
                if at_limit():
                    break
        elif bounded:
            archive.note_coverage(
                "slack.search_skipped_for_bounded_capture: the critical mention searches are "
                "workspace-wide and are skipped when the capture is channel- or message-bounded."
            )

        archive.note_rate_limit(getattr(self.client, "rate_limit_hits", 0))
        events = tuple(sorted(events_by_key.values(), key=lambda item: (item.occurred_at, item.event_id)))
        channels_collected = len(channels) - len(skipped_channels)
        truncated = archive.truncated
        thread_watch = _prune_thread_watch(thread_watch, thread_lookback_days)

        counters = {
            "users_seen": users_seen,
            "usergroups_seen": len(usergroups.get("usergroups") or []),
            "self_usergroups": self_group_ids,
            "messages_seen": messages_seen,
            "threads_repolled": threads_repolled,
            "searches_run": searches_run,
            "search_matches_kept": search_kept,
            "search_matches_context_filtered": search_filtered,
            "search_matches_before_window": search_before_window,
            "watched_threads_after_run": sum(len(value) for value in thread_watch.values()),
            "api_call_counts": dict(getattr(self.client, "call_counts", {}) or {}),
        }

        status = "success"
        if skipped_channels or archive.skips:
            status = "success_with_skips"

        checkpoint_advanced = False
        if advance_checkpoint and not truncated and not self.archive.dry_run:
            self.archive.write_checkpoint(
                {
                    "schema_version": CHECKPOINT_SCHEMA_VERSION,
                    "source": "slack",
                    "run_id": self.archive.run_id,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    "team_id": team_id,
                    "self_user_id": self_user_id,
                    "since": since.isoformat(),
                    "high_watermarks": high_watermarks,
                    "thread_watch": thread_watch,
                    "skipped_channels": skipped_channels,
                }
            )
            checkpoint_advanced = True
        elif truncated:
            archive.note_coverage(
                "slack.checkpoint_withheld_truncated: the run was bounded or truncated, so the "
                "checkpoint was left at its previous position."
            )

        manifest_path = self.archive.finish(
            {
                "status": status,
                "team_id": team_id,
                "self_user_id": self_user_id,
                "since": since.isoformat(),
                "channels_seen": channels_seen,
                "channels_attempted": len(channels),
                "channels_collected": channels_collected,
                "events": len(events),
                "skipped_channels": skipped_channels,
                "high_watermarks": high_watermarks,
                "counters": counters,
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
            checkpoint_advanced=checkpoint_advanced,
            messages_seen=messages_seen,
            threads_repolled=threads_repolled,
            search_matches_kept=search_kept,
            search_matches_context_filtered=search_filtered,
            search_matches_before_window=search_before_window,
            counters=counters,
        )


def _prune_thread_watch(
    thread_watch: dict[str, dict[str, str]], lookback_days: int
) -> dict[str, dict[str, str]]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).timestamp()
    pruned: dict[str, dict[str, str]] = {}
    for channel_id, threads in thread_watch.items():
        kept = {
            thread_ts: latest
            for thread_ts, latest in threads.items()
            if (_as_float(latest) or 0.0) >= cutoff
        }
        if kept:
            pruned[channel_id] = dict(sorted(kept.items()))
    return dict(sorted(pruned.items()))


def make_slack_collector(
    *,
    archive_root: Path,
    environment: str,
    token: str | None = None,
    capture_density: str = "full",
    dry_run: bool = False,
    # Where a run publishes its derived progress snapshot. None falls back to
    # APP_CONFIG_ROOT, and to no snapshot at all when that is unset.
    config_root: Path | None = None,
) -> tuple[SlackClient, RawArchive, SlackCollector]:
    resolved = token or os.environ.get("SLACK_USER_TOKEN")
    if not resolved:
        raise SystemExit("SLACK_USER_TOKEN is required; run scripts/install-slack-secret.sh")
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:10]
    client = SlackClient(resolved)
    archive = RawArchive(
        archive_root,
        "slack",
        run_id,
        environment,
        capture_profile=SLACK_CAPTURE_PROFILE,
        capture_density=capture_density,
        dry_run=dry_run,
        config_root=config_root,
    )
    return client, archive, SlackCollector(client, archive)
