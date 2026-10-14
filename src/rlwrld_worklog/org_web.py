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

from fastapi import APIRouter, HTTPException, Path, Query, Request
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


@router.get("/person/{person_id}")
def person_profile_route(
    request: Request,
    person_id: Annotated[str, Path(pattern=r"^p_[0-9a-f]{12}$")],
) -> dict[str, Any]:
    """The panel that opens when a person is clicked on the chart.

    Behind the same page access as the chart itself: whoever may see the
    chart may see what it says about one person.

    The roster's accounts -- 사번, emails, Slack, GitHub, Notion, Slurm --
    are added for the owner and for nobody else (HK, 2026-10-13: 인사 정보에
    슬랙, 깃헙, 노션, 슬럼, 회사 이메일 등 모든 로스터 정보를 보여줘 ... 나만).
    The chart can be opened to every colleague from the menu editor; these
    must not open with it, so the check is the session's role, not the
    page's setting.
    """
    current = require_page_access("org")(request)
    from .org.profile import person_profile

    database_url = _database_url()
    found = person_profile(database_url, person_id)
    if found is None:
        raise HTTPException(status_code=404, detail="no such person on the roster")
    if current.get("role") == "super_admin":
        from .org.profile import person_accounts

        found["accounts"] = person_accounts(database_url, person_id)
        # 인사정보 상세: 원본 -> 디비 -> 화면 (HK, 2026-10-14). A file newer
        # than the copy here is loaded first, unless the person was edited.
        numbers = [
            item["value"]
            for group in found["accounts"]
            if group["kind"] == "employee_no"
            for item in group["values"]
        ]
        found["cv"] = _cv_read(database_url, numbers)
    return found


def _cv_read(database_url: str, numbers: list[str]) -> dict[str, Any] | None:
    import psycopg

    from .org.cv_store import load_if_newer, read_profile

    with psycopg.connect(database_url) as connection, connection.cursor() as cursor:
        load_if_newer(cursor, numbers)
        return read_profile(cursor, numbers)


def _cv_cursor_call(fn, *args):
    import psycopg

    with psycopg.connect(_database_url()) as connection, connection.cursor() as cursor:
        try:
            return fn(cursor, *args)
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error


@router.get("/cv/related/{entity_id}")
def cv_related_route(request: Request, entity_id: Annotated[int, Path(ge=1)]) -> dict[str, Any]:
    """Who else was at this company, school, lab, project or paper, and when.

    HK, 2026-10-13: 클릭하면, 해당 회사의 모든 구성원, 겹치는 기간. Owner only.
    """
    require_super_admin_session(request)
    from .org.cv_store import related

    return _cv_cursor_call(related, entity_id)


@router.get("/cv/entities")
def cv_entities_route(
    request: Request, kind: Annotated[str, Query(pattern="^(org|school|advisor|project|paper)$")]
) -> dict[str, Any]:
    """The names a row can be merged into, and pairs that look like one place."""
    require_super_admin_session(request)
    from .org.cv_store import entity_list, suggest_same

    return {
        "entities": _cv_cursor_call(entity_list, kind),
        "suggested": _cv_cursor_call(suggest_same, kind),
    }


class CvEdit(BaseModel):
    table: str = Field(pattern="^cv_(profile|education|experience|project|publication|etc)$")
    row_id: str = Field(min_length=1, max_length=40)
    field: str = Field(min_length=1, max_length=40)
    value: Any = None


@router.post("/cv/edit")
def cv_edit_route(request: Request, payload: CvEdit) -> dict[str, Any]:
    """Correct one field read wrongly from the original. Kept across re-reads."""
    current = require_super_admin_session(request)
    _require_csrf(request, current)
    from .org.cv_store import edit_row

    row: Any = payload.row_id if payload.table == "cv_profile" else int(payload.row_id)
    _cv_cursor_call(edit_row, payload.table, row, payload.field, payload.value)
    store().audit("cv.edited", actor=session_actor(current), details=payload.model_dump())
    return {"ok": True}


class CvEntity(BaseModel):
    entity_id: int = Field(ge=1)
    rename: str | None = Field(default=None, max_length=400)
    merge_into: int | None = Field(default=None, ge=1)
    unmerge: bool = False


