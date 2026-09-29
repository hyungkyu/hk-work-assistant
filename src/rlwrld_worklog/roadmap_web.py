"""The roadmap, served read-only.

This is the first backoffice screen a company user may open. Everything else
is the owner's, which is why the nav entry for it carries no padlock and this
route asks for a company session rather than a super-admin one.

Read-only on purpose, for the same reason as `org_web`: the roadmap changes
when a refresh runs against Notion, not because somebody opened the page. The
refresh, and the mapping editor that goes with it, are the next task.
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from .admin_web import (
    _json_object,
    _require_csrf,
    require_company_session,
    require_super_admin_session,
    session_actor,
    store,
)

router = APIRouter(prefix="/api/v1/roadmap")


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        # "not configured" and "nothing in it" must not look the same.
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
    return url


@router.get("")
def roadmap_route(request: Request) -> dict[str, Any]:
    """The whole roadmap, in all three languages.

    The languages are not filtered server-side. Ninety-five rows in three
    languages is a small payload, and sending all of it means the language
    toggle is instant and works with the page's back button, rather than a
    round trip that can fail halfway.

    The two ways this fails on a fresh install are named rather than left to
    become a 500. On 2026-09-29 it was a 500, and "Internal Server Error" is
    the same sentence for "you have not run the migration" and "the code is
    broken" -- which are not the same problem and do not have the same fix.
    """
    require_company_session(request)
    import psycopg

    from .roadmap import read_roadmap

    try:
        return read_roadmap(_database_url())
    except psycopg.errors.UndefinedTable as error:
        raise HTTPException(
            status_code=503,
            detail=(
                "the roadmap tables are missing: apply sql/migrations/0012_roadmap.sql, "
                "then seed with `worklog roadmap seed`"
            ),
        ) from error
    except psycopg.OperationalError as error:
        raise HTTPException(
            status_code=503, detail="the database is not reachable from the application"
        ) from error


# ------------------------------------------------------- the mapping editor

# Reading the roadmap is open to the company; deciding what a row *is* -- which
# product it belongs to, whether it is development or operations, which horizon
# it sits in -- is the owner's.
admin_router = APIRouter(prefix="/api/v1/admin/roadmap")


def _edit_error(error: Exception) -> HTTPException:
    return HTTPException(status_code=400, detail=str(error))


@admin_router.get("/mapping")
def mapping_route(request: Request) -> dict[str, Any]:
    require_super_admin_session(request)
    from .roadmap import read_mapping

    return read_mapping(_database_url())


@admin_router.post("/products", status_code=201)
async def create_product_route(request: Request) -> dict[str, Any]:
    current = require_super_admin_session(request)
    _require_csrf(request, current)
    from .roadmap import RoadmapEditError, create_product

    body = await _json_object(request)
    try:
        product = create_product(_database_url(), body)
    except RoadmapEditError as error:
        raise _edit_error(error) from error
    store().audit("roadmap.product_created", actor=session_actor(current), details=product)
    return {"product": product}


@admin_router.patch("/products/{product_id}")
async def update_product_route(product_id: int, request: Request) -> dict[str, Any]:
    current = require_super_admin_session(request)
    _require_csrf(request, current)
    from .roadmap import RoadmapEditError, update_product

    body = await _json_object(request)
    try:
        product = update_product(_database_url(), product_id, body)
    except RoadmapEditError as error:
        raise _edit_error(error) from error
    store().audit("roadmap.product_updated", actor=session_actor(current), details=product)
    return {"product": product}


@admin_router.delete("/products/{product_id}")
def delete_product_route(product_id: int, request: Request) -> dict[str, bool]:
    current = require_super_admin_session(request)
    _require_csrf(request, current)
    from .roadmap import RoadmapEditError, delete_product

    try:
        delete_product(_database_url(), product_id)
    except RoadmapEditError as error:
        raise _edit_error(error) from error
    store().audit(
        "roadmap.product_deleted", actor=session_actor(current), details={"id": product_id}
    )
    return {"ok": True}


@admin_router.patch("/items/{item_key}")
async def update_item_route(item_key: str, request: Request) -> dict[str, Any]:
    """Re-file one row, and record that a hand did it.

    The `*_override` flag this raises is what a refresh reads to know it must
    leave the value alone. Without it the next refresh would quietly undo every
    correction made here, which is the failure this whole screen would
    otherwise cause rather than prevent.
    """
    current = require_super_admin_session(request)
    _require_csrf(request, current)
    from .roadmap import RoadmapEditError, update_item

    body = await _json_object(request)
    try:
        item = update_item(_database_url(), item_key, body)
    except RoadmapEditError as error:
        raise _edit_error(error) from error
    store().audit(
        "roadmap.item_updated",
        actor=session_actor(current),
        details={"key": item_key, **{k: v for k, v in item.items() if k != "key"}},
    )
    return {"item": item}


ROADMAP_PAGE_ID = "3ce6cbdff6f68086b8f7cc174bbb040d"


@admin_router.post("/refresh")
def refresh_route(request: Request) -> dict[str, Any]:
    """Read the Notion page again and write what changed.

    The owner's, not the company's: this reaches out to Notion and rewrites
    rows, which is a different act from reading the screen.
    """
    current = require_super_admin_session(request)
    _require_csrf(request, current)
    from datetime import datetime, timezone

    from .notion_client import NotionApiError, NotionClient
    from .roadmap_refresh import apply_refresh, fetch_table_rows, parse_table

    token_path = store().secret_path("notion_token")
    if not token_path.exists():
        raise HTTPException(
            status_code=503,
            detail="the Notion token is not configured: set it on the 연결 screen",
        )
    client = NotionClient(token_path.read_text(encoding="utf-8").strip())
    try:
        rows = fetch_table_rows(client, ROADMAP_PAGE_ID)
    except NotionApiError as error:
        # Said rather than raised as a 500: a Notion outage and a broken
        # parser are different problems with different fixes.
        raise HTTPException(status_code=502, detail=f"Notion refused: {error}") from error
    items = parse_table(rows)
    if not items:
        # Writing an empty refresh would delete the whole roadmap and record it
        # as ninety-five deliberate deletions.
        raise HTTPException(
            status_code=502,
            detail="the roadmap page returned no rows; nothing was changed",
        )

    taken = datetime.now(timezone.utc)
    result = apply_refresh(
        _database_url(),
        items,
        label=f"Notion {taken.date().isoformat()}",
        now=taken,
    )
    store().audit("roadmap.refreshed", actor=session_actor(current), details=result)
    return result
