"""Daily incremental Google Calendar capture over the official read-only API.

Coverage design, and the honest limits of it:

  * `calendarList.list` is walked in full on every run with `showDeleted` and
    `showHidden`, so calendar metadata (summary, timezone, accessRole, primary,
    deleted) is re-observed daily and the service DB can be rebuilt from raw.
  * Events use one `nextSyncToken` per calendar with `showDeleted=true` and
    `singleEvents=false`, so recurrence masters, `recurringEventId`,
    `originalStartTime`, attendee response states, organizers, conference data,
    reminders, attachment metadata, `updated`, `etag` and `status` all arrive
    verbatim, and cancellations arrive as `status: cancelled` rows.
  * An expired sync token (HTTP 410) triggers a full resync of **that calendar
    only**, bounded by `timeMin`. Nothing older is deleted: the previous run's
    raw pages stay exactly where they are, and the resync writes new pages in a
    new run directory.
  * Each calendar's token advances independently and only when that calendar
    completed, so one inaccessible calendar cannot corrupt another's position.
  * A first run, and a run after a 410, sees only events from `timeMin`
    forward. Events older than that window keep their last raw observation and
    are not re-observed; this is recorded in coverage rather than implied to be
    complete.

Attachments are metadata and links only; no file body is fetched.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from .archive import RawArchive
from .calendar_client import CalendarApiError, CalendarSyncExpired
from .links import extract_notion_urls
from .models import TimelineEvent
from .normalizers import normalize_calendar

CALENDAR_CAPTURE_PROFILE = "live-google-calendar-api/v1"
CHECKPOINT_SCHEMA_VERSION = 2

COVERAGE_NOTES = (
    "google_calendar.first_sync_is_window_bounded: a calendar with no sync token, and a "
    "calendar recovering from an expired token, is read from timeMin forward. Events older "
    "than that window keep their previous raw observation and are not re-observed.",
    "google_calendar.deletions_arrive_as_cancelled: a removed event is returned with "
    "status=cancelled by an incremental sync; a calendar removed from calendarList is "
    "preserved with deleted=true instead of being dropped.",
    "google_calendar.attachments_are_metadata_only: event attachments are preserved as the "
    "fileId/fileUrl/title metadata the API returns; no file body is downloaded.",
)


class CalendarClient(Protocol):
    def list_calendars(self, *, page_token: str | None = None) -> dict[str, Any]: ...

    def list_events(
        self,
        calendar_id: str,
        *,
        page_token: str | None = None,
        sync_token: str | None = None,
        time_min: str | None = None,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class CalendarCollectionResult:
    run_id: str
    calendars_seen: int
    calendars_collected: int
    calendars_skipped: int
    events: tuple[TimelineEvent, ...]
    notion_urls: tuple[str, ...]
    manifest_path: Path
    checkpoint_advanced: bool = False
    reset_calendars: tuple[str, ...] = ()
    events_archived: int = 0
    cancelled_events: int = 0
    counters: dict[str, Any] = field(default_factory=dict)


def _load_checkpoint(archive: RawArchive) -> dict[str, Any]:
    checkpoint = archive.read_checkpoint()
    if not checkpoint:
        return {"schema_version": CHECKPOINT_SCHEMA_VERSION, "source": "google-calendar", "sync_tokens": {}}
    return checkpoint


class GoogleCalendarCollector:
    def __init__(self, client: CalendarClient, archive: RawArchive) -> None:
        self.client = client
        self.archive = archive

    def collect(
        self,
        *,
        since: datetime,
        calendar_ids: set[str] | None = None,
        max_calendars: int | None = None,
        advance_checkpoint: bool = True,
    ) -> CalendarCollectionResult:
        archive = self.archive
        checkpoint = _load_checkpoint(archive)
        previous_tokens = {
            str(key): str(value)
            for key, value in (checkpoint.get("sync_tokens") or {}).items()
            if value
        }
        archive.set_checkpoint_in(
            {
                "run_id": checkpoint.get("run_id"),
                "calendars_with_sync_token": sorted(previous_tokens),
            }
        )
        archive.set_requested_window(
            {
                "since": since.isoformat(),
                "mode": "incremental" if previous_tokens else "initial",
                "requested_calendar_ids": sorted(calendar_ids or ()),
                "max_calendars": max_calendars,
            }
        )
        for note in COVERAGE_NOTES:
            archive.note_coverage(note)

        calendar_entries: dict[str, dict[str, Any]] = {}
        page_token = None
        while True:
            page = self.client.list_calendars(page_token=page_token)
            items = page.get("items") or []
            archive.write_page(
                "calendar-list",
                page,
                endpoint="calendarList.list",
                request={"pageToken": page_token, "showDeleted": True, "showHidden": True},
                item_count=len(items),
            )
            for item in items:
                if isinstance(item, dict) and item.get("id"):
                    calendar_entries[str(item["id"])] = item
            page_token = page.get("nextPageToken")
            if not page_token:
                break

        selected_ids = set(calendar_entries)
        requested_missing = sorted((calendar_ids or set()) - selected_ids)
        selected_ids.update(calendar_ids or set())
        for calendar_id in requested_missing:
            archive.note_coverage(
                "google_calendar.calendar_requested_but_not_listed: an explicitly requested "
                "calendar is not in calendarList; it is still queried directly."
            )
        ordered_ids = sorted(selected_ids)
        if max_calendars is not None and len(ordered_ids) > max_calendars:
            ordered_ids = ordered_ids[:max_calendars]
            archive.note_truncation("max_calendars", limit=max_calendars, calendars_seen=len(selected_ids))

        events_by_id: dict[str, TimelineEvent] = {}
        next_tokens: dict[str, str] = dict(previous_tokens)
        skipped: list[dict[str, Any]] = []
        reset_calendars: list[str] = []
        notion_urls: set[str] = set()
        events_archived = 0
        cancelled_events = 0
        per_calendar: dict[str, dict[str, Any]] = {}

        for calendar_id in ordered_ids:
            sync_token = previous_tokens.get(calendar_id)
            mode = "incremental" if sync_token else "initial"
            try:
                pages, next_sync_token = self._event_pages(calendar_id, since=since, sync_token=sync_token)
            except CalendarSyncExpired:
                reset_calendars.append(calendar_id)
                archive.note_skip("sync_token_expired", calendar_id=calendar_id, recovery="full_resync")
                mode = "resync_after_410"
                try:
                    pages, next_sync_token = self._event_pages(calendar_id, since=since, sync_token=None)
                except CalendarApiError as error:
                    detail = {"calendar_id": calendar_id, "error": str(error), "code": error.code}
                    skipped.append(detail)
                    archive.note_skip("calendar_inaccessible", **detail)
                    # The stale token is dropped: it is known-invalid, so the
                    # next run must start a clean resync for this calendar.
                    next_tokens.pop(calendar_id, None)
                    per_calendar[calendar_id] = {"mode": mode, "status": "failed"}
                    continue
            except CalendarApiError as error:
                detail = {"calendar_id": calendar_id, "error": str(error), "code": error.code}
                skipped.append(detail)
                archive.note_skip("calendar_inaccessible", **detail)
                # The previous token is preserved untouched, so this calendar
                # resumes exactly where it left off on the next run.
                per_calendar[calendar_id] = {"mode": mode, "status": "failed"}
                continue

            if next_sync_token:
                next_tokens[calendar_id] = next_sync_token
            elif mode != "incremental":
                archive.note_coverage(
                    "google_calendar.no_sync_token_returned: a full listing returned no "
                    "nextSyncToken, so the next run repeats a window-bounded listing."
                )
            calendar_events = 0
            calendar_cancelled = 0
            for page in pages:
                for raw_event in page.get("items") or []:
                    if not isinstance(raw_event, dict):
                        continue
                    events_archived += 1
                    calendar_events += 1
                    if raw_event.get("status") == "cancelled":
                        cancelled_events += 1
                        calendar_cancelled += 1
                    event_record = dict(raw_event)
                    event_record["calendar_id"] = calendar_id
                    for url in extract_notion_urls(event_record):
                        notion_urls.add(url)
                    try:
                        event = normalize_calendar(event_record)
                    except (KeyError, TypeError, ValueError) as error:
                        archive.note_error(
                            "normalize_failed",
                            calendar_id=calendar_id,
                            event_id=str(raw_event.get("id")),
                            error=type(error).__name__,
                        )
                        continue
                    events_by_id[event.event_id] = event
            per_calendar[calendar_id] = {
                "mode": mode,
                "status": "ok",
                "pages": len(pages),
                "events": calendar_events,
                "cancelled": calendar_cancelled,
                "sync_token_advanced": bool(next_sync_token),
            }

        events = tuple(sorted(events_by_id.values(), key=lambda item: (item.occurred_at, item.event_id)))
        counters = {
            "calendars_listed": len(calendar_entries),
            "calendars_attempted": len(ordered_ids),
            "calendars_requested_not_listed": requested_missing,
            "events_archived": events_archived,
            "cancelled_events": cancelled_events,
            "notion_urls_found": len(notion_urls),
            "per_calendar": per_calendar,
        }
        status = "success_with_skips" if skipped or archive.skips else "success"

        checkpoint_advanced = False
        if advance_checkpoint and not archive.dry_run:
            archive.write_checkpoint(
                {
                    "schema_version": CHECKPOINT_SCHEMA_VERSION,
                    "source": "google-calendar",
                    "run_id": archive.run_id,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    "since": since.isoformat(),
                    "sync_tokens": next_tokens,
                    "skipped_calendars": skipped,
                    "reset_calendars": reset_calendars,
                }
            )
            checkpoint_advanced = True

        manifest_path = archive.finish(
            {
                "status": status,
                "since": since.isoformat(),
                "calendars_seen": len(selected_ids),
                "calendars_collected": len(ordered_ids) - len(skipped),
                "skipped_calendars": skipped,
                "reset_calendars": reset_calendars,
                "events": len(events),
                "notion_urls": sorted(notion_urls),
                "counters": counters,
            }
        )
        return CalendarCollectionResult(
            run_id=archive.run_id,
            calendars_seen=len(selected_ids),
            calendars_collected=len(ordered_ids) - len(skipped),
            calendars_skipped=len(skipped),
            events=events,
            notion_urls=tuple(sorted(notion_urls)),
            manifest_path=manifest_path,
            checkpoint_advanced=checkpoint_advanced,
            reset_calendars=tuple(reset_calendars),
            events_archived=events_archived,
            cancelled_events=cancelled_events,
            counters=counters,
        )

    def _event_pages(
        self, calendar_id: str, *, since: datetime, sync_token: str | None
    ) -> tuple[list[dict[str, Any]], str | None]:
        pages: list[dict[str, Any]] = []
        page_token = None
        next_sync_token = None
        time_min = since.astimezone(timezone.utc).isoformat()
        while True:
            page = self.client.list_events(
                calendar_id,
                page_token=page_token,
                sync_token=sync_token,
                time_min=time_min,
            )
            items = page.get("items") or []
            self.archive.write_page(
                f"events-{calendar_id}",
                page,
                endpoint="events.list",
                request={
                    "calendarId": calendar_id,
                    "pageToken": page_token,
                    "syncToken": "<present>" if sync_token else None,
                    "timeMin": None if sync_token else time_min,
                    "showDeleted": True,
                    "singleEvents": False,
                },
                item_count=len(items),
            )
            pages.append(page)
            page_token = page.get("nextPageToken")
            next_sync_token = page.get("nextSyncToken") or next_sync_token
            if not page_token:
                return pages, next_sync_token


def make_calendar_collector(
    *,
    credentials: Any,
    archive_root: Path,
    environment: str,
    capture_density: str = "full",
    dry_run: bool = False,
    # Where a run publishes its derived progress snapshot. None falls back to
    # APP_CONFIG_ROOT, and to no snapshot at all when that is unset.
    config_root: Path | None = None,
) -> tuple[RawArchive, GoogleCalendarCollector]:
    from .calendar_client import GoogleCalendarClient

    run_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:12]}"
    archive = RawArchive(
        archive_root,
        "google-calendar",
        run_id,
        environment,
        capture_profile=CALENDAR_CAPTURE_PROFILE,
        capture_density=capture_density,
        dry_run=dry_run,
        config_root=config_root,
    )
    return archive, GoogleCalendarCollector(GoogleCalendarClient(credentials), archive)
