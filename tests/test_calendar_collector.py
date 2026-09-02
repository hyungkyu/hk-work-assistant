"""Google Calendar daily incremental capture.

A scripted fake API stands in for Google: no network call is made anywhere in
this file. Every identifier and every piece of text is invented.
"""

from __future__ import annotations

import gzip
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rlwrld_worklog.archive import RawArchive
from rlwrld_worklog.calendar_client import CalendarApiError, CalendarSyncExpired
from rlwrld_worklog.calendar_collector import GoogleCalendarCollector

PRIMARY = "owner@example.invalid"
TEAM = "team@example.invalid"
SINCE = datetime(2026, 8, 14, tzinfo=timezone.utc)


def event(event_id: str, **overrides: Any) -> dict[str, Any]:
    body = {
        "id": event_id,
        "status": "confirmed",
        "etag": f'"{event_id}-1"',
        "created": "2026-08-20T00:00:00Z",
        "updated": "2026-08-20T01:00:00Z",
        "start": {"dateTime": "2026-08-20T02:00:00Z"},
        "end": {"dateTime": "2026-08-20T03:00:00Z"},
    }
    body.update(overrides)
    return body


class FakeCalendarClient:
    def __init__(
        self,
        *,
        calendars: list[dict[str, Any]] | None = None,
        calendar_pages: list[dict[str, Any]] | None = None,
        events: dict[str, list[dict[str, Any]]] | None = None,
        event_pages: dict[str, list[dict[str, Any]]] | None = None,
        failing: dict[str, CalendarApiError] | None = None,
    ) -> None:
        self.calendars = calendars if calendars is not None else [
            {"id": TEAM, "accessRole": "reader", "summary": "team"}
        ]
        self.calendar_pages = calendar_pages
        self.events = events if events is not None else {
            TEAM: [
                event(
                    "event-1",
                    description=(
                        "notes https://www.notion.so/Meeting-0123456789abcdef0123456789abcdef"
                    ),
                )
            ]
        }
        self.event_pages = event_pages or {}
        self.failing = failing or {}
        self.event_calls: list[dict[str, Any]] = []
        self.calendar_calls = 0

    def list_calendars(self, *, page_token=None):
        self.calendar_calls += 1
        if self.calendar_pages is not None:
            index = 0 if page_token is None else int(page_token)
            return self.calendar_pages[index]
        return {"items": self.calendars}

    def list_events(self, calendar_id, *, page_token=None, sync_token=None, time_min=None):
        self.event_calls.append(
            {
                "calendar_id": calendar_id,
                "sync_token": sync_token,
                "time_min": time_min,
                "page_token": page_token,
            }
        )
        if calendar_id in self.failing:
            raise self.failing[calendar_id]
        if calendar_id in self.event_pages:
            pages = self.event_pages[calendar_id]
            index = 0 if page_token is None else int(page_token)
            return pages[index]
        return {"items": self.events.get(calendar_id, []), "nextSyncToken": f"sync-{calendar_id}"}


class ExpiredOnceCalendarClient(FakeCalendarClient):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.expired = False

    def list_events(self, calendar_id, *, page_token=None, sync_token=None, time_min=None):
        if sync_token and not self.expired:
            self.expired = True
            self.event_calls.append({"calendar_id": calendar_id, "sync_token": sync_token, "time_min": time_min})
            raise CalendarSyncExpired("expired", code=410)
        return super().list_events(
            calendar_id, page_token=page_token, sync_token=sync_token, time_min=time_min
        )


def collect(tmp_path: Path, client, *, run_id: str = "run-1", dry_run: bool = False, **kwargs: Any):
    archive = RawArchive(tmp_path, "google-calendar", run_id, "test", dry_run=dry_run)
    result = GoogleCalendarCollector(client, archive).collect(
        since=kwargs.pop("since", SINCE), **kwargs
    )
    return archive, result


def archived(archive: RawArchive, root: Path, kind: str) -> list[dict[str, Any]]:
    return [
        json.loads(gzip.decompress((root / item["path"]).read_bytes()))
        for item in archive.files
        if item["kind"] == kind
    ]


