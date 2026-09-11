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


def rooted_path(
    department_raw: str | None,
    *,
    affiliation: str,
    source: str,
    advisor: str | None = None,
) -> list[str]:
    """The chart path for one person, as a list of node names from a root.

    A lab is an advisor's lab. That is what 연구실 means for these people, and
    the roster says it in the external tab's advisor column -- "주한별 교수님".
    The internal tab's Virtual Lab paths carry project names instead (Modular
    VLA, Allex, 3D/4D Perception); those are a different axis and cannot
    answer "whose lab is this person in", so they do not name lab nodes. They
    are not lost: `department_raw` rides along on every row.

    Four cases, in order:

    * A student with an advisor goes under that advisor's node, verbatim,
      honorific and all -- the chart shows the sheet's wording.
    * Any other external row goes under `미배정`, named rather than hidden:
      an unassigned student is a question for whoever keeps the sheet.
    * A row whose path names `Virtual Lab` is re-rooted under the lab root.
      Its professor is placed on their own lab node by `place_professors`
      below, which needs every row to do and so cannot happen here.
    * Everything else keeps the company path it already had.
    """
    parts = [part.strip() for part in str(department_raw or "").split("|") if part.strip()]
    advisor_name = str(advisor or "").strip()

    if source == "roster_seed_ext":
        return [LAB_ROOT, advisor_name or UNASSIGNED]

    if advisor_name:
        return [LAB_ROOT, advisor_name]

    if LAB_MARKER in parts:
        # Pending professor placement. A professor whose name matches a lab
        # goes onto it; one who matches nothing stays here, under the root,
        # which is visible and honest rather than filed under a guess.
        return [LAB_ROOT]

    if affiliation == "student" and not parts:
        return [LAB_ROOT, UNASSIGNED]

    if not parts:
        return [COMPANY_ROOT, UNASSIGNED]
    return parts


def place_professors(people: list[dict]) -> list[str]:
    """Move each professor onto the lab node named after them.

    The students name their lab by their advisor; the professors are listed
    on the internal tab under project names. Matching the two is what puts a
    professor at the head of their own lab instead of loose under the root,
    and it needs every row at once -- which is why it is a pass over the list
    rather than part of `rooted_path`.

    Matching strips honorifics and spaces from both sides and compares what
    is left. Nothing is renamed: the node keeps the students' wording, and a
    professor who matches no lab is left where they are and reported, because
    an unmatched professor is a real thing to look at -- either a lab with no
    students in the sheet yet, or a spelling that differs between the tabs.
    """
    from .normalize import bare_name

    labs: dict[str, str] = {}
    for person in people:
        path = person["chart_path"]
        if len(path) >= 2 and path[0] == LAB_ROOT and path[1] != UNASSIGNED:
            labs.setdefault(bare_name(path[1]), path[1])

    unmatched: list[str] = []
    for person in people:
        if person.get("affiliation") != "professor":
            continue
        path = person["chart_path"]
        if path[:1] != [LAB_ROOT] or len(path) > 1:
            continue
        lab = labs.get(bare_name(person.get("name")))
        if lab:
            person["chart_path"] = [LAB_ROOT, lab]
        else:
            unmatched.append(str(person.get("name")))
    return unmatched


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
           s.advisor, observation.source
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
            advisor,
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
                "advisor": advisor,
                "chart_path": rooted_path(
                    department_raw,
                    affiliation=affiliation,
                    source=source,
                    advisor=advisor,
                ),
            }
        )

    unmatched = place_professors(people)

    return {
        "observations": observations,
        "observed_at": newest[0].isoformat() if newest and newest[0] else None,
        "people": len(people),
        "headcount": headcount(people),
        # A professor whose name matches no lab: either a lab with no students
        # listed yet, or the two tabs spelling one person differently. Named,
        # because both are things somebody can fix and neither fixes itself.
        "professors_without_a_lab": unmatched,
        "tree": build_tree(people),
        "unmapped_accounts": unmapped,
        "include_retired": include_retired,
    }
