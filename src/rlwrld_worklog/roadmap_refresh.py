"""Bringing the roadmap up to date from the Notion page it came from.

Three things this has to get right, and they are the reasons it is not a
re-import:

**It must not undo anybody.** Product, kind and horizon can be corrected by
hand on the mapping screen, and each correction raises an `*_override` flag.
A refresh writes what comes from the source -- the Korean text, the team, the
link -- and leaves every flagged column alone.

**It must know what stayed the same.** Rows are matched by key first and by
content hash second. The second pass is what makes the first refresh after the
initial import a quiet one: those rows were keyed by ordinal and are about to
be keyed by Notion block, and without hash matching every one of them would
read as a deletion followed by an addition.

**It must not let a translation lie.** The page is Korean. When a row's Korean
changes, the English and Japanese beside it are stale, and a screen with a
language toggle has no way to say so unless something records it. That is what
`translated_hash` is for.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from .roadmap import OFFICIAL_SOURCE, text_hash

# The team column of the snapshot table, as the page spells it, against the ids
# the database uses. A name here that the page stops using shows up as a team
# whose rows all disappear, which is loud -- and that is the point of keeping
# the mapping explicit rather than slugifying whatever is in the cell.
TEAM_BY_LABEL = {
    "Robotics Platform": "rp",
    "Human Data": "hd",
    "Infra": "infra",
    "Infra & Data": "infra",
    "LOOP": "loop",
    "HW": "hw",
}

# The three columns after the team name.
COLUMN_HORIZON = ("now", "next", "long")

BULLET = re.compile(r"^[\s ]*([•◦▪·・-])\s*")
TOP_LEVEL = "•"


@dataclass
class ParsedItem:
    key: str
    team: str
    column: str
    text_ko: str
    block_id: str
    hash: str = ""

    def __post_init__(self) -> None:
        if not self.hash:
            self.hash = text_hash(self.text_ko)


def _plain(rich_text: Iterable[Mapping[str, Any]]) -> str:
    return "".join(str(run.get("plain_text") or "") for run in rich_text)


def split_cell(text: str) -> list[list[str]]:
    """One cell into top-level bullets, each with its sub-bullets.

    The page nests one level: `• thing` with `◦ detail` under it. A detail is
    not its own roadmap entry -- it qualifies the entry above it -- so it is
    folded into that entry rather than counted as one more.
    """
    groups: list[list[str]] = []
    for raw in text.replace("\r", "").split("\n"):
        line = raw.strip()
        if not line:
            continue
        marker = BULLET.match(raw)
        body = BULLET.sub("", raw).strip() if marker else line
        if not body:
            continue
        indented = len(raw) - len(raw.lstrip("  \t")) > 0
        is_child = bool(marker) and (marker.group(1) != TOP_LEVEL or indented)
        if groups and is_child:
            groups[-1].append(body)
        else:
            groups.append([body])
    return groups


def fold(group: Sequence[str]) -> str:
    """A bullet and its sub-bullets as one line, the way the seed reads."""
    head, *rest = group
    if not rest:
        return head
    return f"{head} — " + ", ".join(rest)


def parse_table(rows: Sequence[Mapping[str, Any]]) -> list[ParsedItem]:
    """The snapshot table into roadmap rows.

    `rows` is the Notion `table_row` blocks in order, header first.
    """
    items: list[ParsedItem] = []
    for row in rows[1:]:
        block_id = str(row.get("id") or "")
        cells = row.get("table_row", {}).get("cells") or []
        if not cells:
            continue
        label = _plain(cells[0]).strip()
        team = TEAM_BY_LABEL.get(label)
        if team is None:
            # Not a team row (a spacer, or a name nobody told us about). Skipped
            # rather than guessed at: a wrong team is worse than a missing one.
            continue
        for index, column in enumerate(COLUMN_HORIZON, start=1):
            if index >= len(cells):
                continue
            for position, group in enumerate(split_cell(_plain(cells[index]))):
                text = fold(group)
                if not text:
                    continue
                items.append(
                    ParsedItem(
                        key=f"{block_id}:{column}:{position}",
                        team=team,
                        column=column,
                        text_ko=text,
                        block_id=block_id,
                    )
                )
    return items


# The source's third column is one bucket; the screen splits it in two. A
# quarter written in the text is what tells them apart, and anything with no
# stated time is "someday" rather than a guess.
QUARTER = re.compile(r"\((?:[1-4]Q|Q[1-4])\)|[1-4]Q\b")
UNDATED = ("일정이 아직 미정", "일정 미정", "미정이라")


def horizon_for(item: ParsedItem) -> str:
    if any(mark in item.text_ko for mark in UNDATED):
        return "someday"
    if item.column != "long":
        return item.column
    return "soon" if QUARTER.search(item.text_ko) else "someday"


@dataclass
class Change:
    type: str
    key: str
    team: str
    before: str = ""
    after: str = ""


@dataclass
class Diff:
    added: list[ParsedItem] = field(default_factory=list)
    changed: list[tuple[str, ParsedItem]] = field(default_factory=list)
    removed: list[Mapping[str, Any]] = field(default_factory=list)
    carried: list[tuple[str, ParsedItem]] = field(default_factory=list)

    def as_changes(self) -> list[Change]:
        out = [
            Change("added", item.key, item.team, after=item.text_ko) for item in self.added
        ]
        out += [
            Change("changed", key, item.team, after=item.text_ko) for key, item in self.changed
        ]
        out += [
            Change("removed", str(row["item_key"]), str(row["team_id"]), before=str(row["text_ko"]))
            for row in self.removed
        ]
        return out


def diff_items(incoming: Sequence[ParsedItem], existing: Sequence[Mapping[str, Any]]) -> Diff:
    """What changed, matching on key first and on content second.

    The content pass is not a nicety. The rows seeded by the first import are
    keyed by ordinal and every refresh keys them by Notion block, so without it
    the first refresh would report ninety-five deletions and ninety-five
    additions and lose every override with them.
    """
    diff = Diff()
    by_key = {str(row["item_key"]): row for row in existing}
    unmatched = dict(by_key)

    by_hash: dict[str, list[Mapping[str, Any]]] = {}
    for row in existing:
        by_hash.setdefault(str(row["hash"]), []).append(row)

    for item in incoming:
        row = unmatched.pop(item.key, None)
        if row is not None:
            if str(row["hash"]) != item.hash:
                diff.changed.append((item.key, item))
            continue
        # Same words, different key: the row moved rather than being replaced.
        candidates = [r for r in by_hash.get(item.hash, []) if str(r["item_key"]) in unmatched]
        if candidates:
            carried = candidates[0]
            unmatched.pop(str(carried["item_key"]), None)
            diff.carried.append((str(carried["item_key"]), item))
            continue
        diff.added.append(item)

    diff.removed = list(unmatched.values())
    return diff


_EXISTING_SQL = """
SELECT item_key, team_id, product_id, horizon, kind, text_ko, hash,
       horizon_override, product_override, kind_override
