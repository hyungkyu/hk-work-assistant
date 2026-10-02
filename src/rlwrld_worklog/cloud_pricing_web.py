"""The cloud pricing screen, and the button that goes and looks again.

The owner's screen, not the company's: what we pay per GPU-hour is a
negotiating position, so both reading and refreshing ask for a super-admin
session. That is the difference between this and `roadmap_web`, where reading
is open and only editing is not.

A refresh reaches out to five providers and a rate source over the public
internet, so it is a POST with CSRF behind it, and it is audited. Pressing it
twice in a row is harmless by design -- the second press finds nothing changed
and moves the date rather than writing a second identical rate card.
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from .admin_web import (
    _require_csrf,
    require_page_access,
    require_super_admin_session,
    session_actor,
    store,
)

admin_router = APIRouter(prefix="/api/v1/admin/cloud-pricing")

# The tables this screen needs, and the one command that creates them. Named
# rather than left to become a 500: "you have not run the migration" and "the
# code is broken" are not the same problem and do not have the same fix.
_MISSING_TABLES = (
    "the cloud pricing tables are missing: apply sql/migrations/0015_cloud_pricing.sql"
)


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
    return url


def _guarded(call):
    """Run a database read, and name the two ways a fresh install fails."""
    import psycopg

    try:
        return call()
    except psycopg.errors.UndefinedTable as error:
        raise HTTPException(status_code=503, detail=_MISSING_TABLES) from error
    except psycopg.OperationalError as error:
        raise HTTPException(
            status_code=503, detail="the database is not reachable from the application"
        ) from error


@admin_router.get("")
def cloud_pricing_route(request: Request) -> dict[str, Any]:
    """The current rate card, its exchange rate, and the list of older ones."""
    require_page_access("gpu")(request)
    from .cloud_pricing import read_current

    return _guarded(lambda: read_current(_database_url()))


@admin_router.get("/snapshots/{snapshot_id}")
def snapshot_route(snapshot_id: int, request: Request) -> dict[str, Any]:
    """One superseded rate card, with the rate that was on screen at the time.

    Not today's rate applied to yesterday's prices: the conversion is part of
    what was read, and re-converting it later would quietly rewrite history.
    """
    require_page_access("gpu")(request)
    from .cloud_pricing import read_snapshot

    try:
        return _guarded(lambda: read_snapshot(_database_url(), snapshot_id))
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@admin_router.post("/refresh")
def refresh_route(request: Request) -> dict[str, Any]:
    """Go and read all five rate cards again, and the exchange rate with them.

    What comes back decides whether this becomes a new snapshot or just a new
    date on the one already there -- see `cloud_pricing.apply_refresh`. Either
    way the answer says which providers answered, so a page that has been
    redesigned shows up as a named failure instead of a shorter table.
    """
    current = require_super_admin_session(request)
    _require_csrf(request, current)

    from .cloud_pricing import apply_refresh, previous_versions
    from .cloud_pricing_sources import SourceError, fetch_all, fetch_fx

    url = _database_url()
    versions = _guarded(lambda: previous_versions(url))
    results = fetch_all(known_versions=versions)

    # The rate is fetched separately from the prices and is allowed to fail on
    # its own: a rate card with no conversion beside it is still worth showing,
    # and is a great deal more honest than one converted at a guess.
    fx = None
    fx_error: str | None = None
    try:
        fx = fetch_fx()
    except SourceError as error:
        fx_error = str(error)

    if not any(result.ok for result in results):
        # Every provider failed. Writing this would record five deletions and
        # then carry every row forward, which is a lot of ceremony for "the
        # network is down" -- and it would move the date on a screen that
        # learnt nothing.
        raise HTTPException(
            status_code=502,
            detail="어느 공급자에게서도 요금을 받지 못했습니다: "
            + "; ".join(f"{r.provider} {r.detail}" for r in results),
        )

    result = _guarded(lambda: apply_refresh(url, results, fx))
    result["fx_error"] = fx_error
    store().audit(
        "cloud_pricing.refreshed",
        actor=session_actor(current),
        details={
            "changed": result["changed"],
            "snapshot_id": result["snapshot_id"],
            "added": result["added"],
            "updated": result["updated"],
            "removed": result["removed"],
            "runs": result["runs"],
        },
    )
    return result
