"""Measure whether the last few days were actually collected.

The coverage grid has been able to answer this since it was written, but only
when somebody opened the page and looked. On 2026-09-05 the answer was that
three of five sources had gone uncollected for four days, and nothing had said
so: the batch that was supposed to run them named two sources, the code had
grown to five, and the difference was visible only to a reader of the grid.
This module is the reading, and it is meant to run on a timer. An hour is the
longest a source-day should ever be missing without anyone being told.

Everything here is a pure function over a coverage payload
(`collection_status.coverage`). It opens no archive, reads no clock and writes
nothing: the caller builds the grid and passes `now`, which is what makes the
whole judgement testable from a literal payload and lets the batch that runs it
be a few lines of shell.

Nothing here decides whether a gap is *acceptable*. A weekend with no Slack
traffic still has to be collected; a day nobody looked at is reported exactly
like a day that failed, because from the archive's side they are the same
absence.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from .collection_status import (
    COVERAGE_COLLECTED,
    COVERAGE_COLLECTED_WITH_SKIPS,
)

# The two verdicts that mean the day was observed. `collected_with_skips` is
# one of them on purpose: a run that finished and named what it could not reach
# has reported honestly, and honest reporting is not a gap. Every other verdict
# -- including `unknown`, `unverified` and `unexamined` -- is an absence of
# evidence, and this module does not treat absence of evidence as collection.
COLLECTED = (COVERAGE_COLLECTED, COVERAGE_COLLECTED_WITH_SKIPS)

# Finished KST days to check by default. Today is deliberately not one of them:
# it is not over, and a day still in progress is `partial` for a reason that is
# not a defect.
DEFAULT_DAYS = 3

# `next_action` is capped at 500 characters by the work store, and this line is
# written there. Truncating at the source keeps the queue from rejecting the
# audit's own report -- the same reason `board_audit.MAX_SUMMARY_CHARS` exists.
MAX_SUMMARY_CHARS = 500

# How many source-days the one-line summary names before it stops counting them
# out. Past a handful the list stops being a next action and becomes a table.
MAX_NAMED_GAPS = 6

KST = timezone(timedelta(hours=9))

_LABELS = {
    "not_collected": "미수집",
    "partial": "부분수집",
    "failed": "실패",
    "running": "진행중",
    "unknown": "불명",
    "unverified": "미검증",
    "unexamined": "미확인",
}


def audit(grid: Mapping[str, Any], *, now: datetime) -> dict[str, Any]:
    """Report every source-day in this grid that is not collected."""
    sources = [str(source) for source in grid.get("sources") or ()]
    rows = list(grid.get("rows") or ())
    gaps: list[dict[str, Any]] = []
    counts: dict[str, int] = {}

    for row in rows:
        date = str(row.get("date"))
        cells = row.get("cells") or {}
        for source in sources:
            cell = cells.get(source) or {}
            verdict = str(cell.get("coverage") or "unknown")
            if verdict in COLLECTED:
                continue
            counts[verdict] = counts.get(verdict, 0) + 1
            gaps.append(
                {
                    "date": date,
                    "source": source,
                    "coverage": verdict,
                    "runs": cell.get("runs"),
                    "last_status": cell.get("last_status"),
                    "last_run_id": cell.get("last_run_id"),
                    "time_coverage": cell.get("time_coverage"),
                    "evidence_class": cell.get("evidence_class"),
                }
            )

    examined = len(rows) * len(sources)
    return {
        "generated_at": now.astimezone(timezone.utc).isoformat(),
        "ok": not gaps,
        "start": grid.get("start"),
        "end": grid.get("end"),
        "days": len(rows),
        "sources": sources,
        "environment": grid.get("environment"),
        "environment_scope": grid.get("environment_scope"),
        "source_days_examined": examined,
        "source_days_missing": len(gaps),
        "counts": counts,
        "gaps": gaps,
        "summary": summarize(
            gaps, now=now, start=grid.get("start"), end=grid.get("end"), examined=examined
        ),
    }


def summarize(
    gaps: list[Mapping[str, Any]],
    *,
    now: datetime,
    start: Any,
    end: Any,
    examined: int,
) -> str:
    """One line, short enough for `next_action`.

    It names the missing source-days rather than only counting them, because a
    count tells a reader that something is wrong and the names tell them what
    to run.
    """
    kst = now.astimezone(KST)
    stamp = f"수집 감사 {kst:%m-%d %H:%M} KST · {start}~{end} {examined}칸"
    if not gaps:
        return f"{stamp} — 미수집 0"[:MAX_SUMMARY_CHARS]

    counts: dict[str, int] = {}
    for gap in gaps:
        verdict = str(gap.get("coverage"))
        counts[verdict] = counts.get(verdict, 0) + 1
    breached = " · ".join(
        f"{_LABELS.get(verdict, verdict)} {count}" for verdict, count in sorted(counts.items())
    )
    named = ", ".join(
        f"{gap['source']} {str(gap['date'])[5:]}" for gap in gaps[:MAX_NAMED_GAPS]
    )
    if len(gaps) > MAX_NAMED_GAPS:
        named += f" 외 {len(gaps) - MAX_NAMED_GAPS}"
    return f"{stamp} — {breached} · {named}"[:MAX_SUMMARY_CHARS]