FROM roadmap_item
"""


def _fallback_product(cursor) -> int:
    """Where a row born in a refresh lands until somebody files it.

    A new row has to point at some product, and guessing one would be a wrong
    answer wearing a right answer's clothes. So it lands in a visible holding
    pen and the mapping screen is where it gets a real home.
    """
    cursor.execute("SELECT id FROM roadmap_product WHERE name = %s", ("미분류",))
    row = cursor.fetchone()
    if row:
        return row[0]
    cursor.execute(
        "INSERT INTO roadmap_product (name, family_id, owner_team_id, sort) "
        "SELECT %s, f.id, t.id, 9999 FROM roadmap_family f, roadmap_team t "
        "ORDER BY f.sort, t.sort LIMIT 1 RETURNING id",
        ("미분류",),
    )
    return cursor.fetchone()[0]


def apply_refresh(
    database_url: str,
    incoming: Sequence[ParsedItem],
    *,
    label: str,
    prev_html_url: str | None = None,
    source_url: str = OFFICIAL_SOURCE,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Write one refresh: a snapshot, the rows, and what changed.

    Everything in one transaction. A refresh that recorded half its changes
    would leave the history saying something the table does not.
    """
    import psycopg

    taken_at = now or datetime.now(timezone.utc)
    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            existing = _rows_as_dicts(cursor, _EXISTING_SQL)
            diff = diff_items(incoming, existing)

            cursor.execute(
                "INSERT INTO roadmap_snapshot (taken_at, label, source_url, prev_html_url) "
                "VALUES (%s, %s, %s, %s) RETURNING id",
                (taken_at, label, source_url, prev_html_url),
            )
            snapshot_id = cursor.fetchone()[0]

            for key, item in diff.changed:
                _update_row(cursor, key, item, snapshot_id)
            for old_key, item in diff.carried:
                # The key moves to the Notion block id; nothing else does, and
                # the overrides ride along because the row itself is the same.
                _update_row(cursor, old_key, item, snapshot_id, new_key=item.key)
            if diff.added:
                product_id = _fallback_product(cursor)
                for item in diff.added:
                    cursor.execute(
                        "INSERT INTO roadmap_item ("
                        "  item_key, notion_block_id, team_id, product_id, horizon, kind,"
                        "  text_ko, text_en, text_ja, source_url, hash, snapshot_id,"
                        "  translated_hash, sort"
                        ") VALUES (%s, %s, %s, %s, %s, 'dev', %s, %s, %s, %s, %s, %s, NULL,"
                        "  coalesce((SELECT max(sort) + 1 FROM roadmap_item), 0))",
                        (
                            item.key, item.block_id, item.team, product_id, horizon_for(item),
                            item.text_ko, item.text_ko, item.text_ko, source_url,
                            item.hash, snapshot_id,
                        ),
                    )
            for row in diff.removed:
                cursor.execute("DELETE FROM roadmap_item WHERE item_key = %s", (row["item_key"],))

            for change in diff.as_changes():
                cursor.execute(
                    "INSERT INTO roadmap_change "
                    "(snapshot_id, item_key, team_id, type, before_text, after_text) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (
                        snapshot_id, change.key, change.team, change.type,
                        change.before or None, change.after or None,
                    ),
                )
        connection.commit()

    return {
        "snapshot_id": snapshot_id,
        "added": len(diff.added),
        "changed": len(diff.changed),
        "removed": len(diff.removed),
        "carried": len(diff.carried),
        "unchanged": len(incoming) - len(diff.added) - len(diff.changed) - len(diff.carried),
    }


