"""Trim `worklog work board` into a list somebody can check the work against.

Reads the board JSON on stdin, writes the snapshot on stdout.

Why this exists: the audit tick already reports how many items disagree with
the work, and does not say what they are. A reviewer who can only see the
count has to take it on faith. This is the list -- ids, titles, phase, status,
holder, next action -- written by the same batch at the same moment as the
audit, so the two files are always of one instant.

Trimmed on purpose. Descriptions and history stay in the store; this is a
list to check against, not a copy of the board.

A file rather than a heredoc in the shell script because the first version was
a heredoc, it failed on the host, the `|| true` swallowed the reason, and what
landed was a zero-byte file that said nothing. Code that can fail belongs
somewhere it can be tested.
"""

from __future__ import annotations

import json
import sys
from typing import Any, Iterator

KEEP = (
    "id",
    "title",
    "phase",
    "status",
    "assigned_to",
    "next_action",
    "updated_at",
    "revision",
)

# Sorts last, after every real phase. An item nobody has placed reads at the
# bottom of the list rather than mixed in among the placed ones.
UNPLACED = "ZZ"


def walk(value: Any) -> Iterator[dict]:
    """Every item object anywhere in the board's column structure.

    Structural rather than positional: the board nests items under columns
    today and may nest them differently tomorrow, and a snapshot that silently
    returned nothing after a layout change would be the same silence this
    module was written to remove.
    """
    if isinstance(value, dict):
        if "id" in value and "status" in value:
            yield value
            return
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def snapshot(board: Any, *, started_at: str = "") -> dict[str, Any]:
    items: list[dict] = []
    seen: set[str] = set()
    for item in walk(board):
        if item["id"] in seen:
            continue
        seen.add(item["id"])
        items.append({key: item.get(key) for key in KEEP})
    items.sort(
        key=lambda item: (
            str(item.get("phase") or UNPLACED),
            str(item.get("status")),
            str(item.get("title")),
        )
    )
    return {
        "started_at": started_at,
        "outcome": "snapshot",
        "items": len(items),
        "board": items,
    }


def main() -> int:
    import os

    board = json.load(sys.stdin)
    payload = snapshot(board, started_at=os.environ.get("STARTED", ""))
    json.dump(payload, sys.stdout, ensure_ascii=False, sort_keys=True, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
