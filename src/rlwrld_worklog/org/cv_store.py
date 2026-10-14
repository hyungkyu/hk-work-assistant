"""인사정보 상세 in the database: 원본 -> 디비 -> 화면 (HK, 2026-10-14).

The analysis file under ``<archive>/profiles/<사번>.json`` is the original.
`load_if_newer` copies it into the cv_* tables when the file is newer than
what was loaded -- unless the person was edited here, because an edit made to
correct a misreading must survive the next read of the same misreading.

The screen draws from the tables (`read_profile`), edits go to the tables
(`edit_row`), and companies, schools, advisors, projects and papers are
entities that can be renamed and merged (삼성전자 -> 삼성) without touching
the rows that name them.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from psycopg.types.json import Jsonb

from .cv import _EMPLOYEE_NO, name_keys, profiles_root

LEVELS = (
    "고등학교", "대학교", "대학원 (석사)", "대학원 (박사)", "대학원 (석박사통합과정)",
    "대학원 (석사과정)", "대학원 (박사과정)",
)
ENTITY_KINDS = ("org", "school", "advisor", "project", "paper")

# What the screen may change, table by table. Anything else is refused by
# name, so an edit can never reach a key or another person's row by a typo.
EDITABLE: dict[str, tuple[str, ...]] = {
    "cv_profile": ("headline", "summary"),
    "cv_education": ("level", "major", "degree", "lab", "date_from", "date_to", "note"),
    "cv_experience": ("title", "date_from", "date_to", "note"),
    "cv_project": ("role", "note"),
    "cv_publication": ("venue", "year", "position", "citations", "award", "summary", "url", "authors"),
    "cv_etc": ("label", "items"),
}
# Which column of a row names an entity, so a row can be pointed at another.
ENTITY_COLUMNS: dict[str, dict[str, str]] = {
    "cv_education": {"school": "school_id", "advisor": "advisor_id"},
    "cv_experience": {"org": "org_id"},
    "cv_project": {"project": "project_id"},
    "cv_publication": {"paper": "paper_id"},
}


def _lines(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return [str(item) for item in value if item not in (None, "")]
    return [str(value)]


def entity_id(cursor, kind: str, name: str | None) -> int | None:
    name = (name or "").strip()
    if not name:
        return None
    cursor.execute(
        "INSERT INTO cv_entity (kind, name) VALUES (%s, %s) "
        "ON CONFLICT (kind, name) DO UPDATE SET name = EXCLUDED.name RETURNING entity_id",
        (kind, name),
    )
    return cursor.fetchone()[0]


def _level(item: dict[str, Any]) -> str:
    level = (item.get("level") or "").strip()
    if level in LEVELS:
        return level
    degree = f"{item.get('degree') or ''}"
    if "고등" in (item.get("school") or "") or "고등" in degree:
        return "고등학교"
    if "통합" in degree:
        return "대학원 (석박사통합과정)"
    if "박사" in degree:
        return "대학원 (박사과정)" if not item.get("to") and not item.get("end") else "대학원 (박사)"
    if "석사" in degree:
        return "대학원 (석사과정)" if "과정" in degree else "대학원 (석사)"
    return "대학교"


def load_profile(cursor, data: dict[str, Any], *, source_mtime: float | None = None) -> str:
    """Write one analysis into the tables, replacing what was loaded for that 사번."""
    number = str(data.get("employee_no") or "")
    if not _EMPLOYEE_NO.match(number):
        raise ValueError(f"not a 사번: {number!r}")
    cursor.execute("DELETE FROM cv_profile WHERE employee_no = %s", (number,))
    cursor.execute(
        """
        INSERT INTO cv_profile (employee_no, built_at, source_mtime, sources, fetched,
                                headline, summary, metrics, flags, original)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            number, data.get("built_at"), source_mtime,
            Jsonb(data.get("sources") or {}), Jsonb(data.get("fetched") or {}),
            data.get("headline"), data.get("summary"),
            Jsonb(data.get("metrics") or {}), Jsonb(data.get("flags") or []), Jsonb(data),
        ),
    )
    for ord_, item in enumerate(data.get("education") or []):
        cursor.execute(
            """
            INSERT INTO cv_education (employee_no, ord, level, school_id, major, degree,
                                      advisor_id, lab, date_from, date_to, note)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                number, ord_, _level(item), entity_id(cursor, "school", item.get("school")),
                item.get("major"), item.get("degree"),
                entity_id(cursor, "advisor", item.get("advisor")), item.get("lab"),
                item.get("from") or item.get("start") or None, item.get("to") or item.get("end") or None,
                Jsonb(_lines(item.get("note"))),
            ),
        )
    for ord_, item in enumerate(data.get("experience") or []):
        cursor.execute(
            """
            INSERT INTO cv_experience (employee_no, ord, org_id, title, date_from, date_to, note)
            VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING row_id
            """,
            (
                number, ord_, entity_id(cursor, "org", item.get("org")), item.get("title"),
                item.get("from") or item.get("start") or None, item.get("to") or item.get("end") or None,
                Jsonb(_lines(item.get("note"))),
            ),
        )
        experience = cursor.fetchone()[0]
        for project_ord, project in enumerate(item.get("projects") or []):
            cursor.execute(
                "INSERT INTO cv_project (experience_id, ord, project_id, role, note) VALUES (%s, %s, %s, %s, %s)",
                (
                    experience, project_ord, entity_id(cursor, "project", project.get("name")),
                    project.get("role"), Jsonb(_lines(project.get("note"))),
                ),
            )
    for ord_, paper in enumerate(data.get("publications") or []):
        cursor.execute(
            """
            INSERT INTO cv_publication (employee_no, ord, paper_id, venue, year, position,
                                        citations, award, summary, url, authors)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                number, ord_, entity_id(cursor, "paper", paper.get("title")), paper.get("venue"),
                paper.get("year"), paper.get("position"), paper.get("citations"), paper.get("award"),
                paper.get("summary"), paper.get("url"), paper.get("authors"),
            ),
        )
    for ord_, group in enumerate(data.get("etc") or []):
        if not (group.get("items") or []):
            continue
        cursor.execute(
            "INSERT INTO cv_etc (employee_no, ord, label, items) VALUES (%s, %s, %s, %s)",
            (number, ord_, group.get("label") or "기타", Jsonb(group.get("items"))),
        )
    return number


