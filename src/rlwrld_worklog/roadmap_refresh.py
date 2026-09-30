"""Bringing the roadmap up to date from the Notion page it came from.

Three things this has to get right, and they are the reasons it is not a
re-import:

**It must not undo anybody.** Product, kind and horizon can be corrected by
hand on the mapping screen, and each correction raises an `*_override` flag.
A refresh writes what comes from the source -- the Korean text, the team, the
link -- and leaves every flagged column alone.

**It must know what stayed the same.** Rows are matched on the same words
first, then on similar words within the same team. A key alone is not trusted:
it is a position in a cell, so one line inserted at the top shifts every key
below it, and trusting the key would write each line's text onto its
neighbour's row, overrides and all. The similar-words pass is also what makes
the first refresh after the initial import a quiet one -- the seed was edited
by hand ("셋업 (브링업)" became "셋업(브링업)"), so no hash of the page will
ever equal it.

**It must not let a translation lie.** The page is Korean. When a row's Korean
changes, the English and Japanese beside it are stale, and a screen with a
language toggle has no way to say so unless something records it. That is what
`translated_hash` is for.
"""

from __future__ import annotations

import difflib
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
# How deep a marker sits when two lines share an indent. Notion exports nest
# by indent, but a `◦` written flush left is still a sub-bullet.
MARKER_DEPTH = {"◦": 1, "▪": 2}


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


@dataclass
class _Node:
    text: str
    children: list["_Node"] = field(default_factory=list)


def _outline(text: str) -> list[tuple[int, str]]:
    """A cell's lines as (depth, text). Depth 0 is a bare line, a heading.

    Bullet depth is the rank of the line's indent among the cell's bullets,
    not the raw count of spaces: the page indents `•` by two and `◦` by six,
    while a hand-typed cell uses nought and four, and both mean the same tree.
    """
    lines: list[tuple[tuple[int, int] | None, str]] = []
    for raw in text.replace("\r", "").split("\n"):
        if not raw.strip():
            continue
        marker = BULLET.match(raw)
        if not marker:
            lines.append((None, raw.strip()))
            continue
        body = BULLET.sub("", raw).strip()
        if not body:
            continue
        indent = len(raw) - len(raw.lstrip("  \t"))
        lines.append(((indent, MARKER_DEPTH.get(marker.group(1), 0)), body))
    shapes = sorted({shape for shape, _ in lines if shape is not None})
    rank = {shape: depth for depth, shape in enumerate(shapes, start=1)}
    return [(0 if shape is None else rank[shape], body) for shape, body in lines]


def cell_entries(text: str) -> list[str]:
    """One cell into roadmap entries, the unit the seed was written in.

    A bullet with no children is an entry. A bullet with children is a label
    for them -- `• Desktop` over four things Desktop will do -- so each child
    becomes an entry carrying the label in front: `Desktop — 데이터셋 조회`.
    A third level is too fine to stand alone and folds into its parent.

    A bare line is a heading for the bullets under it ("고객에게 제공 가능한
    버전 준비"). It is joined to the first entry beneath it, which is how the
    seed reads it, rather than standing alone with nothing to do. A heading
    with no bullets under it is an entry by itself.
    """
    roots: list[tuple[str | None, _Node]] = []
    stack: list[tuple[int, _Node]] = []
    heading: str | None = None

    for depth, body in _outline(text):
        if depth == 0:
            if heading is not None:
                roots.append((None, _Node(heading)))
            heading = body
            stack.clear()
            continue
        node = _Node(body)
        while stack and stack[-1][0] >= depth:
            stack.pop()
        if stack:
            stack[-1][1].children.append(node)
        else:
            roots.append((heading, node))
            heading = None
        stack.append((depth, node))
    if heading is not None:
        roots.append((None, _Node(heading)))

    def folded(node: _Node) -> str:
        if not node.children:
            return node.text
        return f"{node.text} — " + ", ".join(folded(child) for child in node.children)

    entries: list[str] = []
    for head, node in roots:
        lines = [f"{node.text} — {folded(child)}" for child in node.children] or [node.text]
        if head is not None:
            lines[0] = f"{head} — {lines[0]}"
        entries.extend(lines)
    return entries


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
            for position, text in enumerate(cell_entries(_plain(cells[index]))):
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
    """What a refresh will do. `changed` and `carried` pair an existing row's
    key with the page item that row now is; the item's key may differ."""

    added: list[ParsedItem] = field(default_factory=list)
    changed: list[tuple[str, ParsedItem]] = field(default_factory=list)
    removed: list[Mapping[str, Any]] = field(default_factory=list)
    carried: list[tuple[str, ParsedItem]] = field(default_factory=list)
    # The Korean a changed row had before, by its old key, for the history.
    before: dict[str, str] = field(default_factory=dict)

    def as_changes(self) -> list[Change]:
        out = [
            Change("added", item.key, item.team, after=item.text_ko) for item in self.added
        ]
        out += [
            Change("changed", item.key, item.team, before=self.before.get(key, ""), after=item.text_ko)
            for key, item in self.changed
        ]
        out += [
            Change("removed", str(row["item_key"]), str(row["team_id"]), before=str(row["text_ko"]))
            for row in self.removed
        ]
        return out


