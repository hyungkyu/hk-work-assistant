"""The 릴리즈 노트 screen's one route.

Read-only by construction: there is nothing to POST here. Deploying is the
batch's job, and a button on this page that could deploy would be a second
authority over what runs in production.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query, Request

from .admin_web import require_page_access

router = APIRouter(prefix="/api/v1/admin/release-notes")


@router.get("")
def release_notes_route(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    require_page_access("release")(request)
    from . import release_notes

    return release_notes.read(limit=limit)
