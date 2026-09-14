"""The organisation and the digests, served read-only to the backoffice.

Everything here reads what a batch already wrote. Nothing on these routes
collects, syncs, or generates: the org chart changes when the roster sync
next runs, and a person's day changes when the digest batch next builds it.
That is the contract HK set on 2026-09-11 -- 이건 코드여야지, 네가 하면 안됨
-- and a screen that could regenerate its own contents would quietly break it.

The one exception is `resolve`, which is a person answering a question the
system asked: whose account this is. It is a POST, it carries CSRF like every
other mutation, and what it writes is an identity marked `resolved` so a
reader can always tell a person's answer from what the roster said.
"""

from __future__ import annotations

import os
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from .admin_web import _require_csrf, require_super_admin_session

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
    require_super_admin_session(request)
    from .org.chart import org_chart

    return org_chart(_database_url(), include_retired=include_retired)


@router.get("/status")
def org_status_route(request: Request) -> dict[str, Any]:
    require_super_admin_session(request)
    from .org.store import org_status

    return org_status(_database_url())


@router.get("/unmapped")
def unmapped_route(
    request: Request,
    state: Annotated[str, Query(pattern="^(open|resolved|ignored|all)$")] = "open",
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> dict[str, Any]:
    """Accounts with activity and no owner. Read-only: the scan is a batch."""
    require_super_admin_session(request)
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
    require_super_admin_session(request)
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
    require_super_admin_session(request)
    from .digest import read_digest

    found = read_digest(_database_url(), person_id, date.fromisoformat(day))
    if found is None:
        return {"person_id": person_id, "day": day, "built": False}
    return {"built": True, **found}
