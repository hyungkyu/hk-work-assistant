from __future__ import annotations

import gzip
import http.client
import json
import sys
import unittest
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rlwrld_worklog.archive import RawArchive
from rlwrld_worklog.github_collector import KST
from rlwrld_worklog.slack_client import HttpResponse, SlackApiError, SlackClient
from rlwrld_worklog.slack_collector import (
    SlackCollector,
    _is_expected_search_match,
    parse_since,
    parse_until,
)


class QueueTransport:
    def __init__(self, responses: list[HttpResponse]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, Mapping[str, str], Mapping[str, Any]]] = []

    def get(self, url: str, headers: Mapping[str, str], params: Mapping[str, Any]) -> HttpResponse:
        self.calls.append((url, headers, params))
        return self.responses.pop(0)


class FlakyTransport:
    """Queue of outcomes: an exception is raised, a response is returned."""

    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = outcomes
        self.attempts = 0

    def get(self, url: str, headers: Mapping[str, str], params: Mapping[str, Any]) -> HttpResponse:
        self.attempts += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def response(body: dict[str, Any], *, status: int = 200, headers: Mapping[str, str] | None = None) -> HttpResponse:
    return HttpResponse(status, headers or {}, body)


class SlackClientTests(unittest.TestCase):
    def test_slack_client_retries_429_and_paginates(self) -> None:
        transport = QueueTransport(
            [
                response({"ok": False, "error": "ratelimited"}, status=429, headers={"Retry-After": "2"}),
                response({"ok": True, "items": [1], "response_metadata": {"next_cursor": "next"}}),
                response({"ok": True, "items": [2], "response_metadata": {"next_cursor": ""}}),
            ]
        )
        sleeps: list[float] = []
        client = SlackClient("xoxp-secret", transport=transport, sleeper=sleeps.append)

        pages = list(client.iter_pages("example.list", result_key="items"))

        self.assertEqual([page["items"] for page in pages], [[1], [2]])
        self.assertEqual(sleeps, [2.0])
        self.assertEqual(transport.calls[-1][2]["cursor"], "next")

    def test_a_dropped_response_is_asked_again_instead_of_ending_the_run(self) -> None:
        """A GET that never delivered a verdict can be repeated safely.

        Two August backfills died this way, thousands of calls in, throwing
        away everything already fetched.
        """
        transport = FlakyTransport(
            [
                http.client.IncompleteRead(b"half", 40),
                ConnectionResetError("peer went away"),
                response({"ok": True, "items": [1], "response_metadata": {"next_cursor": ""}}),
            ]
        )
        sleeps: list[float] = []
        client = SlackClient("xoxp-secret", transport=transport, sleeper=sleeps.append)

        body = client.call("example.list")

        self.assertEqual(body["items"], [1])
        self.assertEqual(sleeps, [1.0, 2.0], "backoff doubles between attempts")
        self.assertEqual(client.transport_retries, 2)
        self.assertEqual(client.call_counts["example.list"], 1, "one logical call, not three")

    def test_a_transport_that_keeps_failing_raises_rather_than_returning_less(self) -> None:
        """Exhaustion has to stay loud: a run that gave up is a failed run."""
        transport = FlakyTransport([http.client.IncompleteRead(b"", 10)] * 4)
        client = SlackClient("xoxp-secret", transport=transport, sleeper=lambda _: None)

        with self.assertRaises(http.client.IncompleteRead):
            client.call("example.list")
        self.assertEqual(client.transport_retries, 3, "the final attempt is not a retry")

    def test_an_answered_request_is_not_retried(self) -> None:
        """Only a missing verdict is worth repeating.

        Retrying a request Slack actually answered would multiply real errors
        into silence.
        """
        transport = FlakyTransport([response({"ok": False, "error": "missing_scope"})])
        client = SlackClient("xoxp-secret", transport=transport, sleeper=lambda _: None)

        with self.assertRaises(SlackApiError):
            client.call("users.list")
        self.assertEqual(client.transport_retries, 0)

    def test_slack_error_does_not_expose_token(self) -> None:
        client = SlackClient(
            "xoxp-super-secret",
            transport=QueueTransport([response({"ok": False, "error": "missing_scope"})]),
        )
        with self.assertRaises(SlackApiError) as caught:
            client.call("users.list")
        self.assertNotIn("xoxp", str(caught.exception))
        self.assertNotIn("super-secret", str(caught.exception))

    def test_search_uses_cursor_and_count(self) -> None:
        transport = QueueTransport(
            [
                response(
                    {
                        "ok": True,
                        "messages": {"matches": [{"ts": "1.0"}]},
                        "response_metadata": {"next_cursor": "next"},
                    }
                ),
                response(
                    {
                        "ok": True,
                        "messages": {"matches": []},
                        "response_metadata": {"next_cursor": ""},
                    }
                ),
            ]
        )
        client = SlackClient("xoxp-secret", transport=transport)
        pages = list(client.iter_search_messages("to:me after:2026-08-24"))
        self.assertEqual(len(pages), 2)
        self.assertEqual(transport.calls[0][2]["cursor"], "*")
        self.assertEqual(transport.calls[0][2]["count"], 100)
        self.assertEqual(transport.calls[1][2]["cursor"], "next")

    def test_parse_since_duration(self) -> None:
        now = datetime(2026, 8, 25, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(parse_since("24h", now=now), datetime(2026, 8, 24, 0, 0, tzinfo=timezone.utc))
        self.assertEqual(parse_since("2d", now=now), datetime(2026, 8, 23, 0, 0, tzinfo=timezone.utc))

    def test_parse_since_reads_a_bare_date_as_midnight_kst(self) -> None:
        self.assertEqual(parse_since("2026-08-25"), datetime(2026, 8, 25, 0, 0, tzinfo=KST))

    def test_bare_date_bounds_span_exactly_one_kst_day(self) -> None:
        # The regression this pins: `parse_since` read a bare date as UTC while
        # `parse_until` read one as KST, so a window written with two bare dates
        # ran fifteen hours and was filed as a whole day -- and across a
        # run-per-day backfill the first nine hours of every day belonged to no
        # slice at all. Compare the two bounds rather than either alone: a
        # change that moves both together is a new convention, and one that
        # moves only one is this defect returning.
        since = parse_since("2026-08-25")
        until = parse_until("2026-08-26")
        self.assertEqual(until - since, timedelta(days=1))
        self.assertEqual(since.astimezone(KST).time(), time.min)
        self.assertEqual(until.astimezone(KST).time(), time.min)

    def test_parse_since_keeps_instants_offset_and_all(self) -> None:
        self.assertEqual(
            parse_since("2026-08-25T00:00:00+09:00"), datetime(2026, 8, 25, 0, 0, tzinfo=KST)
        )
        # A time of day makes it an instant, not a date: no offset still means UTC.
        self.assertEqual(
            parse_since("2026-08-25T00:00:00"), datetime(2026, 8, 25, 0, 0, tzinfo=timezone.utc)
        )
        self.assertEqual(
            parse_since("2026-08-25T00:00:00Z"), datetime(2026, 8, 25, 0, 0, tzinfo=timezone.utc)
        )

    def test_search_context_results_are_filtered(self) -> None:
        self.assertTrue(
            _is_expected_search_match(
                "direct-mentions",
                {"text": "hello <@USELF>"},
                self_user_id="USELF",
            )
        )
        self.assertFalse(
            _is_expected_search_match(
                "direct-mentions",
                {"text": "adjacent context only"},
                self_user_id="USELF",
            )
        )
        self.assertTrue(
            _is_expected_search_match(
                "messages-from-self",
                {"user": "USELF"},
                self_user_id="USELF",
            )
        )


class FakeSlackClient:
    def call(self, method: str, **params: Any) -> dict[str, Any]:
        if method == "auth.test":
            return {"ok": True, "team_id": "T1", "user_id": "USELF", "url": "https://example.slack.com/"}
        if method == "usergroups.list":
            return {"ok": True, "usergroups": []}
        raise AssertionError(method)

    def iter_pages(self, method: str, *, result_key: str, limit: int = 200, **params: Any):
        if method == "users.list":
            yield {"ok": True, "members": [{"id": "USELF"}], "response_metadata": {"next_cursor": ""}}
        elif method == "conversations.list":
            yield {"ok": True, "channels": [{"id": "C1", "name": "general"}], "response_metadata": {"next_cursor": ""}}
        elif method == "conversations.history":
            yield {
                "ok": True,
                "messages": [
                    {
                        "type": "message",
                        "user": "U2",
                        "ts": "1787616000.000100",
                        "text": "<@USELF> 확인해주세요",
                        "files": [
                            {
                                "id": "F1",
                                "name": "large.bin",
                                "size": 1000000,
                                "permalink": "https://example.slack.com/files/F1",
                                "preview": "must-not-be-archived",
                            }
                        ],
                    }
                ],
                "response_metadata": {"next_cursor": ""},
            }
        else:
            raise AssertionError(method)

    def iter_search_messages(self, query: str):
        yield {
            "ok": True,
            "messages": {"matches": []},
            "response_metadata": {"next_cursor": ""},
        }


class NoSearchSlackClient(FakeSlackClient):
    def iter_search_messages(self, query: str):
        raise AssertionError("bounded channel collection must not run a workspace-wide search")
        yield  # pragma: no cover


class OneMissingChannelSlackClient(FakeSlackClient):
    def iter_pages(self, method: str, *, result_key: str, limit: int = 200, **params: Any):
        if method == "conversations.list":
            yield {
                "ok": True,
                "channels": [{"id": "C1"}, {"id": "CMISSING"}],
                "response_metadata": {"next_cursor": ""},
            }
            return
        if method == "conversations.history" and params.get("channel") == "CMISSING":
            raise SlackApiError(
                "Slack method conversations.history failed: channel_not_found",
                method="conversations.history",
                code="channel_not_found",
            )
        yield from super().iter_pages(method, result_key=result_key, limit=limit, **params)


class SlackCollectorTests(unittest.TestCase):
    def test_collector_archives_metadata_only_and_normalizes_mentions(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            archive = RawArchive(root, "slack", "run-1", "test")
            collector = SlackCollector(FakeSlackClient(), archive)  # type: ignore[arg-type]

            result = collector.collect(
                since=datetime(2026, 8, 24, tzinfo=timezone.utc),
                expected_team_id="T1",
            )

            self.assertEqual(len(result.events), 1)
            self.assertEqual(result.events[0].mentions[0].direction, "to_self")
            self.assertEqual(
                result.events[0].permalink,
                "https://example.slack.com/archives/C1/p1787616000000100",
            )
            history_path = next(root / item["path"] for item in archive.files if item["kind"] == "history-C1")
            archived = json.loads(gzip.decompress(history_path.read_bytes()))
            self.assertTrue(archived["messages"][0]["files"][0]["permalink"].endswith("/F1"))
            self.assertNotIn("preview", archived["messages"][0]["files"][0])
            self.assertTrue((root / "manifests/slack/test/checkpoint.json").exists())

    def test_max_channels_disables_workspace_wide_search(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            archive = RawArchive(root, "slack", "run-2", "test")
            collector = SlackCollector(NoSearchSlackClient(), archive)  # type: ignore[arg-type]
            result = collector.collect(
                since=datetime(2026, 8, 24, tzinfo=timezone.utc),
                expected_team_id="T1",
                max_channels=1,
                max_messages=20,
            )
            self.assertEqual(result.channels_collected, 1)
            self.assertEqual(len(result.events), 1)

    def test_inaccessible_listed_channel_is_recorded_and_skipped(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            archive = RawArchive(root, "slack", "run-3", "test")
            collector = SlackCollector(OneMissingChannelSlackClient(), archive)  # type: ignore[arg-type]
            result = collector.collect(
                since=datetime(2026, 8, 24, tzinfo=timezone.utc),
                expected_team_id="T1",
            )
            self.assertEqual(result.channels_collected, 1)
            self.assertEqual(result.channels_skipped, 1)
            manifest = json.loads(result.manifest_path.read_text())
            self.assertEqual(manifest["status"], "success_with_skips")
            self.assertEqual(manifest["skipped_channels"][0]["error"], "channel_not_found")
            self.assertTrue((root / "manifests/slack/test/checkpoint.json").exists())


if __name__ == "__main__":
    unittest.main()
