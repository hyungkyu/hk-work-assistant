"""Bringing the roadmap up to date from the Notion page it came from.

**What it reads.** Not the snapshot table at the top of the page. The page asks
teams to copy their final version into that table, and after 2026-09-02 none
did. They write dated sections under "팀별 로드맵 갱신 내역" instead --
"20260930 HW", "20261001 Infra" -- newest on top, each with its own 이번달,
다음달 and 장기계획. Those sections are read, and their relative headings are
turned into months: 이번달 in a section written on 09-01 is 2026-09, and in one
written on 09-30 it is 2026-10, which is what the team itself wrote beside it
("이번달 (10월)").

**How sections add up.** Months accumulate. When a team writes the same month
again, its newest section replaces the older one for that month; September
stays as September's sections left it. 향후 is the newest 장기계획. An item the
source calls unscheduled is filed under 미정 wherever it was written. A line
struck through is done.

**It must not undo anybody.** Product, kind and horizon can be corrected by
hand on the mapping screen, and each correction raises an `*_override` flag.
A refresh writes what comes from the source and leaves every flagged column
alone.

**It must know what stayed the same.** Rows are matched on the same words
first, then on similar words within the same team. Keys are Notion block ids
now, but rows that came from the table are keyed otherwise, and the seed was
edited by hand ("셋업 (브링업)" became "셋업(브링업)"), so words are what tie a
row to its source.

**It must not let a translation lie.** The page is Korean. When a row's Korean
changes, the English and Japanese beside it are stale, and `translated_hash`
records it.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Iterable, Iterator, Mapping, Sequence

from .roadmap import OFFICIAL_SOURCE, text_hash

# The team in a section heading ("20260930 Robotics Platform"), as the page
# spells it, against the ids the database uses. A name here that the page stops
# using shows up as a team whose rows all disappear, which is loud -- and that is
# the point of keeping the mapping explicit rather than slugifying the heading.
TEAM_BY_LABEL = {
    "Robotics Platform": "rp",
    "Human Data": "hd",
    "Infra": "infra",
    "Infra & Data": "infra",
    "LOOP": "loop",
    "HW": "hw",
}

# The heading the dated sections sit under.
UPDATE_LOG = "갱신 내역"

LIST_TYPES = ("bulleted_list_item", "numbered_list_item", "to_do", "toggle")

SECTION = re.compile(r"^\s*(\d{4})(\d{2})(\d{2})\s+(.+?)\s*$")
EXPLICIT_MONTH = re.compile(r"(20\d{2})\s*[/.\-]\s*(\d{1,2})(?!\d)")
BARE_MONTH = re.compile(r"(\d{1,2})\s*월")
LONG_TERM = ("장기", "향후")

# A section written this late in a month calls the coming month 이번달 -- HW and
# Robotics Platform both wrote "이번달 (10월)" on 09-30. Only used when the
# heading names no month itself.
MONTH_END_DAY = 25

UNDATED_MARKS = ("일정이 아직 미정", "일정 미정", "미정이라")


@dataclass
class ParsedItem:
    key: str
    team: str
    # Where the section filed it: YYYY-MM, or "later" for 장기계획.
    column: str
    text_ko: str
    block_id: str
    hash: str = ""
    done: bool = False
    # Position on the page, so the screen lists rows in the source's order.
    sort: int = 0

    def __post_init__(self) -> None:
        if not self.hash:
            self.hash = text_hash(self.text_ko)


def horizon_for(item: ParsedItem) -> str:
    """The section's month, unless the source says the date is not decided."""
    if any(mark in item.text_ko for mark in UNDATED_MARKS):
        return "undated"
    return item.column


# --- reading the page ------------------------------------------------------


def _plain(rich_text: Iterable[Mapping[str, Any]]) -> str:
    return "".join(str(run.get("plain_text") or "") for run in rich_text)


