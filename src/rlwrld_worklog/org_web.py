"""The organisation and the digests, served to the backoffice.

Everything here reads what a batch already wrote, and nothing generates: a
person's day changes when the digest batch next builds it. That is the
contract HK set on 2026-09-11 -- 이건 코드여야지, 네가 하면 안됨.

Two POSTs, each a person acting rather than the screen acting on its own:

* `resolve` -- a person answering whose an unknown account is. What it writes
  is an identity marked `resolved`, so a reader can always tell a person's
  answer from what the roster said.
* `refresh` -- a person asking for the roster sync now instead of waiting for
  the daily batch (HK, 2026-10-07). It runs the same code as `worklog org
  sync --apply`; the sheet stays the only place the chart comes from.

Both carry CSRF like every other mutation.
"""

from __future__ import annotations

import os
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from .admin_web import (
    _require_csrf,
    require_page_access,
    require_super_admin_session,
    session_actor,
    store,
)

router = APIRouter(prefix="/api/v1/admin/org")


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        # Said rather than returned as an empty page: "the database is not
        # configured" and "the organisation is empty" are different answers
        # and must not look the same.
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
    return url


@router.get("/chart")
def org_chart_route(
    request: Request,
    include_retired: Annotated[bool, Query()] = False,
) -> dict[str, Any]:
    """The org chart from the newest roster observation of each tab."""
    require_page_access("org")(request)
    from .org.chart import org_chart
    from .org.sync import roster_sheet_url

    return {
        **org_chart(_database_url(), include_retired=include_retired),
        "roster_url": roster_sheet_url(),
    }


@router.post("/refresh")
def refresh_route(request: Request) -> dict[str, Any]:
    """Read the roster sheet again and record what it says now.

    An unchanged workbook writes nothing -- the same digest check the daily
    batch uses -- so pressing the button twice does not fill the history
    with identical observations.
    """
    current = require_super_admin_session(request)
    _require_csrf(request, current)
    database_url = _database_url()
    token_path = store().secret_path("google_token")
    if not token_path.is_file():
        raise HTTPException(
            status_code=503,
            detail="the Google token is not configured: authorize it on the 연결 screen",
        )
    from .google_auth import DRIVE_READONLY_SCOPE, load_credentials
    from .org.sheet import export_workbook
    from .org.sync import UnusableRoster, sync_workbook

    try:
        data = export_workbook(load_credentials(token_path, [DRIVE_READONLY_SCOPE]))
    except Exception as error:  # noqa: BLE001 -- Google's errors have no common base
        # Said as the sheet's failure rather than a 500: an expired token or a
        # Drive outage is not a bug in the chart.
        raise HTTPException(status_code=502, detail=f"the roster sheet could not be read: {error}") from error
    try:
        outcome = sync_workbook(database_url, data, refuse_empty=True)
    except UnusableRoster as error:
        # A missing tab or an emptied one: refused before anything is written,
        # because recording it would retire everybody in that tab.
        raise HTTPException(status_code=502, detail=str(error)) from error
    store().audit(
        "org.refreshed",
        actor=session_actor(current),
        details={
            "workbook_sha256": outcome["workbook_sha256"],
            "tabs": [
                {key: tab[key] for key in ("source", "observation_id", "people", "unchanged_workbook")}
                for tab in outcome["tabs"]
            ],
        },
    )
    return outcome


@router.get("/status")
def org_status_route(request: Request) -> dict[str, Any]:
    require_page_access("org")(request)
    from .org.store import org_status

    return org_status(_database_url())


@router.get("/unmapped")
def unmapped_route(
    request: Request,
    state: Annotated[str, Query(pattern="^(open|resolved|ignored|all)$")] = "open",
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> dict[str, Any]:
    """Accounts with activity and no owner. Read-only: the scan is a batch."""
    require_page_access("org")(request)
    from .org.unmapped import list_unmapped

    return list_unmapped(_database_url(), state=state, limit=limit)


class ResolveRequest(BaseModel):
    kind: str = Field(min_length=1, max_length=40)
    value: str = Field(min_length=1, max_length=400)
    person_id: str | None = Field(default=None, max_length=80)
    ignore: bool = False
    note: str | None = Field(default=None, max_length=500)


@router.post("/unmapped/resolve")
def resolve_route(request: Request, payload: ResolveRequest) -> dict[str, Any]:
    """A person answering whose account this is, or judging it not a person."""
    current = require_super_admin_session(request)
    _require_csrf(request, current)
    from .org.unmapped import resolve

    try:
        outcome = resolve(
            _database_url(),
            kind=payload.kind,
            value=payload.value,
            person_id=payload.person_id,
            ignore=payload.ignore,
            note=payload.note,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    if not outcome.get("ok"):
        raise HTTPException(status_code=404, detail=outcome.get("reason", "not found"))
    return outcome


@router.get("/digest/status")
def digest_status_route(request: Request) -> dict[str, Any]:
    require_page_access("org")(request)
    from .digest import digest_status

    return digest_status(_database_url())


@router.get("/digest/{person_id}")
def person_day_route(
    request: Request,
    person_id: str,
    day: Annotated[str, Query(pattern=r"^\d{4}-\d{2}-\d{2}$")],
) -> dict[str, Any]:
    """One person's day, exactly as the batch stored it.

    A day with no row is reported as such rather than as a day with no
    activity: "the digest has not been built for this day" and "this person
    did nothing" are different facts, and a screen that showed them the same
    way would make an unbuilt backfill look like a quiet week.
    """
    require_page_access("org")(request)
    from .digest import read_digest

    found = read_digest(_database_url(), person_id, date.fromisoformat(day))
    if found is None:
        return {"person_id": person_id, "day": day, "built": False}
    return {"built": True, **found}