_BUNDLES: dict[str, tuple[float, dict[str, dict[str, Any]]]] = {}


def _bundled(root: Path) -> dict[str, tuple[float, dict[str, Any]]]:
    """Profiles delivered many to a file: ``bundle-*.json``, a list of analyses.

    The whole organisation at once (HK, 2026-10-14: 전체 인원에 대한 것으로
    확대) is one file rather than two hundred. Parsed once per file version.
    """
    out: dict[str, tuple[float, dict[str, Any]]] = {}
    for path in sorted(root.glob("bundle-*.json")) if root.is_dir() else []:
        mtime = path.stat().st_mtime
        cached = _BUNDLES.get(str(path))
        if cached is None or cached[0] != mtime:
            try:
                items = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            by_number = {
                str(item.get("employee_no")): item
                for item in items if isinstance(item, dict) and item.get("employee_no")
            } if isinstance(items, list) else {}
            _BUNDLES[str(path)] = cached = (mtime, by_number)
        for number, item in cached[1].items():
            if number not in out or out[number][0] < mtime:
                out[number] = (mtime, item)
    return out


def load_if_newer(cursor, employee_numbers: list[str], root: Path | None = None) -> list[str]:
    """Load each 사번's file when it is newer than the copy here and nobody edited it."""
    root = root or profiles_root()
    bundled = _bundled(root)
    loaded = []
    for number in employee_numbers:
        if not _EMPLOYEE_NO.match(number or ""):
            continue
        path = root / f"{number}.json"
        mtime, data = None, None
        if path.is_file():
            mtime = path.stat().st_mtime
        if number in bundled and (mtime is None or bundled[number][0] > mtime):
            mtime, data = bundled[number]
        if mtime is None:
            continue
        cursor.execute(
            "SELECT source_mtime, edited_at FROM cv_profile WHERE employee_no = %s", (number,)
        )
        row = cursor.fetchone()
        if row and (row[1] is not None or (row[0] is not None and row[0] >= mtime)):
            continue
        if data is None:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
        if not isinstance(data, dict):
            continue
        data = {**data, "employee_no": number}
        load_profile(cursor, data, source_mtime=mtime)
        loaded.append(number)
    return loaded


