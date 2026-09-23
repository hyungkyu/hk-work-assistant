"""The review queue, served to the backoffice.

HK, 2026-09-21: 결과적으로, 이 답변의 원 질문은 이거 일거 같다는 후보들이
있어서, 난 그걸 선택하는거지. 정확히는 네가 페어링을 한것을 가정하되, 나는
수정할 수 있게 하는거지.

So this screen shows one of his answers with the questions it might have been
answering, one of them already marked as the proposal, and he either agrees or
picks a different one. Two clicks per answer, and the correction is worth more
than the agreement: it is the only measurement of whether the ranking is
getting better.

Read-only except for that decision, like every other screen here. The
candidates are built by `worklog pairs --apply`; nothing on these routes
rebuilds them, because a screen that regenerates its own contents makes "what
did he actually see when he chose this" unanswerable.
"""

from __future__ import annotations

import os
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from .admin_web import _require_csrf, require_super_admin_session

router = APIRouter(prefix="/api/v1/admin/voice")


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
    return url


def _person_id(name: str) -> str:
    """The person whose answers these are, by name.

    A name rather than a uuid because that is what a person types. Failing
    loudly on an unknown name matters more than it looks: an empty queue and a
    misspelt name are different answers and must not look the same.
    """
    from . import digest

    match = digest.resolve_people(_database_url(), [name])
    resolved = match.get("resolved") or {}
    if not resolved:
        raise HTTPException(status_code=404, detail=f"찾지 못한 사람: {name}")
    return next(iter(resolved.values()))


@router.get("/pairs")
def pairs_route(
    request: Request,
    person_name: Annotated[str, Query(min_length=1)],
    state: Annotated[str, Query(pattern="^(undecided|decided|all)$")] = "undecided",
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    """His answers with their candidate questions, newest first."""
    require_super_admin_session(request)
    from .blocks import pair_queue

    return pair_queue(
        _database_url(),
        _person_id(person_name),
        state=state,
        limit=limit,
        offset=offset,
    )


@router.get("/agreement")
def agreement_route(
    request: Request,
    person_name: Annotated[str, Query(min_length=1)],
) -> dict[str, Any]:
    """How often the proposal was the one he chose.

    The number this whole screen exists to move. It is reported next to the
    queue rather than buried, because a proposal rate that is not improving
    means the reviewing is data entry.
    """
    require_super_admin_session(request)
    from .blocks import agreement

    return agreement(_database_url(), _person_id(person_name))


@router.get("/audit")
def audit_route(
    request: Request,
    person_name: Annotated[str, Query(min_length=1)],
    limit: Annotated[int, Query(ge=1, le=100)] = 10,
) -> dict[str, Any]:
    """Answers whose candidates do not carry exactly one proposal.

    A queue row with nothing marked as the proposal is a row he has to review
    from scratch, so the count belongs on the screen rather than in a command
    only I run.
    """
    require_super_admin_session(request)
    from .blocks import audit_pairs

    return audit_pairs(_database_url(), _person_id(person_name), limit=limit)


class ChooseRequest(BaseModel):
    answer_ledger_id: str = Field(min_length=1)
    # None means "none of these was the question", which is a real answer and
    # not a refusal to answer: it says the candidates were all wrong, which is
    # exactly what the ranking needs to hear.
    pair_id: str | None = None


@router.post("/pairs/choose")
def choose_route(request: Request, payload: ChooseRequest) -> dict[str, Any]:
    """Which candidate was the question -- or that none of them was."""
    current = require_super_admin_session(request)
    _require_csrf(request, current)
    from .blocks import choose_pair

    try:
        changed = choose_pair(
            _database_url(),
            payload.answer_ledger_id,
            payload.pair_id,
            actor=str(current.get("email") or "backoffice"),
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    if not changed:
        raise HTTPException(status_code=404, detail="해당 답변을 찾지 못했습니다")
    return {"ok": True, "answer_ledger_id": payload.answer_ledger_id}
