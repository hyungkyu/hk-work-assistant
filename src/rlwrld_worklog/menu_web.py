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

from .admin_web import _require_csrf, require_super_admin_session

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
    require_super_admin_session(request)
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
    return {**found, "source": "database"}


class MenuEntry(BaseModel):
    page_id: str = Field(min_length=1)
    label: str | None = None
    group_label: str | None = None
    hidden: bool = False


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
