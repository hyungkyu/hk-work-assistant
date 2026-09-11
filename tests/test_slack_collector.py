"""Slack daily incremental capture.

A scripted fake Web API stands in for Slack: no network call is made anywhere
in this file. Every identifier and every piece of text is invented.
"""

from __future__ import annotations

import gzip
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from rlwrld_worklog.archive import RawArchive
from rlwrld_worklog.slack_client import SlackApiError
from rlwrld_worklog.slack_collector import SlackCollector, _is_expected_search_match

TEAM = "T0TESTWS01"
SELF = "U0TESTSELF"
OTHER = "U0TESTMATE"
CHANNEL = "C0TESTCH01"
DM = "D0TESTDM01"
GROUP = "S0TESTGRP1"

NOW = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
SINCE = NOW - timedelta(days=1)


def ts(offset_seconds: float) -> str:
    return f"{(NOW + timedelta(seconds=offset_seconds)).timestamp():.6f}"


def message(timestamp: str, **overrides: Any) -> dict[str, Any]:
    body = {"type": "message", "user": OTHER, "ts": timestamp, "text": "synthetic message"}
    body.update(overrides)
    return body


def _within(item: dict[str, Any], oldest: float, latest: float | None) -> bool:
    """Slack honours `oldest` and `latest`; so must the fake, or a bound the
    collector sends can be asserted on without ever being obeyed."""
    ts_value = float(item["ts"])
    return ts_value >= oldest and (latest is None or ts_value < latest)


class FakeSlack:
    """Scripted Slack Web API. Records every call for assertions."""

    def __init__(
        self,
        *,
        history: dict[str, list[list[dict[str, Any]]]] | None = None,
        replies: dict[tuple[str, str], list[dict[str, Any]]] | None = None,
        searches: dict[str, list[dict[str, Any]]] | None = None,
        channels: list[dict[str, Any]] | None = None,
        failing_channels: dict[str, str] | None = None,
        usergroups: list[dict[str, Any]] | None = None,
    ) -> None:
        self.history = history or {}
        self.replies = replies or {}
        self.searches = searches or {}
        self.channels = channels if channels is not None else [
            {"id": CHANNEL, "name": "team-channel", "is_private": False},
            {"id": DM, "is_im": True, "user": OTHER},
        ]
        self.failing_channels = failing_channels or {}
        self.usergroups = usergroups if usergroups is not None else [
            {"id": GROUP, "handle": "team", "users": [SELF, OTHER]}
        ]
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.rate_limit_hits = 0
        self.call_counts: dict[str, int] = {}

    def _record(self, method: str, params: dict[str, Any]) -> None:
        self.calls.append((method, dict(params)))
        self.call_counts[method] = self.call_counts.get(method, 0) + 1

    def call(self, method: str, **params: Any) -> dict[str, Any]:
        self._record(method, params)
        if method == "auth.test":
            return {"ok": True, "team_id": TEAM, "user_id": SELF, "url": "https://example.slack.com/"}
        if method == "usergroups.list":
            return {"ok": True, "usergroups": self.usergroups}
        raise AssertionError(method)

    def iter_pages(self, method: str, *, result_key: str, limit: int = 200, **params: Any):
        self._record(method, params)
        if method == "users.list":
            yield {"ok": True, "members": [{"id": SELF, "name": "self"}], "response_metadata": {"next_cursor": "u2"}}
            yield {"ok": True, "members": [{"id": OTHER, "name": "mate"}], "response_metadata": {"next_cursor": ""}}
            return
        if method == "conversations.list":
            yield {"ok": True, "channels": self.channels, "response_metadata": {"next_cursor": ""}}
            return
        if method == "conversations.history":
            channel = str(params["channel"])
            if channel in self.failing_channels:
                raise SlackApiError(
                    f"Slack method conversations.history failed: {self.failing_channels[channel]}",
                    method="conversations.history",
                    code=self.failing_channels[channel],
                )
            oldest = float(params.get("oldest") or 0)
            latest = float(params["latest"]) if params.get("latest") else None
            for page in self.history.get(channel, []):
                visible = [item for item in page if _within(item, oldest, latest)]
                yield {"ok": True, "messages": visible, "response_metadata": {"next_cursor": ""}}
            return
        if method == "conversations.replies":
            key = (str(params["channel"]), str(params["ts"]))
            oldest = float(params.get("oldest") or 0)
            latest = float(params["latest"]) if params.get("latest") else None
            found = [item for item in self.replies.get(key, []) if _within(item, oldest, latest)]
            yield {"ok": True, "messages": found, "response_metadata": {"next_cursor": ""}}
            return
        raise AssertionError(method)

    def iter_search_messages(self, query: str):
        self._record("search.messages", {"query": query})
        for name, matches in self.searches.items():
            if name in query or query.startswith(name):
                yield {"ok": True, "messages": {"matches": matches}, "response_metadata": {"next_cursor": ""}}
                return
        yield {"ok": True, "messages": {"matches": []}, "response_metadata": {"next_cursor": ""}}


