from __future__ import annotations

from typing import Any


class CalendarApiError(RuntimeError):
    def __init__(self, message: str, *, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


class CalendarSyncExpired(CalendarApiError):
    pass


class GoogleCalendarClient:
    def __init__(self, credentials: Any) -> None:
        from googleapiclient.discovery import build

        self.service = build("calendar", "v3", credentials=credentials, cache_discovery=False)

    def list_calendars(self, *, page_token: str | None = None) -> dict[str, Any]:
        return (
            self.service.calendarList()
            .list(
                pageToken=page_token,
                maxResults=250,
                showDeleted=True,
                showHidden=True,
            )
            .execute()
        )

    def list_events(
        self,
        calendar_id: str,
        *,
        page_token: str | None = None,
        sync_token: str | None = None,
        time_min: str | None = None,
    ) -> dict[str, Any]:
        from googleapiclient.errors import HttpError

        request = self.service.events().list(
            calendarId=calendar_id,
            pageToken=page_token,
            syncToken=sync_token,
            timeMin=None if sync_token else time_min,
            maxResults=2500,
            showDeleted=True,
            singleEvents=False,
        )
        try:
            return request.execute()
        except HttpError as error:
            status = getattr(error.resp, "status", None)
            if status == 410:
                raise CalendarSyncExpired("Google Calendar sync token expired", code=410) from error
            raise CalendarApiError(f"Google Calendar request failed with HTTP {status}", code=status) from error
