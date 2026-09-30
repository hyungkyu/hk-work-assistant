"""Put a missed collection day on the board, once.

The collection audit has measured missing source-days every night since
2026-09-06, and it can write its one-line summary onto a board item -- but only
onto an item somebody named in `collection-audit-target`, and only as that
item's `next_action`, which the next clean night overwrites. Nobody named one.
So the audit knew about every gap and told nobody, which is how the roadmap
page's 2026-09-06 edit fell into the 09/04, 09/05 and 09/07 holes unnoticed.

This turns a gap into its own board item. A new item, not a line on an old one:
a missed day is a decision somebody has to make -- re-run it, or accept it and
say why -- and a decision needs a place on the board that stays until it is
made. Each source-day is reported once. The audit looks back three days, so
without remembering what it already filed it would file the same hole three
nights running.

It files through the owner's outbox, like every other batch, so it holds no
store lock and leaves the same receipt as any queued edit. It never collects.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

# Keys remembered as already filed. Three audited days times five sources is
# fifteen a night; this is months of them, and old ones fall off the end.
MAX_REMEMBERED = 400

# The work store caps titles; a long list belongs in the detail.
MAX_TITLE_CHARS = 120


def gap_keys(report: Mapping[str, Any]) -> list[str]:
    """Each missing source-day as `source:date`, in the audit's order."""
    keys: list[str] = []
    for gap in report.get("gaps") or ():
        source = str(gap.get("source") or "").strip()
        date = str(gap.get("date") or "").strip()
        if source and date:
            keys.append(f"{source}:{date}")
    return keys


def new_gap_item(
    report: Mapping[str, Any], reported: Sequence[str]
) -> tuple[dict[str, Any] | None, list[str]]:
    """The outbox payload for gaps not filed before, and the keys it files.

    None when every gap in the report is already on the board, or there is
    none: a clean night files nothing.
    """
    already = set(reported)
    fresh = [key for key in gap_keys(report) if key not in already]
    if not fresh:
        return None, []
    named = ", ".join(f"{key.split(':', 1)[0]} {key.split(':', 1)[1][5:]}" for key in fresh)
    title = f"수집 공백: {named}"
    if len(title) > MAX_TITLE_CHARS:
        title = title[: MAX_TITLE_CHARS - 1] + "…"
    lines = [
        "수집 감사가 찾은, 아직 보드에 없던 미수집 칸입니다.",
        "",
        *(f"- {key}" for key in fresh),
        "",
        "다시 돌릴지(scripts/backfill-days.sh), 공백을 받아들이고 이유를 적을지 정해 주세요.",
        f"감사 요약: {report.get('summary') or ''}",
    ]
    digest = hashlib.sha256("\n".join(fresh).encode("utf-8")).hexdigest()[:12]
    payload = {
        "op": "create",
        "title": title,
        "detail": "\n".join(lines),
        "status": "ready",
        "priority": "high",
        # Whether to re-run a day or accept its loss is the owner's call, so
        # the item is theirs rather than a batch's.
        "assigned_to": "hk",
        "source_ref": f"collection-gap:{digest}",
        "next_action": "미수집 칸을 다시 수집하거나, 받아들이는 이유를 적는다",
    }
    return payload, fresh


def _read_reported(path: Path) -> list[str]:
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # A lost memory files some gaps twice. That is the cheap failure; the
        # expensive one is filing none.
        return []
    return [str(key) for key in stored] if isinstance(stored, list) else []


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    with os.fdopen(handle, "w", encoding="utf-8") as stream:
        stream.write(text)
    os.replace(temporary, path)


def file_gaps(
    report: Mapping[str, Any], *, state: Path, outbox: Path, now: datetime | None = None
) -> dict[str, Any]:
    """Queue one board item for the new gaps and remember them. Returns what it did."""
    reported = _read_reported(state)
    payload, fresh = new_gap_item(report, reported)
    if payload is None:
        return {"filed": False, "new_gaps": []}
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    target = outbox / f"{stamp}--collection-gap--{payload['source_ref'].split(':', 1)[1]}.json"
    _atomic_write(target, json.dumps(payload, ensure_ascii=False) + "\n")
    # Remembered only after the file is in the outbox: a crash in between
    # files the gap again tomorrow rather than never.
    remembered = (reported + fresh)[-MAX_REMEMBERED:]
    _atomic_write(state, json.dumps(remembered, ensure_ascii=False) + "\n")
    return {"filed": True, "new_gaps": fresh, "outbox_file": target.name}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--state", required=True, type=Path, help="JSON list of filed keys")
    parser.add_argument("--outbox", required=True, type=Path, help="The owner's outbox directory")
    args = parser.parse_args(argv)
    report = json.loads(sys.stdin.read())
    print(json.dumps(file_gaps(report, state=args.state, outbox=args.outbox), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