def test_calendar_incremental_archive_and_notion_link(tmp_path: Path) -> None:
    client = FakeCalendarClient()
    _, result = collect(tmp_path, client)

    assert len(result.events) == 1
    assert result.notion_urls == (
        "https://www.notion.so/Meeting-0123456789abcdef0123456789abcdef",
    )
    checkpoint = json.loads((tmp_path / "manifests/google-calendar/test/checkpoint.json").read_text())
    assert checkpoint["sync_tokens"][TEAM] == f"sync-{TEAM}"
    assert client.event_calls[0]["time_min"].startswith("2026-08-14")


def test_expired_sync_token_falls_back_to_full_sync(tmp_path: Path) -> None:
    seed = RawArchive(tmp_path, "google-calendar", "run-0", "test")
    seed.write_checkpoint(
        {"schema_version": 2, "source": "google-calendar", "run_id": "run-0", "sync_tokens": {TEAM: "old"}}
    )
    client = ExpiredOnceCalendarClient()
    _, result = collect(tmp_path, client, run_id="run-2")

    assert result.calendars_collected == 1
    manifest = json.loads(result.manifest_path.read_text())
    assert manifest["reset_calendars"] == [TEAM]
    assert client.event_calls[-1]["sync_token"] is None
    assert manifest["counters"]["per_calendar"][TEAM]["mode"] == "resync_after_410"
    # The prior run's raw pages are untouched by the resync.
    assert (tmp_path / "manifests/google-calendar/test/checkpoints/run-0.json").exists()


def test_calendar_list_and_event_pagination_are_followed(tmp_path: Path) -> None:
    client = FakeCalendarClient(
        calendar_pages=[
            {"items": [{"id": PRIMARY, "primary": True, "accessRole": "owner"}], "nextPageToken": "1"},
            {"items": [{"id": TEAM, "accessRole": "reader"}]},
        ],
        event_pages={
            PRIMARY: [
                {"items": [event("event-a")], "nextPageToken": "1"},
                {"items": [event("event-b")], "nextSyncToken": "sync-primary"},
            ]
        },
        events={TEAM: []},
    )
    archive, result = collect(tmp_path, client)

    assert client.calendar_calls == 2
    assert result.calendars_seen == 2
    assert result.events_archived == 2
    assert len(archived(archive, tmp_path, f"events-{PRIMARY}")) == 2
    checkpoint = json.loads((tmp_path / "manifests/google-calendar/test/checkpoint.json").read_text())
    assert checkpoint["sync_tokens"][PRIMARY] == "sync-primary", (
        "nextSyncToken only appears on the last page and must still be stored"
    )


def test_recurrence_attendees_and_conference_data_reach_the_archive(tmp_path: Path) -> None:
    client = FakeCalendarClient(
        events={
            TEAM: [
                event(
                    "master-1",
                    recurrence=["RRULE:FREQ=WEEKLY;BYDAY=MO"],
                    attendees=[
                        {"email": "a@example.invalid", "responseStatus": "accepted"},
                        {"email": "b@example.invalid", "responseStatus": "tentative", "optional": True},
                    ],
                    organizer={"email": "a@example.invalid"},
                    creator={"email": "a@example.invalid"},
                    conferenceData={"conferenceId": "abc", "entryPoints": [{"uri": "https://meet.invalid/abc"}]},
                    reminders={"useDefault": False, "overrides": [{"method": "popup", "minutes": 10}]},
                    attachments=[{"fileId": "F1", "title": "agenda", "fileUrl": "https://drive.invalid/F1"}],
                ),
                event(
                    "master-1_20260824T020000Z",
                    recurringEventId="master-1",
                    originalStartTime={"dateTime": "2026-08-24T02:00:00Z"},
                ),
            ]
        }
    )
    archive, result = collect(tmp_path, client)

    stored = archived(archive, tmp_path, f"events-{TEAM}")[0]["items"]
    master = next(item for item in stored if item["id"] == "master-1")
    instance = next(item for item in stored if item.get("recurringEventId") == "master-1")
    assert master["recurrence"] == ["RRULE:FREQ=WEEKLY;BYDAY=MO"]
    assert [attendee["responseStatus"] for attendee in master["attendees"]] == ["accepted", "tentative"]
    assert master["conferenceData"]["conferenceId"] == "abc"
    assert master["reminders"]["overrides"][0]["minutes"] == 10
    assert master["attachments"][0]["fileUrl"] == "https://drive.invalid/F1"
    assert instance["originalStartTime"]["dateTime"] == "2026-08-24T02:00:00Z"
    assert result.events_archived == 2