@router.post("/cv/entity")
def cv_entity_route(request: Request, payload: CvEntity) -> dict[str, Any]:
    """Rename, merge (삼성전자 -> 삼성) or separate again a company, school, ..."""
    current = require_super_admin_session(request)
    _require_csrf(request, current)
    from .org.cv_store import merge_entity, rename_entity, unmerge_entity

    if payload.unmerge:
        _cv_cursor_call(unmerge_entity, payload.entity_id)
    elif payload.merge_into:
        _cv_cursor_call(merge_entity, payload.entity_id, payload.merge_into)
    elif payload.rename:
        _cv_cursor_call(rename_entity, payload.entity_id, payload.rename)
    else:
        raise HTTPException(status_code=400, detail="rename, merge_into or unmerge")
    store().audit("cv.entity", actor=session_actor(current), details=payload.model_dump())
    return {"ok": True}


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


class BulkResolve(BaseModel):
    what: str = Field(pattern="^(email|bots)$")


@router.post("/unmapped/resolve-bulk")
def resolve_bulk_route(request: Request, payload: BulkResolve) -> dict[str, Any]:
    """「이메일 일치 전부 연결」 / 「봇 전부 봇으로」 -- the unambiguous ones only."""
    current = require_super_admin_session(request)
    _require_csrf(request, current)
    from .org.unmapped import resolve_in_bulk

    return resolve_in_bulk(_database_url(), what=payload.what)


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
    from .digest import day_is_covered, read_digest

    database_url = _database_url()
    found = read_digest(database_url, person_id, date.fromisoformat(day))
    if found is None:
        # The builder writes rows only for people who did something, so no
        # row on a built day is a quiet day. It used to be reported as 미생성,
        # which sent people to build a day that was already built.
        quiet = day_is_covered(database_url, date.fromisoformat(day))
        return {"person_id": person_id, "day": day, "built": quiet, "quiet": quiet}
    return {"built": True, **found}


@router.get("/digest/{person_id}/range")
def person_range_route(
    request: Request,
    person_id: str,
    start: Annotated[str, Query(pattern=r"^\d{4}-\d{2}-\d{2}$")],
    end: Annotated[str, Query(pattern=r"^\d{4}-\d{2}-\d{2}$")],
) -> dict[str, Any]:
    """A person's days over a period. HK, 2026-10-12: 기간 별로도 볼 수 있게."""
    require_page_access("org")(request)
    from .digest import read_range

    try:
        return read_range(
            _database_url(), person_id, date.fromisoformat(start), date.fromisoformat(end)
        )
    except LookupError as error:
        raise HTTPException(status_code=404, detail=f"no such person: {person_id}") from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


class BuildDay(BaseModel):
    day: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")


@router.post("/digest/build")
def build_day_route(request: Request, payload: BuildDay) -> dict[str, Any]:
    """Build one day now, from the database alone.

    HK, 2026-10-12: 클릭하면 디비에서 실시간 생성하면 되지 않아? It does --
    a whole day for every person measured 1.2 seconds -- and it builds the
    whole day rather than one person, because "the day ran" is decided by
    whether any row exists for it: a single person's row would make the
    nightly catch-up skip everybody else.

    No Google Drive here, so meeting-note bodies are left out until those
    are collected with the calendar instead of fetched at build time.
    Owner only: it writes, and it rewrites what the nightly batch built.
    """
    current = require_super_admin_session(request)
    _require_csrf(request, current)
    from datetime import datetime, timedelta, timezone

    from .digest import build_day

    day = date.fromisoformat(payload.day)
    today = (datetime.now(timezone.utc) + timedelta(hours=9)).date()
    if day > today:
        raise HTTPException(status_code=400, detail="a day that has not happened yet")
    result = build_day(_database_url(), day).as_dict()
    return {
        "day": payload.day,
        "partial": day == today,
        "people_with_activity": result.get("people_with_activity"),
        "events": result.get("events"),
        "seconds": result.get("seconds"),
        "errors": result.get("errors"),
    }