def collect(
    tmp_path: Path,
    client: FakeSlack,
    *,
    run_id: str = "run-1",
    dry_run: bool = False,
    **kwargs: Any,
):
    archive = RawArchive(tmp_path, "slack", run_id, "test", dry_run=dry_run)
    result = SlackCollector(client, archive).collect(
        since=kwargs.pop("since", SINCE), expected_team_id=TEAM, **kwargs
    )
    return archive, result


def archived(archive: RawArchive, root: Path, kind: str) -> list[dict[str, Any]]:
    return [
        json.loads(gzip.decompress((root / item["path"]).read_bytes()))
        for item in archive.files
        if item["kind"] == kind
    ]


# ------------------------------------------------------------- enumeration


def test_users_usergroups_and_all_conversation_types_are_captured(tmp_path: Path) -> None:
    client = FakeSlack(history={CHANNEL: [[message(ts(-100))]], DM: [[message(ts(-90))]]})
    archive, result = collect(tmp_path, client)

    assert result.channels_seen == 2
    assert result.channels_collected == 2
    assert result.counters["users_seen"] == 2, "users.list pagination must be followed"
    assert result.counters["usergroups_seen"] == 1
    listed = next(call for call in client.calls if call[0] == "conversations.list")[1]
    assert listed["types"] == "public_channel,private_channel,mpim,im"
    assert listed["exclude_archived"] is False
    manifest = json.loads(result.manifest_path.read_text())
    assert manifest["api_coverage"]["users.list"]["pages"] == 2
    assert manifest["api_coverage"]["conversations.history"]["pages"] == 2


def test_reactions_edits_threads_and_file_links_survive_into_the_archive(tmp_path: Path) -> None:
    parent = ts(-500)
    reply = ts(-100)
    client = FakeSlack(
        history={
            CHANNEL: [
                [
                    message(
                        parent,
                        reply_count=1,
                        latest_reply=reply,
                        reactions=[{"name": "eyes", "users": [SELF], "count": 1}],
                        edited={"user": OTHER, "ts": ts(-400)},
                        files=[
                            {
                                "id": "F0TEST",
                                "name": "notes.pdf",
                                "size": 1024,
                                "permalink": "https://example.slack.com/files/F0TEST",
                                "preview": "must-not-be-archived",
                            }
                        ],
                    )
                ]
            ]
        },
        replies={(CHANNEL, parent): [message(reply, thread_ts=parent, text="synthetic reply")]},
    )
    archive, result = collect(tmp_path, client)

    stored = archived(archive, tmp_path, f"history-{CHANNEL}")[0]["messages"][0]
    assert stored["reactions"][0]["name"] == "eyes"
    assert stored["edited"]["ts"] == ts(-400)
    assert stored["files"][0]["permalink"].endswith("/F0TEST")
    assert "preview" not in stored["files"][0], "file bodies and previews are never archived"
    assert archived(archive, tmp_path, f"replies-{CHANNEL}-{parent}")[0]["messages"][0]["thread_ts"] == parent
    assert result.messages_seen == 2


# -------------------------------------------------------------- checkpoint


