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
from .admin_web import require_super_admin_session
from .collection_progress import is_safe_name
from .collection_rules import COLLECTOR_SOURCES, SOURCES, registry_as_dict

router = APIRouter(prefix="/api/v1/admin/collection")

_SOURCE_PATTERN = "^(slack|notion|google-calendar)$"
_LEDGER_SOURCE_PATTERN = "^(slack|notion|google_calendar)$"
_ENVIRONMENT_PATTERN = "^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$"
_DATE_PATTERN = "^\\d{4}-\\d{2}-\\d{2}$"

DEFAULT_COVERAGE_DAYS = 30


def _paths() -> collection_status.CollectionPaths:
    return collection_status.paths_from_environment()


def _environment(value: str | None) -> str | None:
    """A single safe path segment, or a 400. Never a path."""
    if value is None or value == "":
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
