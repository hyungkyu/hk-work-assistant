"""The platform roadmap, read out of the database.

Until 2026-09-29 this screen was a generated HTML file with the whole dataset
inlined. At this size -- 5 teams, 7 families, ~34 products, ~95 rows -- the
generation bought no speed and cost the one thing that matters: a stale file
and a current one look identical.

The shape this module returns is the shape the generated file already had, so
the viewer's renderer did not change when the source did. `build_payload` is
pure and takes rows; `read_roadmap` is the thin part that fetches them. The
split is what lets the payload be tested without a database.
"""

from __future__ import annotations

import hashlib
from typing import Any, Iterable, Mapping, Sequence

from .roadmap_copy import GROUP_LABELS, HORIZONS, KINDS, TEAM_NOTES, UI_COPY

HORIZON_IDS = tuple(horizon["id"] for horizon in HORIZONS)
KIND_IDS = tuple(kind["id"] for kind in KINDS)

OFFICIAL_SOURCE = "https://app.notion.com/p/3ce6cbdff6f68086b8f7cc174bbb040d"


def text_hash(text_ko: str) -> str:
    """The unit a refresh compares. A changed hash is a changed row."""
    return hashlib.sha1(text_ko.encode("utf-8")).hexdigest()[:10]


def _labels(row: Mapping[str, Any]) -> dict[str, str]:
    return {"ko": row["label_ko"], "en": row["label_en"], "ja": row["label_ja"]}


def build_payload(
    *,
    teams: Sequence[Mapping[str, Any]],
    families: Sequence[Mapping[str, Any]],
    products: Sequence[Mapping[str, Any]],
    items: Sequence[Mapping[str, Any]],
    snapshot: Mapping[str, Any] | None = None,
    history: Sequence[Mapping[str, Any]] = (),
    generated: str = "",
) -> dict[str, Any]:
    """Assemble what the screen reads. Pure: rows in, one dict out."""
    by_product = {product["id"]: product for product in products}
    team_labels = {team["id"]: _labels(team) for team in teams}

    payload_items: list[dict[str, Any]] = []
    for row in items:
        product = by_product.get(row["product_id"])
        if product is None:
            # A row whose product was deleted under it is a bug upstream, not
            # a row to drop silently -- dropping it would make the screen
            # quietly disagree with the count beside it.
            raise ValueError(f"roadmap item {row['item_key']!r} names an unknown product")
        owner = product["owner_team_id"]
        payload_items.append(
            {
                "id": row["item_key"],
                "team": row["team_id"],
                "hz": row["horizon"],
                "kind": row["kind"],
                "fam": product["family_id"],
                "prod": product["name"],
                "t": {"ko": row["text_ko"], "en": row["text_en"], "ja": row["text_ja"]},
                "src": row.get("source_url") or OFFICIAL_SOURCE,
                "det": product.get("detail_url") or OFFICIAL_SOURCE,
                "detLabel": team_labels.get(owner, {"ko": owner, "en": owner, "ja": owner}),
                "hash": row["hash"],
                # The page is Korean and a refresh cannot translate. When a
                # row's Korean moves ahead of the translation beside it, the
                # screen has to say so -- English that reads as current and
                # is not is worse than English that is missing.
                "stale": row.get("translated_hash", row["hash"]) != row["hash"],
                # What a refresh may not touch. The screen greys the control
                # rather than hiding that a value was set by hand.
                "locked": sorted(
                    field
                    for field in ("horizon", "product", "kind")
                    if row.get(f"{field}_override")
                ),
            }
        )

    return {
        "ui": UI_COPY,
        "groups": GROUP_LABELS,
        "teamNotes": TEAM_NOTES,
        "horizons": list(HORIZONS),
        "kinds": list(KINDS),
        "teams": [{"id": team["id"], "label": _labels(team)} for team in teams],
        "families": [
            {"id": family["id"], "label": _labels(family), "color": family["color"]}
            for family in families
        ],
        "items": payload_items,
        "history": list(history),
        "snapshot": dict(snapshot) if snapshot else {"id": "", "label": "", "prevUrl": None},
        "source": OFFICIAL_SOURCE,
        "generated": generated,
    }


_TEAMS_SQL = "SELECT id, label_ko, label_en, label_ja FROM roadmap_team ORDER BY sort, id"
_FAMILIES_SQL = (
    "SELECT id, label_ko, label_en, label_ja, color FROM roadmap_family ORDER BY sort, id"
)
_PRODUCTS_SQL = (
    "SELECT id, name, family_id, owner_team_id, detail_url FROM roadmap_product ORDER BY sort, name"
)
_ITEMS_SQL = """
SELECT item_key, team_id, product_id, horizon, kind,
       text_ko, text_en, text_ja, source_url, hash, translated_hash,
       horizon_override, product_override, kind_override
FROM roadmap_item
ORDER BY sort, id
"""
_SNAPSHOT_SQL = """
SELECT id, taken_at, label, prev_html_url
FROM roadmap_snapshot ORDER BY taken_at DESC, id DESC LIMIT 1
"""
_HISTORY_SQL = """
SELECT s.taken_at, s.label, s.prev_html_url, c.type, c.team_id, c.after_text, c.before_text
FROM roadmap_change c JOIN roadmap_snapshot s ON s.id = c.snapshot_id
ORDER BY s.taken_at DESC, c.id
"""