def test_history_resumes_from_the_per_channel_watermark(tmp_path: Path) -> None:
    seed = RawArchive(tmp_path, "slack", "run-0", "test")
    seed.write_checkpoint(
        {"schema_version": 2, "source": "slack", "run_id": "run-0", "high_watermarks": {CHANNEL: ts(-300)}}
    )
    client = FakeSlack(history={CHANNEL: [[message(ts(-400)), message(ts(-200))]], DM: [[]]})

    archive, result = collect(tmp_path, client, run_id="run-1")

    history_calls = [
        params for method, params in client.calls
        if method == "conversations.history" and params["channel"] == CHANNEL
    ]
    assert history_calls[0]["oldest"] == ts(-300), "the channel resumes from its own watermark"
    dm_calls = [
        params for method, params in client.calls
        if method == "conversations.history" and params["channel"] == DM
    ]
    assert float(dm_calls[0]["oldest"]) == pytest.approx(SINCE.timestamp()), (
        "a channel with no watermark falls back to --since"
    )
    assert result.messages_seen == 1
    checkpoint = json.loads((tmp_path / "manifests/slack/test/checkpoint.json").read_text())
    assert checkpoint["high_watermarks"][CHANNEL] == ts(-200)
    assert result.checkpoint_advanced is True


def test_a_truncated_run_leaves_the_checkpoint_where_it_was(tmp_path: Path) -> None:
    seed = RawArchive(tmp_path, "slack", "run-0", "test")
    seed.write_checkpoint({"schema_version": 2, "source": "slack", "run_id": "run-0", "high_watermarks": {}})
    client = FakeSlack(history={CHANNEL: [[message(ts(-100)), message(ts(-90))]], DM: [[message(ts(-80))]]})

    _, result = collect(tmp_path, client, run_id="run-1", max_messages=1)

    assert result.truncated is True
    assert result.checkpoint_advanced is False
    checkpoint = json.loads((tmp_path / "manifests/slack/test/checkpoint.json").read_text())
    assert checkpoint["run_id"] == "run-0", "a bounded run must not move the production checkpoint"
    manifest = json.loads(result.manifest_path.read_text())
    assert manifest["truncation"][0]["reason"] == "max_messages"


def test_dry_run_captures_raw_but_never_advances_the_checkpoint(tmp_path: Path) -> None:
    client = FakeSlack(history={CHANNEL: [[message(ts(-100))]], DM: [[]]})
    archive, result = collect(tmp_path, client, dry_run=True)

    assert result.checkpoint_advanced is False
    assert not (tmp_path / "manifests/slack/test/checkpoint.json").exists()
    assert archive.files, "a dry run still preserves what it fetched"
    assert json.loads(result.manifest_path.read_text())["dry_run"] is True


# ------------------------------------------------------------------ threads


def test_replies_to_an_older_thread_are_re_polled_from_the_checkpoint(tmp_path: Path) -> None:
    old_parent = ts(-86_400 * 3)
    new_reply = ts(-60)
    seed = RawArchive(tmp_path, "slack", "run-0", "test")
    seed.write_checkpoint(
        {
            "schema_version": 2,
            "source": "slack",
            "run_id": "run-0",
            "high_watermarks": {CHANNEL: ts(-3600)},
            "thread_watch": {CHANNEL: {old_parent: ts(-7200)}},
        }
    )
    client = FakeSlack(
        history={CHANNEL: [[]], DM: [[]]},
        replies={(CHANNEL, old_parent): [message(new_reply, thread_ts=old_parent, text="late reply")]},
    )

    archive, result = collect(tmp_path, client, run_id="run-1")

    assert result.threads_repolled == 1
    assert result.messages_seen == 1, "the reply is captured even though history never returned it"
    stored = archived(archive, tmp_path, f"replies-{CHANNEL}-{old_parent}")[0]
    assert stored["messages"][0]["ts"] == new_reply
    checkpoint = json.loads((tmp_path / "manifests/slack/test/checkpoint.json").read_text())
    assert checkpoint["thread_watch"][CHANNEL][old_parent] == new_reply


def test_a_parent_stamped_the_way_slack_stamps_one_still_gets_its_replies_swept(
    tmp_path: Path,
) -> None:
    """Slack gives a thread parent a `thread_ts` equal to its own `ts`.

    A guard that skips on the presence of `thread_ts` alone therefore skips
    every parent, and `conversations.replies` is never reached. Fixtures that
    leave `thread_ts` off the parent cannot see that: they describe a shape
    Slack does not send. This one uses the real shape.
    """
    parent = ts(-500)
    reply = ts(-100)
    client = FakeSlack(
        history={CHANNEL: [[message(parent, thread_ts=parent, reply_count=1, latest_reply=reply)]]},
        replies={
            (CHANNEL, parent): [
                message(parent, thread_ts=parent, reply_count=1),
                message(reply, thread_ts=parent, text="synthetic reply"),
            ]
        },
    )

    archive, result = collect(tmp_path, client)

    swept = [params for method, params in client.calls if method == "conversations.replies"]
    assert swept and swept[0]["ts"] == parent, (
        "the parent's replies must be fetched from Slack, not merely present by some other route"
    )
    assert result.threads_repolled == 1
    assert reply in {item["ts"] for item in archived(archive, tmp_path, f"replies-{CHANNEL}-{parent}")[0]["messages"]}


