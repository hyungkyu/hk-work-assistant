"""Measure whether the board still matches the work.

P0 was "실제로 하고 있는 일과 백오피스 업무 현황을 일치시키는 것". It was reached
once by hand on 2026-09-05 and had drifted again within hours, because nothing
was watching. A property that is checked once is not a property; this module is
the check, and it is meant to run on a timer.

Everything here is a pure function over the board document and its history. It
reads no clock of its own, opens no file, and writes nothing: the caller passes
`now`, and the caller decides what to do with the findings. That is what makes
it testable without a store, and it is why the batch that runs it can be four
lines of shell.

Nothing here is a judgement about whether an item is *right*. Each check
measures one thing the board can be wrong about in a way a machine can see.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence

from .collection_status import parse_instant

# Statuses that mean the item is still somebody's problem. `done` and
# `cancelled` have left the queue and are not audited: an item can sit in
# either one indefinitely without the board being wrong about anything.
LIVE_STATUSES = ("in_progress", "ready", "todo", "backlog", "waiting", "blocked")

DEFAULT_STALLED_AFTER_HOURS = 6
DEFAULT_STALE_READY_AFTER_HOURS = 24
MAX_SUMMARY_LINES = 3


def _hours_since(moment: datetime | None, now: datetime) -> float | None:
    if moment is None:
        return None
    return round((now - moment).total_seconds() / 3600.0, 1)


def _last_touch_by(
    history: Iterable[Mapping[str, Any]], item_id: str, actor: str
) -> datetime | None:
    """When the named actor last changed this item.

    The assignee's own edits are the only evidence in the board that the work
    is moving. The requester editing the ticket is not evidence of that, which
    is exactly the confusion that let an unworked item read `in_progress` for
    most of a day.
    """
    latest: datetime | None = None
    for entry in history:
        if entry.get("item_id") != item_id:
            continue
        if str(entry.get("actor") or "") != actor:
            continue
        at = parse_instant(entry.get("at") or entry.get("recorded_at"))
        if at is not None and (latest is None or at > latest):
            latest = at
    return latest


def _summary_lines(item: Mapping[str, Any]) -> int:
    text = str(item.get("progress_summary") or "")
    return len([line for line in text.splitlines() if line.strip()])


def audit(
    items: Sequence[Mapping[str, Any]],
    history: Sequence[Mapping[str, Any]],
    *,
    now: datetime,
    roster: Sequence[str] = (),
    stalled_after_hours: float = DEFAULT_STALLED_AFTER_HOURS,
    stale_ready_after_hours: float = DEFAULT_STALE_READY_AFTER_HOURS,
) -> dict[str, Any]:
    """Report every way this board currently disagrees with the work."""
    live = [
        item
        for item in items
        if not item.get("archived_at") and str(item.get("status")) in LIVE_STATUSES
    ]
    stalled_cut = now - timedelta(hours=stalled_after_hours)
    ready_cut = now - timedelta(hours=stale_ready_after_hours)

    def _row(item: Mapping[str, Any], **extra: Any) -> dict[str, Any]:
        return {
            "id": item.get("id"),
            "assigned_to": item.get("assigned_to"),
            "title": str(item.get("title") or "")[:80],
            **extra,
        }

    # P0 condition 3. An item claiming to be underway whose own assignee has
    # not touched it. `updated_at` is deliberately not used: the requester
    # editing the ticket would refresh it and hide the very thing being looked
    # for.
    unworked = []
    for item in live:
        if str(item.get("status")) != "in_progress":
            continue
        assignee = str(item.get("assigned_to") or "")
        touched = _last_touch_by(history, str(item.get("id")), assignee) if assignee else None
        if touched is None or touched < stalled_cut:
            unworked.append(
                _row(item, hours_since_assignee_touched=_hours_since(touched, now))
            )

    # An open item nobody can act on. Not a rule about writing style: an empty
    # `next_action` on a live item means the board names no next step, and the
    # queue is the only place that step would have been recorded.
    without_next = [
        _row(item, status=item.get("status"))
        for item in live
        if not str(item.get("next_action") or "").strip()
    ]

    # P0 condition 6.
    long_summary = [
        _row(item, lines=_summary_lines(item))
        for item in live
        if _summary_lines(item) > MAX_SUMMARY_LINES
    ]

    # Work queued for someone who is not there. Only checked when the caller
    # names the roster, because this module has no way to know who exists.
    known = {name for name in roster if name}
    outside_roster = (
        [
            _row(item, status=item.get("status"))
            for item in live
            if str(item.get("assigned_to") or "") not in known
        ]
        if known
        else []
    )

    # `ready` means "someone could start this now". A ready item nobody has
    # touched for a day is either not ready or has no executor.
    stale_ready = []
    for item in live:
        if str(item.get("status")) != "ready":
            continue
        updated = parse_instant(item.get("updated_at"))
        if updated is not None and updated < ready_cut:
            stale_ready.append(_row(item, hours_since_change=_hours_since(updated, now)))

    checks = {
        "in_progress_without_assignee_activity": unworked,
        "live_without_next_action": without_next,
        "progress_summary_over_three_lines": long_summary,
        "assigned_outside_roster": outside_roster,
        "ready_untouched": stale_ready,
    }
    counts = {name: len(rows) for name, rows in checks.items()}
    total = sum(counts.values())
    return {
        "generated_at": now.astimezone(timezone.utc).isoformat(),
        "ok": total == 0,
        "live_items": len(live),
        "counts": counts,
        "checks": checks,
        "summary": summarize(counts, now=now, live_items=len(live)),
    }


_LABELS = {
    "in_progress_without_assignee_activity": "실행 없는 in_progress",
    "live_without_next_action": "next_action 없음",
    "progress_summary_over_three_lines": "요약 3줄 초과",
    "assigned_outside_roster": "실행자 미상",
    "ready_untouched": "방치된 ready",
}

# `next_action` is capped at 500 characters by the store, and this line is
# written there. Truncating at the source keeps the queue from rejecting the
# audit's own report, which happened to a dispatch on 2026-09-04.
MAX_SUMMARY_CHARS = 500


def summarize(counts: Mapping[str, int], *, now: datetime, live_items: int) -> str:
    """One line, short enough for `next_action`."""
    kst = now.astimezone(timezone(timedelta(hours=9)))
    stamp = f"보드 감사 {kst:%m-%d %H:%M} KST · 열린 항목 {live_items}"
    breached = [
        f"{_LABELS.get(name, name)} {count}" for name, count in counts.items() if count
    ]
    line = f"{stamp} — " + (" · ".join(breached) if breached else "위반 0")
    return line[:MAX_SUMMARY_CHARS]