def _rows(cursor, sql: str) -> list[dict[str, Any]]:
    cursor.execute(sql)
    columns = [column.name for column in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _group_history(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """One entry per snapshot, newest first, in the shape the drawer reads."""
    grouped: list[dict[str, Any]] = []
    index: dict[str, int] = {}
    for row in rows:
        at = row["taken_at"].isoformat()
        if at not in index:
            index[at] = len(grouped)
            grouped.append({"at": at, "prev": row["prev_html_url"], "changes": []})
        grouped[index[at]]["changes"].append(
            {
                "type": row["type"],
                "team": row["team_id"],
                "text": row["after_text"] or row["before_text"] or "",
            }
        )
    return grouped


def read_roadmap(database_url: str) -> dict[str, Any]:
    import psycopg

    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            teams = _rows(cursor, _TEAMS_SQL)
            families = _rows(cursor, _FAMILIES_SQL)
            products = _rows(cursor, _PRODUCTS_SQL)
            items = _rows(cursor, _ITEMS_SQL)
            snapshot_rows = _rows(cursor, _SNAPSHOT_SQL)
            history_rows = _rows(cursor, _HISTORY_SQL)

    snapshot = None
    generated = ""
    if snapshot_rows:
        row = snapshot_rows[0]
        snapshot = {
            "id": row["taken_at"].isoformat(),
            "label": row["label"],
            "prevUrl": row["prev_html_url"],
        }
        generated = row["taken_at"].date().isoformat()

    return build_payload(
        teams=teams,
        families=families,
        products=products,
        items=items,
        snapshot=snapshot,
        history=_group_history(history_rows),
        generated=generated,
    )


# --------------------------------------------------------------- seeding

_SEED_SNAPSHOT = """
INSERT INTO roadmap_snapshot (label, source_url, prev_html_url)
VALUES (%(label)s, %(source_url)s, %(prev_html_url)s) RETURNING id
"""


def seed_from_payload(database_url: str, payload: Mapping[str, Any]) -> dict[str, int]:
    """Load a first snapshot from the shape the generated file already had.

    Written to be re-runnable: the reference rows are upserted and the items
    are replaced wholesale, because a seed is not a refresh -- it has no
    changes to record and nothing of anybody's to preserve. The refresh that
    does have both is the next task, and it is the one that has to respect
    `*_override`.
    """
    import psycopg

    counts = {"teams": 0, "families": 0, "products": 0, "items": 0}
    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            for sort, team in enumerate(payload["teams"]):
                cursor.execute(
                    """
                    INSERT INTO roadmap_team (id, label_ko, label_en, label_ja, sort)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO UPDATE SET
                        label_ko = EXCLUDED.label_ko, label_en = EXCLUDED.label_en,
                        label_ja = EXCLUDED.label_ja, sort = EXCLUDED.sort
                    """,
                    (team["id"], team["label"]["ko"], team["label"]["en"], team["label"]["ja"], sort),
                )
                counts["teams"] += 1

            for sort, family in enumerate(payload["families"]):
                cursor.execute(
                    """
                    INSERT INTO roadmap_family (id, label_ko, label_en, label_ja, color, sort)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO UPDATE SET
                        label_ko = EXCLUDED.label_ko, label_en = EXCLUDED.label_en,
                        label_ja = EXCLUDED.label_ja, color = EXCLUDED.color, sort = EXCLUDED.sort
                    """,
                    (
                        family["id"], family["label"]["ko"], family["label"]["en"],
                        family["label"]["ja"], family["color"], sort,
                    ),
                )
                counts["families"] += 1

            # The product's owning team is the team that holds most of its
            # rows. It is a default for roll-ups, not a claim of exclusivity.
            seen: dict[str, dict[str, Any]] = {}
            for item in payload["items"]:
                entry = seen.setdefault(
                    item["prod"],
                    {"family": item["fam"], "detail": item.get("det"), "teams": {}},
                )
                entry["teams"][item["team"]] = entry["teams"].get(item["team"], 0) + 1

            product_ids: dict[str, int] = {}
            for sort, (name, entry) in enumerate(sorted(seen.items())):
                owner = max(entry["teams"].items(), key=lambda pair: (pair[1], pair[0]))[0]
                cursor.execute(
                    """
                    INSERT INTO roadmap_product (name, family_id, owner_team_id, detail_url, sort)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (name) DO UPDATE SET
                        family_id = EXCLUDED.family_id,
                        owner_team_id = EXCLUDED.owner_team_id,
                        detail_url = EXCLUDED.detail_url,
                        sort = EXCLUDED.sort
                    RETURNING id
                    """,
                    (name, entry["family"], owner, entry["detail"], sort),
                )
                product_ids[name] = cursor.fetchone()[0]
                counts["products"] += 1

            snapshot = payload.get("snapshot") or {}
            cursor.execute(
                _SEED_SNAPSHOT,
                {
                    "label": snapshot.get("label") or "seed",
                    "source_url": payload.get("source") or OFFICIAL_SOURCE,
                    "prev_html_url": snapshot.get("prevUrl"),
                },
            )
            snapshot_id = cursor.fetchone()[0]

            cursor.execute("DELETE FROM roadmap_item")
            for sort, item in enumerate(payload["items"]):
                cursor.execute(
                    """
                    INSERT INTO roadmap_item (
                        item_key, team_id, product_id, horizon, kind,
                        text_ko, text_en, text_ja, source_url, hash, snapshot_id, sort,
                        -- A seed carries all three languages written together, so
                        -- the translation is current by construction. Leaving this
                        -- NULL would mark every seeded row as awaiting translation.
                        translated_hash
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        str(item["id"]), item["team"], product_ids[item["prod"]],
                        item["hz"], item["kind"],
                        item["t"]["ko"], item["t"]["en"], item["t"]["ja"],
                        item.get("src"), item.get("hash") or text_hash(item["t"]["ko"]),
                        snapshot_id, sort,
                        item.get("hash") or text_hash(item["t"]["ko"]),
                    ),
                )
                counts["items"] += 1
        connection.commit()
    return counts


# --------------------------------------------------------------- mapping

_MAPPING_PRODUCTS_SQL = """
SELECT p.id, p.name, p.family_id, p.owner_team_id, p.detail_url, p.sort,
       count(i.id) AS items,
       count(i.id) FILTER (WHERE i.kind = 'dev') AS dev,
       count(i.id) FILTER (WHERE i.kind = 'ops') AS ops,
       coalesce(
           array_agg(DISTINCT i.team_id) FILTER (WHERE i.team_id IS NOT NULL),
           ARRAY[]::text[]
       ) AS teams
FROM roadmap_product p
LEFT JOIN roadmap_item i ON i.product_id = p.id
GROUP BY p.id
ORDER BY p.sort, p.name
"""

_MAPPING_ITEMS_SQL = """
SELECT i.item_key, i.team_id, i.product_id, i.horizon, i.kind, i.text_ko,
       i.horizon_override, i.product_override, i.kind_override
FROM roadmap_item i
ORDER BY i.sort, i.id
"""


def read_mapping(database_url: str) -> dict[str, Any]:
    """What the mapping screen edits.

    The per-product counts and the list of teams that touch it are computed,
    never stored: a product spans teams, and a second place to write that down
    is a second place for it to be wrong.
    """
    import psycopg

    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            products = _rows(cursor, _MAPPING_PRODUCTS_SQL)
            items = _rows(cursor, _MAPPING_ITEMS_SQL)
            teams = _rows(cursor, _TEAMS_SQL)
            families = _rows(cursor, _FAMILIES_SQL)

    return {
        "products": [
            {
                "id": row["id"],
                "name": row["name"],
                "family_id": row["family_id"],
                "owner_team_id": row["owner_team_id"],
                "detail_url": row["detail_url"],
                "items": row["items"],
                "dev": row["dev"],
                "ops": row["ops"],
                "teams": sorted(row["teams"]),
            }
            for row in products
        ],
        "items": [
            {
                "key": row["item_key"],
                "team": row["team_id"],
                "product_id": row["product_id"],
                "horizon": row["horizon"],
                "kind": row["kind"],
                "text_ko": row["text_ko"],
                "overrides": sorted(
                    field
                    for field in ("horizon", "product", "kind")
                    if row[f"{field}_override"]
                ),
            }
            for row in items
        ],
        "teams": [{"id": row["id"], "label": _labels(row)} for row in teams],
        "families": [{"id": row["id"], "label": _labels(row)} for row in families],
    }


class RoadmapEditError(ValueError):
    """A mapping edit the store refuses."""


def _one(cursor, sql: str, params: tuple[Any, ...]) -> tuple[Any, ...] | None:
    cursor.execute(sql, params)
    return cursor.fetchone()


def update_product(database_url: str, product_id: int, changes: Mapping[str, Any]) -> dict[str, Any]:
    """Change a product's family, owner team, detail link or name."""
    import psycopg

    allowed = {"name", "family_id", "owner_team_id", "detail_url"}
    unknown = set(changes) - allowed
    if unknown:
        raise RoadmapEditError(f"unknown fields: {', '.join(sorted(unknown))}")
    if not changes:
        raise RoadmapEditError("nothing to change")

    assignments = ", ".join(f"{field} = %s" for field in changes)
    values = tuple(changes[field] for field in changes) + (product_id,)
    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            try:
                row = _one(
                    cursor,
                    f"UPDATE roadmap_product SET {assignments} WHERE id = %s "
                    "RETURNING id, name, family_id, owner_team_id, detail_url",
                    values,
                )
            except psycopg.errors.ForeignKeyViolation as error:
                raise RoadmapEditError("no such family or team") from error
            except psycopg.errors.UniqueViolation as error:
                raise RoadmapEditError("a product with that name already exists") from error
            if row is None:
                raise RoadmapEditError("no such product")
        connection.commit()
    return {
        "id": row[0], "name": row[1], "family_id": row[2],
        "owner_team_id": row[3], "detail_url": row[4],
    }


def create_product(database_url: str, fields: Mapping[str, Any]) -> dict[str, Any]:
    import psycopg

    name = str(fields.get("name") or "").strip()
    if not name:
        raise RoadmapEditError("a product needs a name")
    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            try:
                row = _one(
                    cursor,
                    "INSERT INTO roadmap_product (name, family_id, owner_team_id, detail_url, sort) "
                    "VALUES (%s, %s, %s, %s, coalesce((SELECT max(sort) + 1 FROM roadmap_product), 0)) "
                    "RETURNING id, name, family_id, owner_team_id, detail_url",
                    (name, fields.get("family_id"), fields.get("owner_team_id"), fields.get("detail_url")),
                )
            except psycopg.errors.ForeignKeyViolation as error:
                raise RoadmapEditError("no such family or team") from error
            except psycopg.errors.UniqueViolation as error:
                raise RoadmapEditError("a product with that name already exists") from error
            except psycopg.errors.NotNullViolation as error:
                raise RoadmapEditError("family and owner team are required") from error
        connection.commit()
    return {
        "id": row[0], "name": row[1], "family_id": row[2],
        "owner_team_id": row[3], "detail_url": row[4],
    }


def delete_product(database_url: str, product_id: int) -> None:
    """Only a product nothing points at.

    Refused rather than cascaded: deleting a product that rows still name would
    take those rows off the screen, and a roadmap that loses items quietly is
    the one failure this whole screen exists to prevent.
    """
    import psycopg

    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            used = _one(
                cursor, "SELECT count(*) FROM roadmap_item WHERE product_id = %s", (product_id,)
            )
            if used and used[0]:
                raise RoadmapEditError(f"{used[0]} roadmap items still use this product")
            row = _one(cursor, "DELETE FROM roadmap_product WHERE id = %s RETURNING id", (product_id,))
            if row is None:
                raise RoadmapEditError("no such product")
        connection.commit()


def update_item(database_url: str, item_key: str, changes: Mapping[str, Any]) -> dict[str, Any]:
    """Re-file one roadmap row by hand, and remember that a hand did it.

    Setting a value here raises the matching `*_override` flag, which is what
    stops the next refresh from putting the automatic answer back.
    """
    import psycopg

    allowed = {"product_id", "kind", "horizon"}
    unknown = set(changes) - allowed
    if unknown:
        raise RoadmapEditError(f"unknown fields: {', '.join(sorted(unknown))}")
    if not changes:
        raise RoadmapEditError("nothing to change")
    if "kind" in changes and changes["kind"] not in KIND_IDS:
        raise RoadmapEditError(f"kind must be one of: {', '.join(KIND_IDS)}")
    if "horizon" in changes and changes["horizon"] not in HORIZON_IDS:
        raise RoadmapEditError(f"horizon must be one of: {', '.join(HORIZON_IDS)}")

    fields = dict(changes)
    for field in ("product", "kind", "horizon"):
        key = "product_id" if field == "product" else field
        if key in changes:
            fields[f"{field}_override"] = True

    assignments = ", ".join(f"{field} = %s" for field in fields)
    values = tuple(fields.values()) + (item_key,)
    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            try:
                row = _one(
                    cursor,
                    f"UPDATE roadmap_item SET {assignments} WHERE item_key = %s "
                    "RETURNING item_key, product_id, kind, horizon, "
                    "horizon_override, product_override, kind_override",
                    values,
                )
            except psycopg.errors.ForeignKeyViolation as error:
                raise RoadmapEditError("no such product") from error
            if row is None:
                raise RoadmapEditError("no such roadmap item")
        connection.commit()
    return {
        "key": row[0], "product_id": row[1], "kind": row[2], "horizon": row[3],
        "overrides": sorted(
            field for field, flag in
            (("horizon", row[4]), ("product", row[5]), ("kind", row[6])) if flag
        ),
    }
