"""Super-admin JSON API for the delegated-work board.

Authentication, CSRF, and audit follow the same patterns as the settings API in
``admin_web``: a super-administrator session is required, every mutation carries
the session CSRF token, and every mutation appends a record to the admin audit
log alongside the work store's own change history.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Any, Iterator, Mapping

from contextlib import contextmanager

from fastapi import APIRouter, HTTPException, Query, Request

from .admin_web import (
    _require_csrf,
    agent_name,
    require_board_session,
    require_super_admin_session,
    session_actor,
    store,
)
from .cowork import DIRECTING_PARTIES, registry_as_dict
from .work_store import (
    PHASES,
    PRIORITIES,
    describe_roles,
    STATUSES,
    status_metadata,
    WorkConflictError,
    WorkCorruptionError,
    WorkLockTimeout,
    WorkNotFoundError,
    WorkStore,
    WorkValidationError,
)


router = APIRouter(prefix="/api/v1/admin/work")


@lru_cache(maxsize=1)
def work_store() -> WorkStore:
    return WorkStore.from_environment()


@contextmanager
def _translated_errors() -> Iterator[None]:
    """Map store failures onto explicit, actionable HTTP statuses."""
    try:
        yield
    except WorkValidationError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except WorkNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except WorkConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except WorkLockTimeout as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except WorkCorruptionError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


async def _json_object(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception as error:
        raise HTTPException(status_code=400, detail="request body must be JSON") from error
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="request body must be an object")
    return body


def _actor(current: Mapping[str, Any]) -> str:
    # One reading of "who is acting", shared with the admin routes. Two
    # readings would drift, and this one is written into history where a
    # wrong name is not correctable afterwards.
    return session_actor(current)


def _require_may_write(current: Mapping[str, Any], item: Mapping[str, Any]) -> None:
    """Writing someone else's item is directing them, and that is a role.

    Rule 8 says an executor does not hand work to another executor; the same
    sentence expressed as a permission. A person, and an agent standing in for a
    judgement role, may write any item. Everyone else may write their own, which
    is what recording your own progress means.
    """
    name = agent_name(current)
    if name is None or name in DIRECTING_PARTIES:
        return
    if str(item.get("assigned_to") or "") != name:
        raise HTTPException(
            status_code=403,
            detail="an agent may write its own items; issuing work to another is a judgement role",
        )


def _concurrency(body: Mapping[str, Any]) -> dict[str, Any]:
    expected_revision = body.get("expected_revision")
    if expected_revision is not None and (
        not isinstance(expected_revision, int) or isinstance(expected_revision, bool)
    ):
        raise HTTPException(status_code=400, detail="expected_revision must be an integer")
    expected_updated_at = body.get("expected_updated_at")
    if expected_updated_at is not None and not isinstance(expected_updated_at, str):
        raise HTTPException(status_code=400, detail="expected_updated_at must be a string")
    return {
        "expected_revision": expected_revision,
        "expected_updated_at": expected_updated_at,
    }


def _audit(action: str, *, actor: str, item: Mapping[str, Any]) -> None:
    """Record the fact of the change; never the item's free text."""
    store().audit(
        action,
        actor=actor,
        details={
            "item_id": item["id"],
            "revision": item["revision"],
            "status": item["status"],
            "assigned_to": item["assigned_to"],
        },
    )


@router.get("/meta")
def work_meta(request: Request) -> dict[str, Any]:
    """The status schema and the board layout derived from it.

    `statuses` and `priorities` keep their original shape and meaning, so a
    client written against the earlier response keeps working; everything the
    four-stage board needs is added beside them.
    """
    require_board_session(request)
    payload = status_metadata()
    payload["statuses"] = list(STATUSES)
    payload["priorities"] = list(PRIORITIES)
    payload["phases"] = list(PHASES)
    # Who may direct whom, and how a historical actor name resolves. Additive:
    # a client written against the earlier response is unaffected.
    payload["cowork"] = registry_as_dict()
    return payload


@router.get("/items")
def list_items(
    request: Request,
    include_archived: bool = False,
    status: Annotated[list[str] | None, Query()] = None,
    assigned_to: str | None = None,
    limit: Annotated[int, Query(ge=1, le=2_000)] = 500,
) -> dict[str, Any]:
    require_board_session(request)
    with _translated_errors():
        payload = work_store().list_items(
            include_archived=include_archived,
            statuses=status,
            assigned_to=assigned_to,
        )
    payload["items"] = payload["items"][:limit]
    return payload


@router.get("/items/{item_id}")
def get_item(item_id: str, request: Request) -> dict[str, Any]:
    require_board_session(request)
    with _translated_errors():
        store = work_store()
        item = store.get_item(item_id)
        # Beside the item, never inside it: who directed, carried and checks
        # this, and where it stands. All four are read off what is already
        # there, so none of them can contradict it.
        return {"item": item, "roles": describe_roles(item, store.list_items()["items"])}


@router.post("/items", status_code=201)
async def create_item(request: Request) -> dict[str, Any]:
    current = require_board_session(request)
    _require_csrf(request, current)
    body = await _json_object(request)
    actor = _actor(current)
    fields = dict(body.get("fields") if isinstance(body.get("fields"), dict) else body)
    fields.pop("expected_revision", None)
    fields.pop("expected_updated_at", None)
    fields.setdefault("requested_by", actor)
    with _translated_errors():
        item = work_store().create_item(fields, actor=actor)
    _audit("work.created", actor=actor, item=item)
    return {"item": item}


@router.patch("/items/{item_id}")
async def update_item(item_id: str, request: Request) -> dict[str, Any]:
    current = require_board_session(request)
    _require_csrf(request, current)
    body = await _json_object(request)
    expectations = _concurrency(body)
    actor = _actor(current)
    fields = dict(body.get("fields") if isinstance(body.get("fields"), dict) else body)
    fields.pop("expected_revision", None)
    fields.pop("expected_updated_at", None)
    with _translated_errors():
        _require_may_write(current, work_store().get_item(item_id))
        item = work_store().update_item(item_id, fields, actor=actor, **expectations)
    _audit("work.updated", actor=actor, item=item)
    return {"item": item}


@router.post("/items/{item_id}/archive")
async def archive_item(item_id: str, request: Request) -> dict[str, Any]:
    current = require_super_admin_session(request)
    _require_csrf(request, current)
    body = await _json_object(request)
    expectations = _concurrency(body)
    actor = _actor(current)
    with _translated_errors():
        item = work_store().archive_item(item_id, actor=actor, **expectations)
    _audit("work.archived", actor=actor, item=item)
    return {"item": item}


@router.get("/items/{item_id}/timeline")
def work_timeline(
    item_id: str,
    request: Request,
    limit: Annotated[int, Query(ge=1, le=1_000)] = 200,
) -> dict[str, Any]:
    """One item's activity: who directed it, who acted, and where the receipts are.

    ``item_id`` is looked up in the work document and is never joined onto a
    path, so it cannot address a file. Entries written before the timeline
    fields existed come back marked ``legacy`` with their unknown fields
    named, rather than back-filled with a guess.
    """
    require_board_session(request)
    with _translated_errors():
        return work_store().read_timeline(item_id, limit=limit)


@router.get("/history")
def work_history(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    item_id: str | None = None,
) -> dict[str, Any]:
    require_board_session(request)
    with _translated_errors():
        return {"items": work_store().read_history(limit=limit, item_id=item_id)}