def test_a_reply_carried_by_history_is_not_mistaken_for_a_parent(tmp_path: Path) -> None:
    """The other direction: a genuine reply names a *different* message.

    Sweeping from the reply itself would ask Slack for a thread rooted at a
    timestamp that roots nothing, once per reply. The thread is reached
    through its parent instead - here by the watched-thread re-poll, since a
    parent this window never saw is a parent older than the window.
    """
    parent = ts(-500)
    reply = ts(-100)
    client = FakeSlack(
        history={CHANNEL: [[message(reply, thread_ts=parent, reply_count=4, text="a reply")]]},
        replies={
            (CHANNEL, parent): [
                message(parent, thread_ts=parent, reply_count=1, latest_reply=reply),
                message(reply, thread_ts=parent),
            ]
        },
    )

    _, result = collect(tmp_path, client)

    swept = [params["ts"] for method, params in client.calls if method == "conversations.replies"]
    assert reply not in swept, "a reply must never be used as a thread root"
    assert swept == [parent], "the thread is reached through the parent the reply names"
    assert result.counters["thread_parents_swept"] == 0, (
        "the history sweep counts parents it sighted; this one was reached by the re-poll"
    )


def test_the_sweep_reports_what_it_swept_and_names_replies_it_could_not_fetch(
    tmp_path: Path,
) -> None:
    """A parent declares its reply count, so a shortfall is measurable.

    Reporting a clean success over replies the run never archived is exactly
    the failure this collector is supposed to make impossible.
    """
    parent = ts(-500)
    reply = ts(-100)
    client = FakeSlack(
        history={CHANNEL: [[message(parent, thread_ts=parent, reply_count=3, latest_reply=reply)]]},
        replies={
            (CHANNEL, parent): [
                message(parent, thread_ts=parent, reply_count=3),
                message(reply, thread_ts=parent, text="the only reply Slack returned"),
            ]
        },
    )

    archive, result = collect(tmp_path, client)

    counters = result.counters
    assert counters["thread_parents_swept"] == 1
    assert counters["thread_replies_declared"] == 3
    assert counters["thread_replies_fetched"] == 1, "the parent echo is not counted as a reply"
    manifest = json.loads((tmp_path / f"manifests/slack/test/{result.run_id}.json").read_text())
    assert any(
        note.startswith("slack.thread_replies_incomplete") for note in manifest["coverage_notes"]
    ), "two replies never arrived and the manifest has to say so"


def test_a_fully_swept_thread_does_not_claim_an_incomplete_sweep(tmp_path: Path) -> None:
    parent = ts(-500)
    client = FakeSlack(
        history={CHANNEL: [[message(parent, thread_ts=parent, reply_count=1, latest_reply=ts(-100))]]},
        replies={
            (CHANNEL, parent): [
                message(parent, thread_ts=parent, reply_count=1),
                message(ts(-100), thread_ts=parent),
            ]
        },
    )

    _, result = collect(tmp_path, client)

    manifest = json.loads((tmp_path / f"manifests/slack/test/{result.run_id}.json").read_text())
    assert not [
        note for note in manifest["coverage_notes"] if note.startswith("slack.thread_replies_incomplete")
    ]


