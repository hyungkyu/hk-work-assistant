"""Authenticated, read-only JSON API behind the 수집 현황 backoffice page.

Every route requires a super-administrator session, exactly like the settings
and delegated-work APIs, and every route is a GET: this page reports on
external evidence and never changes it.

No route accepts a path. ``source`` is matched against the three collector
names, ``environment`` against a single safe path segment that must also be
one the archive actually contains, and dates against ``YYYY-MM-DD``. The
reader itself then re-checks that every resolved path stays inside
``RAW_ARCHIVE_ROOT`` or ``APP_CONFIG_ROOT``, so a hostile directory name on
disk cannot walk a request out of those roots either.
"""

from __future__ import annotations

from datetime import date as date_type, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request

from . import collection_status
from .admin_web import _require_csrf, require_super_admin_session
from .collection_progress import is_safe_name
from .collection_rules import COLLECTOR_SOURCES, SOURCES, registry_as_dict

router = APIRouter(prefix="/api/v1/admin/collection")

_SOURCE_PATTERN = "^(slack|notion|google-calendar)$"
_LEDGER_SOURCE_PATTERN = "^(slack|notion|google_calendar)$"
_ENVIRONMENT_PATTERN = "^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$"  # `all` is a valid segment and is the widening sentinel
_DATE_PATTERN = "^\\d{4}-\\d{2}-\\d{2}$"

DEFAULT_COVERAGE_DAYS = 30


def _database_url() -> str:
    import os

    url = os.environ.get("DATABASE_URL")
    if not url:
        # A 503 rather than a 500: nothing is broken, the service simply has no
        # database configured, and the page can say so instead of showing an
        # empty result that looks like "nothing matched".
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
    return url


@router.get("/search")
def search(
    request: Request,
    q: Annotated[str, Query(min_length=1, max_length=200)],
    matcher: Annotated[str, Query(pattern="^(auto|words|substring)$")] = "auto",
    source: Annotated[list[str] | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 25,
) -> dict[str, Any]:
    """Search the collected text. Read-only, like every route on this page."""
    from .search import search_text

    require_super_admin_session(request)
    sources = [item for item in (source or []) if item in set(SOURCES)]
    try:
        result = search_text(
            _database_url(), q, matcher=matcher, sources=sources, limit=limit
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return result.as_dict()


@router.get("/search/status")
def search_corpus_status(request: Request) -> dict[str, Any]:
    """What the corpus holds, so an empty result is not read as an empty index."""
    from .search import search_status

    require_super_admin_session(request)
    return search_status(_database_url())


def _paths() -> collection_status.CollectionPaths:
    return collection_status.paths_from_environment()


# What an unqualified request means. A test capture must never fill in a gap
# in the production picture: someone reads this board to decide whether real
# data was collected, and a smoke run answering that question is a lie.
DEFAULT_ENVIRONMENT = "production"

# The one value that deliberately means "every environment", so the test view
# stays reachable without making it the default.
ALL_ENVIRONMENTS = "all"


def _environment(value: str | None) -> str | None:
    """The environment to report on. Defaults to production, never to all.

    `None` and an empty value both mean "unqualified", which resolves to
    production. Only the explicit sentinel `all` widens the view, and it
    returns `None` because that is what the reader treats as unfiltered.
    """
    if value is None or value == "":
        return DEFAULT_ENVIRONMENT
    if value == ALL_ENVIRONMENTS:
        return None
    if not is_safe_name(value):
        raise HTTPException(status_code=400, detail="environment name is not allowed")
    return value


def _date(value: str | None, *, field: str) -> date_type | None:
    if value is None or value == "":
        return None
    parsed = collection_status.parse_iso_date(value)
    if parsed is None:
        raise HTTPException(status_code=400, detail=f"{field} must be a YYYY-MM-DD date")
    return parsed


@router.get("/rules")
def collection_rules(request: Request) -> dict[str, Any]:
    """The append-only rule registry, with each version's content digest."""
    require_super_admin_session(request)
    return registry_as_dict()


@router.get("/overview")
def collection_overview(
    request: Request,
    environment: Annotated[str | None, Query(pattern=_ENVIRONMENT_PATTERN)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 20,
) -> dict[str, Any]:
    require_super_admin_session(request)
    return collection_status.overview(
        _paths(), environment=_environment(environment), limit=limit
    )


@router.get("/runs")
def collection_runs(
    request: Request,
    source: Annotated[str | None, Query(pattern=_SOURCE_PATTERN)] = None,
    environment: Annotated[str | None, Query(pattern=_ENVIRONMENT_PATTERN)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> dict[str, Any]:
    require_super_admin_session(request)
    if source is not None and source not in COLLECTOR_SOURCES:
        raise HTTPException(status_code=400, detail="unsupported source")
    return collection_status.list_runs(
        _paths(), source=source, environment=_environment(environment), limit=limit
    )


@router.get("/batch-runs")
def collection_batch_runs(request: Request) -> dict[str, Any]:
    """Every scheduled batch's last run, read from the logs the runs write.

    The manifests say what a collection found; they say nothing about whether
    the batch that should have produced one ever ran. This is the other half:
    run-logged.sh's last.json and running.json per batch, including runs in
    progress, stalls, and batches that have never run at all.
    """
    require_super_admin_session(request)
    from .batch_runs import read_batch_runs

    return read_batch_runs()


@router.post("/refresh")
def collection_refresh(
    request: Request,
    screen: Annotated[str, Query(pattern="^(overview|runs|coverage|all)$")] = "all",
) -> dict[str, Any]:
    """Drop the derived caches one screen depends on and report what was dropped.

    Everything behind this endpoint is derived and rebuildable, so this only
    forces the next read to go back to disk. It is a POST because it changes
    server state (the caches), and it carries CSRF like every other mutation.
    """
    current = require_super_admin_session(request)
    _require_csrf(request, current)
    names = None if screen == "all" else collection_status.CACHE_GROUPS[screen]
    dropped = collection_status.clear_caches(names)
    return {"screen": screen, "caches_cleared": sorted(dropped)}


@router.get("/coverage")
def collection_coverage(
    request: Request,
    start: Annotated[str | None, Query(pattern=_DATE_PATTERN)] = None,
    end: Annotated[str | None, Query(pattern=_DATE_PATTERN)] = None,
    source: Annotated[list[str] | None, Query(pattern=_LEDGER_SOURCE_PATTERN)] = None,
    environment: Annotated[str | None, Query(pattern=_ENVIRONMENT_PATTERN)] = None,
    group: Annotated[str, Query(pattern="^(date|weekday)$")] = "date",
) -> dict[str, Any]:
    """Coverage by KST date or by KST weekday, per source."""
    require_super_admin_session(request)
    requested = [name for name in (source or []) if name in SOURCES] or list(SOURCES)
    today = datetime.now(collection_status.KST).date()
    end_date = _date(end, field="end") or today
    start_date = _date(start, field="start") or end_date - timedelta(
        days=DEFAULT_COVERAGE_DAYS - 1
    )
    return collection_status.coverage(
        _paths(),
        start=start_date,
        end=end_date,
        sources=requested,
        environment=_environment(environment),
        group=group,
    )
