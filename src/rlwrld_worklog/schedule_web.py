"""Read-only schedule API for the backoffice 스케줄 page.

Super-administrator only, GET only, and it takes no parameters at all: the
units it may look at come from the catalogue in ``schedules``, never from the
request, so there is no unit name or path for a caller to supply.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from . import schedules
from .admin_web import require_super_admin_session, store

router = APIRouter(prefix="/api/v1/admin/schedules")


@router.get("")
def list_schedules(request: Request) -> dict[str, Any]:
    require_super_admin_session(request)
    return schedules.describe(store().load_settings())