def _runs(block: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    body = block.get(str(block.get("type") or "")) or {}
    return list(body.get("rich_text") or [])


def block_text(block: Mapping[str, Any]) -> str:
    return _plain(_runs(block)).strip()


def is_struck(block: Mapping[str, Any]) -> bool:
    """Every run that says something is struck through."""
    runs = [run for run in _runs(block) if str(run.get("plain_text") or "").strip()]
    return bool(runs) and all((run.get("annotations") or {}).get("strikethrough") for run in runs)


def _month(year: int, month: int) -> str:
    year += (month - 1) // 12
    month = (month - 1) % 12 + 1
    return f"{year:04d}-{month:02d}"


def period_for(heading: str, written: date) -> str | None:
    """A period heading as a month or "later", or None for one nobody planned for.

    A month the heading names wins over 이번달/다음달 beside it: "이번달 (10월)"
    is October because it says so.
    """
    if any(word in heading for word in LONG_TERM):
        return "later"
    explicit = EXPLICIT_MONTH.search(heading)
    if explicit and 1 <= int(explicit.group(2)) <= 12:
        return _month(int(explicit.group(1)), int(explicit.group(2)))
    bare = BARE_MONTH.search(heading)
    if bare and 1 <= int(bare.group(1)) <= 12:
        month = int(bare.group(1))
        # December writing about 1월 means next January.
        year = written.year + (1 if written.month - month > 6 else 0)
        return _month(year, month)
    base = written.month + (1 if written.day >= MONTH_END_DAY else 0)
    if "이번달" in heading:
        return _month(written.year, base)
    if "다음달" in heading:
        return _month(written.year, base + 1)
    return None


def _is_placeholder(block: Mapping[str, Any]) -> bool:
    """`Wheeled Humanoid:` with nothing after it is a slot, not a plan."""
    text = block_text(block)
    return not text or (text.endswith((":", "：")) and not _real_children(block))


def _real_children(block: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [child for child in block.get("children") or () if not _is_placeholder(child)]


def _folded(block: Mapping[str, Any]) -> str:
    children = _real_children(block)
    if not children:
        return block_text(block)
    return f"{block_text(block)} — " + ", ".join(_folded(child) for child in children)


@dataclass
class _Entry:
    key: str
    text: str
    done: bool


def _entries(blocks: Sequence[Mapping[str, Any]]) -> list[_Entry]:
    """One period's blocks into entries, the unit the seed was written in.

    A bullet with no children is an entry. A bullet with children is a label
    for them -- `• Desktop` over four things Desktop will do -- so each child is
    an entry with the label in front: `Desktop — 데이터셋 조회`. A third level
    folds into its parent. A paragraph is a heading for the bullets under it
    and joins the first of them.
    """
    entries: list[_Entry] = []
    heading: tuple[str, str] | None = None

    def take(entry: _Entry) -> None:
        nonlocal heading
        if heading is not None:
            entry = _Entry(entry.key, f"{heading[1]} — {entry.text}", entry.done)
            heading = None
        entries.append(entry)

    for block in blocks:
        kind = block.get("type")
        if kind == "paragraph":
            if not block_text(block):
                continue
            if heading is not None:
                entries.append(_Entry(heading[0], heading[1], False))
            heading = (str(block.get("id") or ""), block_text(block))
            continue
        if kind not in LIST_TYPES or _is_placeholder(block):
            continue
        children = _real_children(block)
        if not children:
            take(_Entry(str(block["id"]), block_text(block), is_struck(block)))
            continue
        for child in children:
            take(_Entry(
                str(child["id"]),
                f"{block_text(block)} — {_folded(child)}",
                is_struck(block) or is_struck(child),
            ))
    if heading is not None:
        entries.append(_Entry(heading[0], heading[1], False))
    return entries


def _flattened(blocks: Sequence[Mapping[str, Any]]) -> Iterator[Mapping[str, Any]]:
    """Headings with their bodies folded inside them (toggle headings) laid flat."""
    for block in blocks:
        yield block
        if str(block.get("type") or "").startswith("heading") and block.get("children"):
            yield from _flattened(block["children"])


@dataclass
class _Period:
    written: date
    order: int
    team: str
    period: str
    blocks: list[Mapping[str, Any]] = field(default_factory=list)


def parse_updates(blocks: Sequence[Mapping[str, Any]]) -> list[ParsedItem]:
    """The update log into roadmap rows: months accumulated, newest section per month.

    `blocks` are the blocks after the update-log heading, each carrying its
    loaded descendants under `children`.
    """
    periods: list[_Period] = []
    team: str | None = None
    written: date | None = None
    current: _Period | None = None
    for block in _flattened(blocks):
        kind = block.get("type")
        if kind in ("heading_1", "heading_2"):
            # The end of the update log.
            break
        if kind == "heading_3":
            current = None
            match = SECTION.match(block_text(block))
            team = TEAM_BY_LABEL.get(match.group(4)) if match else None
            try:
                written = date(int(match.group(1)), int(match.group(2)), int(match.group(3))) if match else None
            except ValueError:
                written = None
            if written is None:
                team = None
            continue
        if kind == "heading_4":
            current = None
            if team is None or written is None:
                continue
            period = period_for(block_text(block), written)
            if period is None:
                continue
            current = _Period(written, len(periods), team, period)
            periods.append(current)
            continue
        if current is not None:
            current.blocks.append(block)

    # Newest section per team and period; on the same day, the one higher on the
    # page, since the page stacks newest on top.
    chosen: dict[tuple[str, str], _Period] = {}
    for period in periods:
        key = (period.team, period.period)
        held = chosen.get(key)
        if held is None or (period.written, -period.order) > (held.written, -held.order):
            chosen[key] = period

    items: list[ParsedItem] = []
    seen: set[str] = set()
    ordered = sorted(chosen.values(), key=lambda p: (p.period == "later", p.period, p.order))
    for period in ordered:
        for entry in _entries(period.blocks):
            if not entry.key or entry.key in seen:
                continue
            seen.add(entry.key)
            items.append(ParsedItem(
                key=entry.key, team=period.team, column=period.period, text_ko=entry.text,
                block_id=entry.key, done=entry.done, sort=len(items),
            ))
    return items


def read_page(client: Any, page_id: str) -> list[dict[str, Any]]:
    """The blocks after the update-log heading, with their descendants loaded.

    The snapshot table above it is not read: nobody has kept it current since
    2026-09-02, and its rows would only be an older copy of the sections.
    """
    top = list(_children(client, page_id))
    start = next(
        (n for n, block in enumerate(top) if block.get("type") == "heading_2" and UPDATE_LOG in block_text(block)),
        None,
    )
    if start is None:
        return []
    body = top[start + 1:]
    for block in body:
        _load(client, block)
    return body


def read_items(client: Any, page_id: str) -> list[ParsedItem]:
    return parse_updates(read_page(client, page_id))


def _load(client: Any, block: dict[str, Any]) -> None:
    if not block.get("has_children") or block.get("type") in ("table", "child_page", "child_database"):
        return
    block["children"] = list(_children(client, block["id"]))
    for child in block["children"]:
        _load(client, child)


def _children(client: Any, block_id: str) -> Iterator[dict[str, Any]]:
    """The blocks under one block, across every page of the listing.

    `iter_block_children` yields Notion's list responses, not blocks; reading
    each response as a block found nothing and made every refresh report an
    empty page (2026-09-30).
    """
    for page in client.iter_block_children(block_id):
        yield from page.get("results") or []


# --- the diff -------------------------------------------------------------


@dataclass
class Change:
    type: str
    key: str
    team: str
    before: str = ""
    after: str = ""


@dataclass
class Diff:
    """What a refresh will do. `same`, `changed` and `carried` pair an existing
    row's key with the page item that row now is; the item's key may differ."""

    added: list[ParsedItem] = field(default_factory=list)
    changed: list[tuple[str, ParsedItem]] = field(default_factory=list)
    removed: list[Mapping[str, Any]] = field(default_factory=list)
    carried: list[tuple[str, ParsedItem]] = field(default_factory=list)
    # Same key, same words. Still written: its month, order or strike may move.
    same: list[tuple[str, ParsedItem]] = field(default_factory=list)
    # Matched rows whose strike-through came or went.
    toggled: list[ParsedItem] = field(default_factory=list)
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
        out += [
            Change("completed" if item.done else "reopened", item.key, item.team, after=item.text_ko)
            for item in self.toggled
        ]
        return out


# How alike two lines must be to be one row reworded rather than one row
# replaced by another. Measured twice against the live page. On 2026-10-01,
# reworded rows scored 0.68 and up and Infra's rewritten roadmap never above
# 0.38. On 2026-10-07, when HW and Robotics Platform wrote October again,
# every true pair scored 0.68 and up and every false one 0.58 or less. The line
# sits in that gap.
SIMILAR = 0.65
# Tie-breakers, not reasons: between two equally similar rows, prefer the one
# already in this position, then the one already in this column.
SAME_KEY_BONUS = 0.05
SAME_HORIZON_BONUS = 0.05

_NOISE = re.compile(r"[\W_]+")
LABEL = " — "


def _forms(text: str) -> set[str]:
    """The line, and the line without the label a parent bullet put in front."""
    forms = {text}
    if LABEL in text:
        forms.add(text.split(LABEL, 1)[1])
    return {_NOISE.sub("", form).casefold() for form in forms} - {""}


def _alike(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


def similarity(a: str, b: str) -> float:
    """How alike two lines are, ignoring spacing and punctuation, taken over each
    line with and without its leading label: `HW Develop — ALLEX 센서 부착` is
    still `ALLEX 센서 부착`.

    Not "is the short line inside the long one". That once let
    `Simplexity IX Pro 테스트` claim a September line that merely listed
    Simplexity IX Pro among four robots (2026-10-07).
    """
    return max((_alike(x, y) for x in _forms(a) for y in _forms(b)), default=0.0)


def diff_items(incoming: Sequence[ParsedItem], existing: Sequence[Mapping[str, Any]]) -> Diff:
    """What changed: same words first, then similar words in the same team.

    1. Same key, same words: the same row (`same`).
    2. Same words anywhere: the row moved (`carried`).
    3. Similar words, same team: the row was reworded (`changed`), and it keeps
       its overrides and its translation, now marked behind.
    4. Whatever is left was added or removed.

    Any matched row whose strike-through came or went is also `toggled`.
    """
    diff = Diff()
    unmatched = {str(row["item_key"]): row for row in existing}
    done_before: dict[str, bool] = {key: bool(row.get("done")) for key, row in unmatched.items()}

    pending: list[ParsedItem] = []
    for item in incoming:
        row = unmatched.get(item.key)
        if row is not None and str(row["hash"]) == item.hash:
            del unmatched[item.key]
            diff.same.append((item.key, item))
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
    diff.toggled = [
        item
        for key, item in diff.same + diff.carried + diff.changed
        if done_before.get(key, False) != item.done
    ]
    return diff


_EXISTING_SQL = """
SELECT item_key, team_id, product_id, horizon, kind, text_ko, hash, done,
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
            # nobody uses: keys are unique, and a re-keyed row can be handed the
            # key another row still holds.
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
            for key, item in diff.same:
                _update_row(cursor, key, item, snapshot_id)
            if diff.added:
                product_id = _fallback_product(cursor)
                for item in diff.added:
                    cursor.execute(
                        "INSERT INTO roadmap_item ("
                        "  item_key, notion_block_id, team_id, product_id, horizon, kind,"
                        "  text_ko, text_en, text_ja, source_url, hash, snapshot_id,"
                        "  translated_hash, done, sort"
                        ") VALUES (%s, %s, %s, %s, %s, 'dev', %s, %s, %s, %s, %s, %s, NULL, %s, %s)",
                        (
                            item.key, item.block_id, item.team, product_id, horizon_for(item),
                            item.text_ko, item.text_ko, item.text_ko, source_url,
                            item.hash, snapshot_id, item.done, item.sort,
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
        "unchanged": len(diff.same),
        "toggled": len(diff.toggled),
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
        "  done = %s,"
        "  sort = %s,"
        "  horizon = CASE WHEN horizon_override THEN horizon ELSE %s END "
        "WHERE item_key = %s",
        (
            new_key, item.block_id, item.team, item.text_ko, item.hash,
            snapshot_id, item.done, item.sort, horizon_for(item), key,
        ),
    )


def _rows_as_dicts(cursor, sql: str) -> list[dict[str, Any]]:
    cursor.execute(sql)
    columns = [column.name for column in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]
