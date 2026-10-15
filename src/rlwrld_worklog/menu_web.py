"""The left menu's arrangement, read by every page load and written by one screen.

Read is the hot path -- the backoffice calls it before it draws anything -- so
it must never be the reason the page does not come up. When the database is
unreachable the route says so plainly and the page falls back to the order in
the markup, which is always correct about what exists even when nothing has
been arranged.
"""

from __future__ import annotations

import os
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from .admin_web import _require_csrf, require_reader_session, require_super_admin_session, session_actor

router = APIRouter(prefix="/api/v1/admin/menu")


def _database_url() -> str | None:
    return os.environ.get("DATABASE_URL") or None


@router.get("")
def menu_route(request: Request) -> dict[str, Any]:
    """The menu as it should be drawn.

    Degrades rather than fails. A backoffice that cannot draw its own
    navigation because a query failed is a backoffice nobody can use to find
    out why the query failed.
    """
    # Every signed-in company user needs the saved access decisions in order
    # to discover which menu entries are public.  Keeping this read behind the
    # owner guard leaves the safe, owner-only HTML defaults in place and makes
    # every public entry invisible.  Saving the arrangement remains owner-only
    # below.
    current = require_reader_session(request)
    from . import menu

    declared = menu.declared_pages()
    url = _database_url()
    if not url:
        arranged = menu.arrange(declared, [])
        return {
            "pages": arranged,
            "groups": menu.as_groups(arranged),
            "unarranged": len(arranged),
            "source": "markup",
            "reason": "DATABASE_URL is not configured",
        }

    try:
        found = menu.read(url)
    except Exception as error:  # pragma: no cover - exercised by the fallback test
        arranged = menu.arrange(declared, [])
        return {
            "pages": arranged,
            "groups": menu.as_groups(arranged),
            "unarranged": len(arranged),
            "source": "markup",
            "reason": str(error)[:200],
        }
    return {**_for_session(current, found, url), "source": "database"}


def _for_session(current: dict[str, Any], found: dict[str, Any], url: str) -> dict[str, Any]:
    """A colleague's menu shows the screens their role was granted as open.

    권한 체계 (HK, 2026-10-15). The owner sees the arrangement as saved, so
    the editor keeps showing the real 공개 switches.
    """
    if current.get("role") != "company_user":
        return found
    from . import access

    granted = {
        resource[5:]
        for resource in access.grants(access.role_of(current, url) or "general", url)
        if resource.startswith("page:")
    }
    if not granted:
        return found

    def mark(entry: dict[str, Any]) -> dict[str, Any]:
        if entry.get("page_id") in granted:
            return {**entry, "requires": "company_user", "owner_only": False}
        return entry

    return {
        **found,
        "pages": [mark(page) for page in found.get("pages", [])],
        "groups": [
            {**group, "entries": [mark(entry) for entry in group.get("entries", [])]}
            for group in found.get("groups", [])
        ],
    }


@router.get("/access")
def access_route(request: Request) -> dict[str, Any]:
    """권한: roles, who holds which, and what each role may reach. Owner only."""
    require_super_admin_session(request)
    from . import access, menu

    url = _database_url()
    if not url:
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
    return access.overview(url, menu.read(url).get("pages", []))


class AccessUser(BaseModel):
    email: str = Field(min_length=3, max_length=200)
    role: str | None = Field(default=None, pattern="^(general|sensitive)$")


@router.post("/access/user")
def access_user_route(request: Request, payload: AccessUser) -> dict[str, Any]:
    current = require_super_admin_session(request)
    _require_csrf(request, current)
    from . import access

    try:
        access.set_user_role(_database_url() or "", payload.email, payload.role, session_actor(current))
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {"ok": True}


class AccessGrant(BaseModel):
    role: str = Field(pattern="^(general|sensitive)$")
    resource: str = Field(pattern="^(page|area):[a-z0-9_]+$")
    on: bool


@router.post("/access/grant")
def access_grant_route(request: Request, payload: AccessGrant) -> dict[str, Any]:
    current = require_super_admin_session(request)
    _require_csrf(request, current)
    from . import access

    try:
        access.set_grant(_database_url() or "", payload.role, payload.resource, payload.on, session_actor(current))
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {"ok": True}


class MenuEntry(BaseModel):
    page_id: str = Field(min_length=1)
    label: str | None = None
    group_label: str | None = None
    hidden: bool = False
    # "company_user" opens the screen to anybody signed in from the company;
    # anything else, including an absent field, means super_admin only. The
    # permissive value has to be spelled out -- a dropped or misspelled field
    # can only ever shut a screen, never open one.
    requires: str | None = None


class SaveMenu(BaseModel):
    # The whole menu in the order the person arranged it. Order is the list's
    # order -- a `position` field would be a second place for it to be wrong.
    entries: Annotated[list[MenuEntry], Field(min_length=1)]


@router.post("")
def save_menu_route(request: Request, payload: SaveMenu) -> dict[str, Any]:
    current = require_super_admin_session(request)
    _require_csrf(request, current)
    from . import menu

    url = _database_url()
    if not url:
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
    try:
        return menu.save(
            url,
            [entry.model_dump() for entry in payload.entries],
            actor=str(current.get("email") or "backoffice"),
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