def load_all(cursor, root: Path | None = None) -> list[str]:
    root = root or profiles_root()
    numbers = [path.stem for path in sorted(root.glob("*.json")) if _EMPLOYEE_NO.match(path.stem)] if root.is_dir() else []
    numbers = sorted(set(numbers) | set(_bundled(root)))
    return load_if_newer(cursor, numbers, root)


_ENTITY_SQL = """
    WITH RECURSIVE up(entity_id, start_id, depth) AS (
        SELECT entity_id, entity_id, 0 FROM cv_entity
        UNION ALL
        SELECT e.canonical_id, up.start_id, up.depth + 1
          FROM up JOIN cv_entity e ON e.entity_id = up.entity_id
         WHERE e.canonical_id IS NOT NULL AND up.depth < 20
    )
    SELECT DISTINCT ON (up.start_id) up.start_id, up.entity_id
      FROM up ORDER BY up.start_id, up.depth DESC
"""


def _roots(cursor) -> dict[int, int]:
    """entity -> the entity it has been merged into, at the end of the chain."""
    cursor.execute(_ENTITY_SQL)
    return dict(cursor.fetchall())


def _entities(cursor) -> dict[int, dict[str, Any]]:
    cursor.execute("SELECT entity_id, kind, name, canonical_id FROM cv_entity")
    return {row[0]: {"id": row[0], "kind": row[1], "name": row[2], "merged_into": row[3]} for row in cursor.fetchall()}


def _show(entities, roots, entity) -> dict[str, Any] | None:
    if entity is None:
        return None
    root = roots.get(entity, entity)
    return {
        "id": root,
        "name": entities[root]["name"],
        "read_as": entities[entity]["name"] if root != entity else None,
        "row_entity": entity,
    }


