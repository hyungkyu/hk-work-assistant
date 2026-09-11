"""The org chart, derived from the latest roster observation.

Two roots, not one (HK, 2026-09-11):

    RLWRLD                 the company
    RLWRLD Virtual Lab     the labs, with students under the lab they are in

The roster sheet does not say that. It carries one `조직` string per person,
and every Virtual Lab row sits *inside* the company path -- `RLWRLD | Model
Team | Virtual Lab | Modular VLA`. That nesting is how the company books the
lab, and it is the wrong shape for reading the chart: the labs are a
different population (professors and students, not employees), counted
differently, and burying them three levels inside Model Team makes the one
question people ask of this page -- who is in which lab -- the hardest to
answer.

So the tree is re-rooted here rather than in the sheet: the sheet stays the
single place a person edits, and the reshaping is code that can be changed
without asking anyone to restructure a spreadsheet. Nothing is dropped or
renamed on the way -- `department_raw` is carried on every node, so a reader
can always see the path the sheet actually said.

This module only reads, and only from the database. It is what the daily
batch renders; it never calls a model and never invents a node.
"""

from __future__ import annotations

from typing import Any

# The segment that marks a lab row, wherever it appears in the path.
LAB_MARKER = "Virtual Lab"

# The two roots. `LAB_ROOT` is a name this code introduces; it is never read
# back into the roster, so the sheet is unaffected by it.
COMPANY_ROOT = "RLWRLD"
LAB_ROOT = "RLWRLD Virtual Lab"

# Where a lab member with no lab of their own goes. Named rather than hidden:
# an unassigned person is a question for whoever maintains the sheet, and a
# person who silently vanishes from the chart is not.
UNASSIGNED = "미배정"


def rooted_path(department_raw: str | None, *, affiliation: str, source: str) -> list[str]:
    """The chart path for one person, as a list of node names from a root.

    Three cases, in order:

    * A path that names a lab is re-rooted under `RLWRLD Virtual Lab`, keeping
      everything after the `Virtual Lab` segment as the lab name. `RLWRLD |
      Model Team | Virtual Lab | Modular VLA` becomes `RLWRLD Virtual Lab |
      Modular VLA`; a professor listed directly on `... | Virtual Lab` with
      no lab lands under `미배정`.
    * A row from the external tab (students) has no company path at all, so
      whatever department it carries is read as the lab name directly.
    * Everything else keeps the company path it already had.
    """
    parts = [part.strip() for part in str(department_raw or "").split("|") if part.strip()]

    if LAB_MARKER in parts:
        index = parts.index(LAB_MARKER)
        tail = parts[index + 1 :]
        return [LAB_ROOT, *(tail or [UNASSIGNED])]

    if source == "roster_seed_ext" or (affiliation == "student" and not parts):
        # An external row's department, when it has one, is already the lab.
        return [LAB_ROOT, *(parts or [UNASSIGNED])]

    if not parts:
        return [COMPANY_ROOT, UNASSIGNED]
    return parts


def build_tree(people: list[dict]) -> dict[str, Any]:
    """A two-rooted tree from person rows, each carrying its own path.

    Counts are cumulative: a node's `people` is everyone at or below it, which
    is what a reader means by "how many are in Model Team". The members listed
    on a node are only those sitting exactly there, so nobody is listed twice.
    """
    roots: dict[str, dict[str, Any]] = {}

    def node_for(path: list[str]) -> dict[str, Any]:
        children = roots
        node: dict[str, Any] | None = None
        for depth, name in enumerate(path):
            if name not in children:
                children[name] = {
                    "name": name,
                    "path": " | ".join(path[: depth + 1]),
                    "depth": depth,
                    "people": 0,
                    "members": [],
                    "children": {},
                }
            node = children[name]
            children = node["children"]
        assert node is not None
        return node

    for person in people:
        path = person["chart_path"]
        leaf = node_for(path)
        leaf["members"].append(person)
        # Every ancestor counts this person once.
        for depth in range(len(path)):
            node_for(path[: depth + 1])["people"] += 1

    def finish(mapping: dict[str, dict]) -> list[dict]:
        out = []
        for node in mapping.values():
            node = dict(node)
            node["children"] = finish(node["children"])
            node["members"] = sorted(node["members"], key=lambda person: person["name"])
            out.append(node)
        # Named roots first in a fixed order, then by headcount: a chart whose
        # rows move around between days is one nobody trusts.
        return sorted(out, key=lambda node: (-node["people"], node["name"]))

    tree = finish(roots)
    order = {COMPANY_ROOT: 0, LAB_ROOT: 1}
    return sorted(tree, key=lambda node: (order.get(node["name"], 2), -node["people"]))