def test_a_date_slice_refuses_to_advance_the_checkpoint(tmp_path: Path) -> None:
    """A slice read one window, not everything up to its end.

    Moving a channel's watermark to the slice's end would assert the months
    between that end and the previous watermark had been read, and nothing
    would ever fetch them again.
    """
    seed = RawArchive(tmp_path, "slack", "run-0", "test")
    seed.write_checkpoint(
        {
            "schema_version": 2,
            "source": "slack",
            "run_id": "run-0",
            "high_watermarks": {CHANNEL: ts(-60)},
        }
    )
    old = ts(-86_400 * 200)
    client = FakeSlack(history={CHANNEL: [[message(old)]], DM: [[]]})

    archive, result = collect(
        tmp_path, client, run_id="run-1", since=NOW - timedelta(days=210), until=NOW - timedelta(days=190)
    )

    assert result.checkpoint_advanced is False
    checkpoint = json.loads((tmp_path / "manifests/slack/test/checkpoint.json").read_text())
    assert checkpoint["run_id"] == "run-0", "the earlier checkpoint has to survive untouched"
    assert checkpoint["high_watermarks"][CHANNEL] == ts(-60)
    manifest = json.loads((tmp_path / f"manifests/slack/test/{result.run_id}.json").read_text())
    assert any(note.startswith("slack.date_slice_capture") for note in manifest["coverage_notes"])
    assert manifest["requested_window"]["mode"] == "date_slice"
    assert manifest["requested_window"]["until"] is not None


def test_a_date_slice_reads_the_window_rather_than_resuming_from_the_watermark(
    tmp_path: Path,
) -> None:
    """The watermark tracks the incremental front, far ahead of any old window.

    Honouring it would ask Slack for messages after today and hand back an
    empty slice that still looked like a successful run.
    """
    seed = RawArchive(tmp_path, "slack", "run-0", "test")
    seed.write_checkpoint(
        {"schema_version": 2, "source": "slack", "run_id": "run-0", "high_watermarks": {CHANNEL: ts(-60)}}
    )
    old = ts(-86_400 * 200)
    client = FakeSlack(history={CHANNEL: [[message(old)]], DM: [[]]})

    _, result = collect(
        tmp_path, client, run_id="run-1", since=NOW - timedelta(days=210), until=NOW - timedelta(days=190)
    )

    history = [params for method, params in client.calls if method == "conversations.history"]
    asked = next(p for p in history if p["channel"] == CHANNEL)
    assert float(asked["oldest"]) < float(ts(-60)), "the slice must not resume from the watermark"
    assert asked["latest"] is not None, "the upper bound has to reach Slack"
    assert result.messages_seen == 1


def test_a_date_slice_repolls_only_watched_threads_active_in_that_window(tmp_path: Path) -> None:
    old_parent = ts(-86_400 * 200)
    slice_since = NOW - timedelta(days=210)
    slice_until = NOW - timedelta(days=190)
    in_window_reply = f"{(slice_since + timedelta(days=1)).timestamp():.6f}"
    seed = RawArchive(tmp_path, "slack", "run-0", "test")
    seed.write_checkpoint(
        {
            "schema_version": 2,
            "source": "slack",
            "run_id": "run-0",
            "thread_watch": {CHANNEL: {old_parent: in_window_reply}},
        }
    )
    client = FakeSlack(
        history={CHANNEL: [[]], DM: [[]]},
        replies={(CHANNEL, old_parent): [message(in_window_reply, thread_ts=old_parent)]},
        searches={"direct-mentions": [{"ts": ts(-30), "channel": {"id": CHANNEL}, "text": "<@U0TESTSELF>"}]},
    )

    _, result = collect(
        tmp_path,
        client,
        run_id="run-1",
        since=slice_since,
        until=slice_until,
        prewindow_parent_pages_per_channel=0,
    )

    assert result.threads_repolled == 1
    assert not [method for method, _ in client.calls if method == "search.messages"] or (
        result.search_matches_kept == 0
    ), "a mention from outside the window must not land in the slice"
    assert result.messages_seen == 1


def test_a_date_slice_discovers_a_parent_before_the_window_and_fetches_its_reply(
    tmp_path: Path,
) -> None:
    slice_since = NOW - timedelta(days=10)
    slice_until = slice_since + timedelta(days=1)
    parent = f"{(slice_since - timedelta(days=20)).timestamp():.6f}"
    reply = f"{(slice_since + timedelta(hours=2)).timestamp():.6f}"
    client = FakeSlack(
        history={
            CHANNEL: [[message(parent, thread_ts=parent, reply_count=1, latest_reply=reply)]],
            DM: [[]],
        },
        replies={
            (CHANNEL, parent): [
                message(parent, thread_ts=parent, reply_count=1, latest_reply=reply),
                message(reply, thread_ts=parent),
            ]
        },
    )

    archive, result = collect(
        tmp_path,
        client,
        since=slice_since,
        until=slice_until,
        prewindow_parent_lookback_days=30,
    )

    assert result.messages_seen == 1
    assert {event.external_id for event in result.events} == {f"{TEAM}:{CHANNEL}:{reply}"}
    assert result.counters["prewindow_parent_discovery"]["parents_repolled"] == 1
    assert result.counters["prewindow_parent_discovery"]["pages_per_channel_limit"] == 1
    assert archived(archive, tmp_path, f"parent-discovery-{CHANNEL}")[0]["messages"][0]["ts"] == parent
    assert archived(archive, tmp_path, f"replies-{CHANNEL}-{parent}")[0]["messages"][0]["ts"] == reply