def test_cancelled_events_and_deleted_calendars_are_preserved(tmp_path: Path) -> None:
    client = FakeCalendarClient(
        calendars=[{"id": TEAM, "accessRole": "reader"}, {"id": PRIMARY, "primary": True, "deleted": True}],
        events={TEAM: [event("event-1", status="cancelled")], PRIMARY: []},
    )
    archive, result = collect(tmp_path, client)

    assert result.cancelled_events == 1
    stored = archived(archive, tmp_path, "calendar-list")[0]["items"]
    assert any(item.get("deleted") for item in stored), "a removed calendar stays in the archive"


def test_one_inaccessible_calendar_cannot_disturb_another(tmp_path: Path) -> None:
    seed = RawArchive(tmp_path, "google-calendar", "run-0", "test")
    seed.write_checkpoint(
        {
            "schema_version": 2,
            "source": "google-calendar",
            "run_id": "run-0",
            "sync_tokens": {TEAM: "keep-me", PRIMARY: "primary-old"},
        }
    )
    client = FakeCalendarClient(
        calendars=[{"id": TEAM, "accessRole": "reader"}, {"id": PRIMARY, "primary": True}],
        events={PRIMARY: [event("event-1")]},
        failing={TEAM: CalendarApiError("forbidden", code=403)},
    )
    _, result = collect(tmp_path, client, run_id="run-1")

    assert result.calendars_skipped == 1
    checkpoint = json.loads((tmp_path / "manifests/google-calendar/test/checkpoint.json").read_text())
    assert checkpoint["sync_tokens"][TEAM] == "keep-me", (
        "a failing calendar keeps its position instead of losing it"
    )
    assert checkpoint["sync_tokens"][PRIMARY] == f"sync-{PRIMARY}"
    manifest = json.loads(result.manifest_path.read_text())
    assert manifest["status"] == "success_with_skips"
    assert manifest["counters"]["per_calendar"][TEAM]["status"] == "failed"


def test_a_dead_sync_token_is_dropped_when_the_resync_also_fails(tmp_path: Path) -> None:
    seed = RawArchive(tmp_path, "google-calendar", "run-0", "test")
    seed.write_checkpoint(
        {"schema_version": 2, "source": "google-calendar", "run_id": "run-0", "sync_tokens": {TEAM: "stale"}}
    )

    class AlwaysFailingAfterExpiry(FakeCalendarClient):
        def list_events(self, calendar_id, *, page_token=None, sync_token=None, time_min=None):
            self.event_calls.append({"calendar_id": calendar_id, "sync_token": sync_token})
            if sync_token:
                raise CalendarSyncExpired("expired", code=410)
            raise CalendarApiError("forbidden", code=403)

    _, result = collect(tmp_path, AlwaysFailingAfterExpiry(), run_id="run-1")

    checkpoint = json.loads((tmp_path / "manifests/google-calendar/test/checkpoint.json").read_text())
    assert TEAM not in checkpoint["sync_tokens"], (
        "a token Google already rejected must not be replayed on the next run"
    )
    assert result.calendars_skipped == 1


def test_dry_run_captures_raw_but_never_advances_the_checkpoint(tmp_path: Path) -> None:
    archive, result = collect(tmp_path, FakeCalendarClient(), dry_run=True, max_calendars=1)

    assert result.checkpoint_advanced is False
    assert not (tmp_path / "manifests/google-calendar/test/checkpoint.json").exists()
    assert archive.files
    assert json.loads(result.manifest_path.read_text())["dry_run"] is True


def test_coverage_notes_declare_the_window_bound(tmp_path: Path) -> None:
    _, result = collect(tmp_path, FakeCalendarClient())
    notes = " ".join(json.loads(result.manifest_path.read_text())["coverage_notes"])
    assert "google_calendar.first_sync_is_window_bounded" in notes
    assert "google_calendar.deletions_arrive_as_cancelled" in notes


def test_rerunning_the_same_window_is_idempotent(tmp_path: Path) -> None:
    _, first = collect(tmp_path, FakeCalendarClient(), run_id="run-1")
    _, second = collect(tmp_path, FakeCalendarClient(), run_id="run-2")
    assert [item.event_id for item in first.events] == [item.event_id for item in second.events]
