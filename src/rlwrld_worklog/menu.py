"""The left menu: what exists comes from the markup, how it is arranged from the database.

HK, 2026-10-01: LEFT 메뉴를 편집할 수 있게 해주면 좋겠어.

The split is the whole design, and it is there to stop one failure repeating.
Four sessions share this repository and every backoffice screen lives in
`admin.html`. On 2026-09-30 two sessions edited one file blind to each other
and one reverted the other; the menu is the likeliest place for that to happen
again, because every new screen wants a line in it and the test compares the
whole list by equality.

So:

* **admin.html decides which screens exist.** It is the only place that can be
  right about that -- a database row naming a page with no section is a menu
  entry leading nowhere, and a section with no row would vanish from the menu
  entirely.
* **The database decides order, name, group and visibility.** Nothing else.

A screen added tomorrow therefore shows up at the end of the menu on its own,
without anybody touching the menu, and a row left behind for a screen that was
removed is ignored rather than breaking the page.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

STATIC = Path(__file__).resolve().parent / "static"
ADMIN_HTML = STATIC / "admin.html"

# `data-page="x" ... >라벨</button>`, and whether it is disabled or restricted.
_BUTTON = re.compile(
    r'<button\s+data-page="(?P<page>[a-z-]+)"(?P<attrs>[^>]*)>(?P<label>[^<]*)</button>',
    re.S,
)
_GROUP = re.compile(r'<div class="nav-group">(?P<body>.*?)</div>', re.S)
_GROUP_LABEL = re.compile(r'<span class="nav-label">(?P<label>[^<]+)</span>')


def declared_pages(html: str | None = None) -> list[dict[str, Any]]:
    """Every screen the markup declares, in the order it declares them.

    This is the list the menu can contain. Read from the file rather than kept
    in a constant, because a constant would be a second place to update and
    the one somebody forgets.
    """
    source = html if html is not None else ADMIN_HTML.read_text(encoding="utf-8")
    nav = source.split("</nav>")[0]

    pages: list[dict[str, Any]] = []
    for position, group in enumerate(_GROUP.finditer(nav)):
        body = group.group("body")
        label_match = _GROUP_LABEL.search(body)
        group_label = label_match.group("label") if label_match else None
        for button in _BUTTON.finditer(body):
            attrs = button.group("attrs")
            pages.append(
                {
                    "page_id": button.group("page"),
                    "label": button.group("label").strip(),
                    "group_label": group_label,
                    "declared_group": position,
                    # A screen another session is still building. It stays in
                    # the list -- hiding it would make the menu editor unable
                    # to place it before it is finished -- but it is not
                    # something a reader can open yet.
                    "unbuilt": "disabled" in attrs,
                    "owner_only": 'data-requires="super_admin"' in attrs,
                }
            )
    return pages


def arrange(
    declared: list[dict[str, Any]], arrangement: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Apply a stored arrangement to the declared pages.

    Pure, so the rules below are testable without a database or a browser:

    * a declared page with no row keeps its markup label and group and sorts
      after everything arranged -- a screen added today appears tonight,
      at the end, without anybody editing the menu;
    * a row for a page the markup no longer declares is dropped, so deleting
      a screen cannot leave a dead entry;
    * an empty label or group falls back to the markup rather than rendering
      a blank button, because a menu entry with no name is indistinguishable
      from a screen that failed to load.
    """
    by_page = {str(row.get("page_id")): row for row in arrangement}
    arranged: list[dict[str, Any]] = []

    for index, page in enumerate(declared):
        row = by_page.get(page["page_id"])
        if row is None:
            arranged.append(
                {
                    **page,
                    # Beyond any stored position, keeping the markup's own
                    # order among themselves.
                    "position": 10_000 + index,
                    "hidden": False,
                    "arranged": False,
                }
            )
            continue
        label = (row.get("label") or "").strip() or page["label"]
        group_label = (row.get("group_label") or "").strip() or page["group_label"]
        arranged.append(
            {
                **page,
                "label": label,
                "group_label": group_label,
                "position": int(row.get("position") or 0),
                "hidden": bool(row.get("hidden")),
                "arranged": True,
            }
        )

    arranged.sort(key=lambda item: (item["position"], item["page_id"]))
    return arranged


def as_groups(arranged: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The menu as the page renders it: groups in order, entries inside.

    Consecutive entries sharing a group label are one group. Consecutive, not
    collected -- so moving one entry out of a group and back in is a move the
    person can see, rather than something that silently rejoins.
    """
    groups: list[dict[str, Any]] = []
    for entry in arranged:
        if entry.get("hidden"):
            continue
        label = entry.get("group_label")
        if groups and groups[-1]["label"] == label:
            groups[-1]["entries"].append(entry)
        else:
            groups.append({"label": label, "entries": [entry]})
    return groups


def read(database_url: str) -> dict[str, Any]:
    """The declared pages with the stored arrangement applied."""
    import psycopg

    rows: list[dict[str, Any]] = []
    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT page_id, label, group_label, position, hidden "
                "  FROM admin_menu ORDER BY position, page_id"
            )
            rows = [
                {
                    "page_id": row[0],
                    "label": row[1],
                    "group_label": row[2],
                    "position": row[3],
                    "hidden": row[4],
                }
                for row in cursor.fetchall()
            ]

    declared = declared_pages()
    arranged = arrange(declared, rows)
    return {
        "pages": arranged,
        "groups": as_groups(arranged),
        # How many declared screens nobody has placed yet. Worth showing: it
        # is how a screen that arrived overnight announces itself.
        "unarranged": sum(1 for item in arranged if not item["arranged"]),
    }


def save(
    database_url: str, entries: list[dict[str, Any]], *, actor: str
) -> dict[str, Any]:
    """Replace the arrangement with this one.

    Replace rather than merge: the screen sends the whole menu as the person
    sees it, and a merge would make "I moved this one up" depend on what was
    already stored. Rows for pages the markup does not declare are refused
    rather than written, because the only thing they could do later is point
    at nothing.
    """
    import psycopg

    declared = {page["page_id"] for page in declared_pages()}
    unknown = sorted(
        {str(entry.get("page_id")) for entry in entries} - declared
    )
    if unknown:
        raise ValueError(f"선언되지 않은 화면: {', '.join(unknown)}")

    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM admin_menu")
            for position, entry in enumerate(entries):
                cursor.execute(
                    "INSERT INTO admin_menu "
                    "  (page_id, label, group_label, position, hidden, updated_by) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (
                        str(entry.get("page_id")),
                        (entry.get("label") or "").strip() or None,
                        (entry.get("group_label") or "").strip() or None,
                        position,
                        bool(entry.get("hidden")),
                        actor,
                    ),
                )
        connection.commit()

    return {"saved": len(entries), "actor": actor}