def test_prewindow_parent_discovery_stops_at_the_page_budget(tmp_path: Path) -> None:
    slice_since = NOW - timedelta(days=10)
    client = FakeSlack(
        channels=[{"id": CHANNEL, "name": "team-channel"}],
        history={CHANNEL: [[message(ts(-86_400 * 11))], [message(ts(-86_400 * 12))]]},
    )

    _, result = collect(
        tmp_path,
        client,
        since=slice_since,
        until=slice_since + timedelta(days=1),
        prewindow_parent_pages_per_channel=1,
    )

    assert result.counters["prewindow_parent_discovery"]["pages_read"] == 1


def test_the_ordinary_incremental_run_still_resumes_and_advances(tmp_path: Path) -> None:
    """The slice behaviour is additive: without `until` nothing changes."""
    seed = RawArchive(tmp_path, "slack", "run-0", "test")
    seed.write_checkpoint(
        {"schema_version": 2, "source": "slack", "run_id": "run-0", "high_watermarks": {CHANNEL: ts(-600)}}
    )
    client = FakeSlack(history={CHANNEL: [[message(ts(-60))]], DM: [[]]})

    _, result = collect(tmp_path, client, run_id="run-1")

    assert result.checkpoint_advanced is True
    history = [params for method, params in client.calls if method == "conversations.history"]
    asked = next(p for p in history if p["channel"] == CHANNEL)
    assert asked["oldest"] == ts(-600), "an incremental run still resumes from the watermark"
    assert asked["latest"] is None
    manifest = json.loads((tmp_path / f"manifests/slack/test/{result.run_id}.json").read_text())
    assert not [n for n in manifest["coverage_notes"] if n.startswith("slack.date_slice_capture")]
    assert manifest["requested_window"]["mode"] == "incremental"


def test_threads_older_than_the_lookback_are_dropped_from_the_watch_list(tmp_path: Path) -> None:
    ancient = f"{(NOW - timedelta(days=400)).timestamp():.6f}"
    seed = RawArchive(tmp_path, "slack", "run-0", "test")
    seed.write_checkpoint(
        {"schema_version": 2, "source": "slack", "run_id": "run-0", "thread_watch": {CHANNEL: {ancient: ancient}}}
    )
    client = FakeSlack(history={CHANNEL: [[]], DM: [[]]})

    _, result = collect(tmp_path, client, run_id="run-1")

    assert result.threads_repolled == 0
    checkpoint = json.loads((tmp_path / "manifests/slack/test/checkpoint.json").read_text())
    assert checkpoint["thread_watch"] == {}


# ------------------------------------------------------------------ search


def test_every_critical_search_runs_including_each_self_usergroup(tmp_path: Path) -> None:
    client = FakeSlack(history={CHANNEL: [[]], DM: [[]]})
    _, result = collect(tmp_path, client)

    assert result.counters["searches_run"] == [
        "direct-mentions",
        "direct-messages-to-self",
        "messages-from-self",
        "broadcast-channel",
        "broadcast-here",
        "broadcast-everyone",
        f"usergroup-{GROUP}",
    ]
    assert result.counters["self_usergroups"] == [GROUP]