def _update_row(cursor, key: str, item: ParsedItem, snapshot_id: int, new_key: str | None = None) -> None:
    """Write what the source owns, and only that.

    `horizon` is written only when nobody has pinned it. `product` and `kind`
    are never written here at all -- the source has no opinion about either.
    """
    cursor.execute(
        "UPDATE roadmap_item SET "
        "  item_key = coalesce(%s, item_key),"
        "  notion_block_id = %s,"
        "  team_id = %s,"
        "  text_ko = %s,"
        "  hash = %s,"
        "  snapshot_id = %s,"
        "  horizon = CASE WHEN horizon_override THEN horizon ELSE %s END "
        "WHERE item_key = %s",
        (
            new_key, item.block_id, item.team, item.text_ko, item.hash,
            snapshot_id, horizon_for(item), key,
        ),
    )


def _rows_as_dicts(cursor, sql: str) -> list[dict[str, Any]]:
    cursor.execute(sql)
    columns = [column.name for column in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def fetch_table_rows(client: Any, page_id: str) -> list[dict[str, Any]]:
    """The first table on the page, as its `table_row` blocks.

    The first table is the snapshot table -- the one the page calls
    팀별 로드맵 and keeps current. The per-team write-ups below it are the
    history of how it got that way, and reading both would double every row.
    """
    for block in client.iter_block_children(page_id):
        if block.get("type") != "table":
            continue
        return [
            child for child in client.iter_block_children(block["id"])
            if child.get("type") == "table_row"
        ]
    return []
