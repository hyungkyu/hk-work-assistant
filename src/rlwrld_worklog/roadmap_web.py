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

from .admin_web import require_company_session

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