def test_a_mention_only_search_can_reach_is_captured_and_counted(tmp_path: Path) -> None:
    mention_ts = ts(-120)
    client = FakeSlack(
        history={CHANNEL: [[]], DM: [[]]},
        searches={
            f"<@{SELF}>": [
                {
                    "ts": mention_ts,
                    "user": OTHER,
                    "text": f"<@{SELF}|self> please review",
                    "channel": {"id": "C0OTHERCH", "name": "other"},
                }
            ]
        },
    )
    archive, result = collect(tmp_path, client)

    assert result.search_matches_kept == 1
    assert result.messages_seen == 1
    assert [event.container_id for event in result.events] == ["C0OTHERCH"], (
        "a mention in a channel history never reached must still be collected"
    )
    stored = archived(archive, tmp_path, "direct-mentions")[0]
    assert stored["messages"]["matches"][0]["ts"] == mention_ts


def test_pipe_encoded_mentions_are_not_filtered_out() -> None:
    assert _is_expected_search_match("direct-mentions", {"text": f"<@{SELF}>"}, self_user_id=SELF)
    assert _is_expected_search_match(
        "direct-mentions", {"text": f"hi <@{SELF}|display-name> there"}, self_user_id=SELF
    )
    assert not _is_expected_search_match(
        "direct-mentions", {"text": f"<@{SELF}EXTRA> not me"}, self_user_id=SELF
    )


def test_dropped_search_matches_are_counted_never_silent(tmp_path: Path) -> None:
    client = FakeSlack(
        history={CHANNEL: [[]], DM: [[]]},
        searches={
            f"<@{SELF}>": [
                {"ts": ts(-60), "user": OTHER, "text": "no mention here", "channel": {"id": CHANNEL}},
                {"ts": ts(-86_400 * 5), "user": OTHER, "text": f"<@{SELF}> old", "channel": {"id": CHANNEL}},
            ]
        },
    )
    _, result = collect(tmp_path, client)

    assert result.search_matches_context_filtered == 1
    assert result.search_matches_before_window == 1
    manifest = json.loads(result.manifest_path.read_text())
    assert manifest["counters"]["search_matches_context_filtered"] == 1
    assert manifest["counters"]["search_matches_before_window"] == 1


def test_a_bounded_capture_skips_the_workspace_wide_searches(tmp_path: Path) -> None:
    client = FakeSlack(history={CHANNEL: [[message(ts(-100))]], DM: [[]]})
    _, result = collect(tmp_path, client, max_channels=1)

    assert "search.messages" not in client.call_counts
    assert result.counters["searches_run"] == []


# --------------------------------------------------------- partial coverage


def test_an_inaccessible_channel_is_recorded_and_the_run_continues(tmp_path: Path) -> None:
    client = FakeSlack(
        history={CHANNEL: [[message(ts(-100))]]},
        failing_channels={DM: "not_in_channel"},
    )
    _, result = collect(tmp_path, client)

    assert result.channels_skipped == 1
    assert result.channels_collected == 1
    manifest = json.loads(result.manifest_path.read_text())
    assert manifest["status"] == "success_with_skips"
    assert manifest["skips"][0]["channel_id"] == DM
    assert manifest["skips"][0]["error"] == "not_in_channel"


def test_an_unexpected_slack_error_fails_the_run_rather_than_hiding_it(tmp_path: Path) -> None:
    client = FakeSlack(history={CHANNEL: [[]]}, failing_channels={DM: "internal_error"})
    with pytest.raises(SlackApiError):
        collect(tmp_path, client)


def test_rate_limit_hits_reach_the_manifest(tmp_path: Path) -> None:
    client = FakeSlack(history={CHANNEL: [[]], DM: [[]]})
    client.rate_limit_hits = 4
    _, result = collect(tmp_path, client)
    assert json.loads(result.manifest_path.read_text())["rate_limit_hits"] == 4


def test_coverage_notes_declare_the_web_api_gaps(tmp_path: Path) -> None:
    client = FakeSlack(history={CHANNEL: [[]], DM: [[]]})
    _, result = collect(tmp_path, client)
    notes = " ".join(json.loads(result.manifest_path.read_text())["coverage_notes"])
    assert "slack.message_deletion_not_exposed" in notes
    assert "slack.thread_replies_need_supplements" in notes


def test_rerunning_the_same_window_is_idempotent(tmp_path: Path) -> None:
    payload = {CHANNEL: [[message(ts(-100))]], DM: [[]]}
    _, first = collect(tmp_path, FakeSlack(history=payload), run_id="run-1")
    _, second = collect(tmp_path, FakeSlack(history=payload), run_id="run-2")

    assert [event.event_id for event in first.events] == [event.event_id for event in second.events]