def headcount(people: list[dict]) -> dict[str, Any]:
    """The two honest answers, kept apart.

    `people` counts persons once. `by_access` counts what each person can
    reach, and a 방문 연구원 appears under both `student` affiliation and
    `staff_equivalent` access -- that is HK's definition (2026-09-04), not a
    double count waiting to be collapsed. They are never added into one
    number here, because there is no one number.
    """
    affiliation: dict[str, int] = {}
    access: dict[str, int] = {}
    status: dict[str, int] = {}
    for person in people:
        for bucket, key in (
            (affiliation, "affiliation"),
            (access, "access_level"),
            (status, "status"),
        ):
            value = str(person.get(key) or "unknown")
            bucket[value] = bucket.get(value, 0) + 1
    return {
        "people": len(people),
        "by_affiliation": dict(sorted(affiliation.items())),
        "by_access": dict(sorted(access.items())),
        "by_status": dict(sorted(status.items())),
    }


_PEOPLE_SQL = """
    SELECT s.person_id, p.name, s.nickname, s.title, s.employment_type,
           s.affiliation, s.access_level, s.status, s.department_raw,
           observation.source
      FROM org_person_state s
      JOIN org_person p ON p.person_id = s.person_id
      JOIN roster_observation observation
        ON observation.observation_id = s.observation_id
     WHERE s.observation_id = ANY(%(observations)s)
     ORDER BY p.name
"""


def latest_observations(cursor) -> dict[str, int]:
    """The newest observation of each roster tab.

    Per tab, because the two tabs are separate observations of separate
    populations: reading only the newest observation overall would show the
    company without the labs, or the labs without the company, depending on
    which tab was synced last.
    """
    cursor.execute(
        """
        SELECT DISTINCT ON (source) source, observation_id, observed_at
          FROM roster_observation
         ORDER BY source, observation_id DESC
        """
    )
    return {row[0]: int(row[1]) for row in cursor.fetchall()}


def org_chart(database_url: str, *, include_retired: bool = False) -> dict[str, Any]:
    """The chart as data: two roots, counts, and every person on their node."""
    import psycopg

    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            observations = latest_observations(cursor)
            if not observations:
                return {
                    "observations": {},
                    "people": 0,
                    "tree": [],
                    "headcount": headcount([]),
                    "reason": "no roster observation yet; run `worklog org sync --apply`",
                }
            cursor.execute(_PEOPLE_SQL, {"observations": sorted(observations.values())})
            rows = cursor.fetchall()
            cursor.execute(
                "SELECT observed_at FROM roster_observation WHERE observation_id = ANY(%s)"
                " ORDER BY observed_at DESC LIMIT 1",
                (sorted(observations.values()),),
            )
            newest = cursor.fetchone()
            cursor.execute(
                "SELECT kind, value, events FROM org_unmapped_account"
                " WHERE state = 'open' ORDER BY events DESC LIMIT 50"
            )
            unmapped = [
                {"kind": row[0], "value": row[1], "events": row[2]} for row in cursor.fetchall()
            ]

    people = []
    for row in rows:
        (
            person_id,
            name,
            nickname,
            title,
            employment_type,
            affiliation,
            access_level,
            status,
            department_raw,
            source,
        ) = row
        if status == "retired" and not include_retired:
            continue
        people.append(
            {
                "person_id": person_id,
                "name": name,
                "nickname": nickname,
                "title": title,
                "employment_type": employment_type,
                "affiliation": affiliation,
                "access_level": access_level,
                "status": status,
                "department_raw": department_raw,
                "source": source,
                "chart_path": rooted_path(
                    department_raw, affiliation=affiliation, source=source
                ),
            }
        )

    return {
        "observations": observations,
        "observed_at": newest[0].isoformat() if newest and newest[0] else None,
        "people": len(people),
        "headcount": headcount(people),
        "tree": build_tree(people),
        "unmapped_accounts": unmapped,
        "include_retired": include_retired,
    }