def read_profile(cursor, employee_numbers: list[str]) -> dict[str, Any] | None:
    numbers = [n for n in employee_numbers if _EMPLOYEE_NO.match(n or "")]
    if not numbers:
        return None
    cursor.execute(
        """
        SELECT employee_no, built_at, loaded_at, edited_at, sources, fetched, headline,
               summary, metrics, flags
          FROM cv_profile WHERE employee_no = ANY(%s)
         ORDER BY built_at DESC NULLS LAST LIMIT 1
        """,
        (numbers,),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    number = row[0]
    entities, roots = _entities(cursor), _roots(cursor)
    show = lambda entity: _show(entities, roots, entity)  # noqa: E731
    out: dict[str, Any] = {
        "employee_no": number, "built_at": row[1],
        "loaded_at": row[2].isoformat() if row[2] else None,
        "edited_at": row[3].isoformat() if row[3] else None,
        "sources": row[4], "fetched": row[5], "headline": row[6], "summary": row[7],
        "metrics": row[8], "flags": row[9], "levels": list(LEVELS),
    }
    cursor.execute(
        "SELECT row_id, level, school_id, major, degree, advisor_id, lab, date_from, date_to, note "
        "FROM cv_education WHERE employee_no = %s",
        (number,),
    )
    order = {level: index for index, level in enumerate(LEVELS)}
    education = [
        {"row_id": r[0], "level": r[1], "school": show(r[2]), "major": r[3], "degree": r[4],
         "advisor": show(r[5]), "lab": r[6], "date_from": r[7], "date_to": r[8], "note": r[9]}
        for r in cursor.fetchall()
    ]
    # 학력 in the order of the list HK gave: 고등학교 first, graduate school last.
    out["education"] = sorted(education, key=lambda item: (order.get(item["level"], 99), item["date_from"] or ""))
    cursor.execute(
        "SELECT row_id, org_id, title, date_from, date_to, note FROM cv_experience "
        "WHERE employee_no = %s ORDER BY ord",
        (number,),
    )
    experience = [
        {"row_id": r[0], "org": show(r[1]), "title": r[2], "date_from": r[3], "date_to": r[4],
         "note": r[5], "projects": []}
        for r in cursor.fetchall()
    ]
    by_id = {item["row_id"]: item for item in experience}
    if by_id:
        cursor.execute(
            "SELECT row_id, experience_id, project_id, role, note FROM cv_project "
            "WHERE experience_id = ANY(%s) ORDER BY ord",
            (list(by_id),),
        )
        for r in cursor.fetchall():
            by_id[r[1]]["projects"].append({"row_id": r[0], "project": show(r[2]), "role": r[3], "note": r[4]})
    out["experience"] = experience
    cursor.execute(
        "SELECT row_id, paper_id, venue, year, position, citations, award, summary, url, authors "
        "FROM cv_publication WHERE employee_no = %s ORDER BY year DESC NULLS LAST, ord",
        (number,),
    )
    out["publications"] = [
        {"row_id": r[0], "paper": show(r[1]), "venue": r[2], "year": r[3], "position": r[4],
         "citations": r[5], "award": r[6], "summary": r[7], "url": r[8], "authors": r[9]}
        for r in cursor.fetchall()
    ]
    cursor.execute(
        "SELECT row_id, label, items FROM cv_etc WHERE employee_no = %s ORDER BY ord", (number,)
    )
    out["etc"] = [{"row_id": r[0], "label": r[1], "items": r[2]} for r in cursor.fetchall()]
    return out


def related(cursor, entity: int) -> dict[str, Any]:
    """Everyone whose rows name this entity or anything merged into it."""
    entities, roots = _entities(cursor), _roots(cursor)
    if entity not in entities:
        raise LookupError("no such entity")
    root = roots.get(entity, entity)
    family = [e for e in entities if roots.get(e, e) == root]
    kind = entities[root]["kind"]
    queries = {
        "school": ("SELECT employee_no, date_from, date_to, level || coalesce(' · ' || major, ''), school_id "
                   "FROM cv_education WHERE school_id = ANY(%s)"),
        "advisor": ("SELECT employee_no, date_from, date_to, level || coalesce(' · ' || major, ''), advisor_id "
                    "FROM cv_education WHERE advisor_id = ANY(%s)"),
        "org": "SELECT employee_no, date_from, date_to, title, org_id FROM cv_experience WHERE org_id = ANY(%s)",
        "project": ("SELECT x.employee_no, x.date_from, x.date_to, p.role, p.project_id FROM cv_project p "
                    "JOIN cv_experience x ON x.row_id = p.experience_id WHERE p.project_id = ANY(%s)"),
        "paper": ("SELECT employee_no, year::text, year::text, position || coalesce(' · ' || venue, ''), paper_id "
                  "FROM cv_publication WHERE paper_id = ANY(%s)"),
    }
    cursor.execute(queries[kind], (family,))
    rows = cursor.fetchall()
    numbers = sorted({r[0] for r in rows})
    people: dict[str, tuple[str, str]] = {}
    if numbers:
        cursor.execute(
            """
            SELECT identity.value, person.person_id, person.name
              FROM org_identity identity
              JOIN org_person person ON person.person_id = identity.person_id
             WHERE identity.kind = 'employee_no' AND identity.value = ANY(%s)
            """,
            (numbers,),
        )
        people = {number: (pid, name) for number, pid, name in cursor.fetchall()}
    members = []
    for number, date_from, date_to, role, row_entity in rows:
        pid, name = people.get(number, (None, None))
        members.append({
            "employee_no": number, "person_id": pid, "person_name": name,
            "date_from": date_from, "date_to": date_to, "role": role,
            "read_as": entities[row_entity]["name"] if row_entity != root else None,
        })
    members.sort(key=lambda m: (m["date_from"] or "", m["person_name"] or ""))
    return {
        "entity": {"id": root, "kind": kind, "name": entities[root]["name"]},
        "merged": [
            {"id": e, "name": entities[e]["name"]} for e in family if e != root
        ],
        "members": members,
    }


def entity_list(cursor, kind: str) -> list[dict[str, Any]]:
    if kind not in ENTITY_KINDS:
        raise ValueError(f"unknown kind: {kind}")
    roots = _roots(cursor)
    cursor.execute("SELECT entity_id, name FROM cv_entity WHERE kind = %s ORDER BY name", (kind,))
    return [{"id": e, "name": n} for e, n in cursor.fetchall() if roots.get(e, e) == e]


def suggest_same(cursor, kind: str) -> list[dict[str, Any]]:
    """Pairs whose names share a part -- 다음 / 카카오 and 카카오 -- offered, never applied."""
    items = entity_list(cursor, kind)
    out = []
    for i, a in enumerate(items):
        for b in items[i + 1:]:
            if name_keys(a["name"]) & name_keys(b["name"]):
                out.append({"a": a, "b": b})
    return out


def _touch(cursor, table: str, row_id: int) -> None:
    owner = {
        "cv_profile": None,
        "cv_project": "SELECT x.employee_no FROM cv_project p JOIN cv_experience x ON x.row_id = p.experience_id WHERE p.row_id = %s",
    }.get(table, f"SELECT employee_no FROM {table} WHERE row_id = %s")
    if owner is None:
        return
    cursor.execute(owner, (row_id,))
    found = cursor.fetchone()
    if found:
        cursor.execute("UPDATE cv_profile SET edited_at = now() WHERE employee_no = %s", (found[0],))


def edit_row(cursor, table: str, row_id: Any, field: str, value: Any) -> None:
    """One field of one row, from the screen. The person is marked edited."""
    if table not in EDITABLE:
        raise ValueError(f"not editable: {table}")
    if field in EDITABLE[table]:
        if field == "level" and value not in LEVELS:
            raise ValueError(f"학력 구분은 {', '.join(LEVELS)} 중 하나")
        if field in ("note", "items"):
            value = Jsonb(_lines(value) if not isinstance(value, list) else value)
        elif field in ("year", "citations"):
            value = int(value) if str(value).strip() else None
        else:
            value = (str(value).strip() or None) if value is not None else None
        key = "employee_no" if table == "cv_profile" else "row_id"
        cursor.execute(f"UPDATE {table} SET {field} = %s WHERE {key} = %s", (value, row_id))  # noqa: S608
        if cursor.rowcount != 1:
            raise LookupError("no such row")
        if table == "cv_profile":
            cursor.execute("UPDATE cv_profile SET edited_at = now() WHERE employee_no = %s", (row_id,))
        else:
            _touch(cursor, table, int(row_id))
        return
    entity_kinds = ENTITY_COLUMNS.get(table, {})
    if field in entity_kinds:
        target = entity_id(cursor, field, str(value or ""))
        cursor.execute(
            f"UPDATE {table} SET {entity_kinds[field]} = %s WHERE row_id = %s", (target, row_id)  # noqa: S608
        )
        if cursor.rowcount != 1:
            raise LookupError("no such row")
        _touch(cursor, table, int(row_id))
        return
    raise ValueError(f"not editable: {table}.{field}")


def rename_entity(cursor, entity: int, name: str) -> None:
    name = (name or "").strip()
    if not name:
        raise ValueError("빈 이름")
    cursor.execute("SELECT kind FROM cv_entity WHERE entity_id = %s", (entity,))
    row = cursor.fetchone()
    if row is None:
        raise LookupError("no such entity")
    cursor.execute("SELECT entity_id FROM cv_entity WHERE kind = %s AND name = %s", (row[0], name))
    clash = cursor.fetchone()
    if clash and clash[0] != entity:
        # The name is taken: that is a merge, said as one.
        merge_entity(cursor, entity, clash[0])
        return
    cursor.execute("UPDATE cv_entity SET name = %s WHERE entity_id = %s", (name, entity))


def merge_entity(cursor, entity: int, into: int) -> None:
    """삼성전자 -> 삼성: `entity` (and whatever was merged into it) now shows as `into`."""
    entities, roots = _entities(cursor), _roots(cursor)
    if entity not in entities or into not in entities:
        raise LookupError("no such entity")
    if entities[entity]["kind"] != entities[into]["kind"]:
        raise ValueError("다른 종류끼리는 합칠 수 없습니다")
    source, target = roots.get(entity, entity), roots.get(into, into)
    if source == target:
        return
    cursor.execute("UPDATE cv_entity SET canonical_id = %s WHERE entity_id = %s", (target, source))


def unmerge_entity(cursor, entity: int) -> None:
    cursor.execute("UPDATE cv_entity SET canonical_id = NULL WHERE entity_id = %s", (entity,))