# How alike two lines must be to be one row reworded rather than one row
# replaced by another. Measured on 2026-10-01 against the live page and the
# hand-edited seed: reworded rows scored 0.68 and up, while Infra -- which had
# rewritten its roadmap outright -- never scored above 0.38 against its old
# rows. The line sits in that gap, nearer the reworded side.
SIMILAR = 0.65
# Tie-breakers, not reasons: between two equally similar rows, prefer the one
# already in this position, then the one already in this column.
SAME_KEY_BONUS = 0.05
SAME_HORIZON_BONUS = 0.05
# Below this many letters, "the short line appears inside the long one" says
# nothing: a two-word label appears inside half the rows of its team.
MIN_CONTAINED = 8

_NOISE = re.compile(r"[\W_]+")
LABEL = " — "


def _forms(text: str) -> set[str]:
    """The line, and the line without the label a parent bullet put in front."""
    forms = {text}
    if LABEL in text:
        forms.add(text.split(LABEL, 1)[1])
    return {_NOISE.sub("", form).casefold() for form in forms} - {""}


def _alike(a: str, b: str) -> float:
    ratio = difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()
    short, long_ = sorted((a, b), key=len)
    if len(short) < MIN_CONTAINED:
        return ratio
    run = difflib.SequenceMatcher(None, short, long_, autojunk=False).find_longest_match(
        0, len(short), 0, len(long_)
    )
    return max(ratio, 0.9 * run.size / len(short))


def similarity(a: str, b: str) -> float:
    """How much of one line is the other, ignoring spacing and punctuation.

    The larger of the plain ratio and the share of the shorter line found in
    the longer one, taken over each line with and without its leading label:
    `RRC 신규 기능 추가 — Leader 및 operator feedback 확장` is still the seed's
    `Leader 및 operator feedback 확장 (HMD gamepad, ...)`.
    """
    return max((_alike(x, y) for x in _forms(a) for y in _forms(b)), default=0.0)


def diff_items(incoming: Sequence[ParsedItem], existing: Sequence[Mapping[str, Any]]) -> Diff:
    """What changed: same words first, then similar words in the same team.

    1. Same key, same words: nothing happened.
    2. Same words anywhere: the row moved (`carried`). The first refresh after
       an import re-keys every row this way.
    3. Similar words, same team: the row was reworded (`changed`), and it keeps
       its overrides and its translation, now marked behind.
    4. Whatever is left was added or removed.

    A shared key with different words is deliberately not step 1. The key is a
    position in a cell; a line inserted above shifts it.
    """
    diff = Diff()
    unmatched = {str(row["item_key"]): row for row in existing}

    pending: list[ParsedItem] = []
    for item in incoming:
        row = unmatched.get(item.key)
        if row is not None and str(row["hash"]) == item.hash:
            del unmatched[item.key]
        else:
            pending.append(item)

    rest: list[ParsedItem] = []
    for item in pending:
        same_words = [key for key, row in unmatched.items() if str(row["hash"]) == item.hash]
        if same_words:
            # A same-team row first; the page moving a line between teams is
            # rarer than two teams writing the same line. Then the same column.
            same_words.sort(key=lambda key: (
                str(unmatched[key]["team_id"]) != item.team,
                unmatched[key].get("horizon") != horizon_for(item),
            ))
            key = same_words[0]
            del unmatched[key]
            diff.carried.append((key, item))
        else:
            rest.append(item)

    def ranked(item: ParsedItem, key: str, row: Mapping[str, Any]) -> tuple[float, float]:
        alike = similarity(item.text_ko, str(row["text_ko"]))
        bonus = (SAME_KEY_BONUS if key == item.key else 0.0) + (
            SAME_HORIZON_BONUS if row.get("horizon") == horizon_for(item) else 0.0
        )
        return alike + bonus, alike

    scored = sorted(
        (
            (*ranked(item, key, row), n, key)
            for n, item in enumerate(rest)
            for key, row in unmatched.items()
            if str(row["team_id"]) == item.team
        ),
        reverse=True,
    )
    paired: set[int] = set()
    for _total, alike, n, key in scored:
        # The bonuses order the candidates; only the words decide whether a
        # pair is close enough to be one row.
        if alike < SIMILAR or n in paired or key not in unmatched:
            continue
        paired.add(n)
        diff.before[key] = str(unmatched.pop(key)["text_ko"])
        diff.changed.append((key, rest[n]))

    diff.added = [item for n, item in enumerate(rest) if n not in paired]
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

            # Removed rows go first and every moving row steps aside to a key
            # nobody uses: keys are unique, and a line inserted at the top of a
            # cell hands each row below it the key its neighbour still holds.
            for row in diff.removed:
                cursor.execute("DELETE FROM roadmap_item WHERE item_key = %s", (row["item_key"],))
            moving = diff.changed + diff.carried
            for old_key, _item in moving:
                cursor.execute(
                    "UPDATE roadmap_item SET item_key = %s WHERE item_key = %s",
                    (f"moving:{old_key}", old_key),
                )
            for old_key, item in moving:
                # The overrides ride along because the row itself is the same;
                # only what the source owns is written.
                _update_row(cursor, f"moving:{old_key}", item, snapshot_id, new_key=item.key)
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
    for block in _children(client, page_id):
        if block.get("type") != "table":
            continue
        return [child for child in _children(client, block["id"]) if child.get("type") == "table_row"]
    return []


def _children(client: Any, block_id: str) -> Iterable[dict[str, Any]]:
    """The blocks under one block, across every page of the listing.

    `iter_block_children` yields Notion's list responses, not blocks; reading
    each response as a block found no table and made every refresh report an
    empty page (2026-09-30).
    """
    for page in client.iter_block_children(block_id):
        yield from page.get("results") or []
