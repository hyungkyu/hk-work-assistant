"""What one roster observation should write, as a pure calculation.

Separated from the database on purpose: the interesting decisions here are
about identity and hierarchy, and they are testable without a server. The
execution layer (`store.py`) takes this output and writes it.
"""

from __future__ import annotations

import hashlib
from typing import Any

from .normalize import IDENTITY_FIELDS, team_path


def person_id(record: dict) -> str:
    """A person's stable key: an email if there is one, otherwise the name.

    Employment type and team are deliberately excluded from the key. Somebody
    who moves from 방문 연구원 to 정규직, or between teams, would otherwise
    become a different person and lose everything they had done -- which is
    exactly the attribution loss this whole package exists to prevent.
    """
    key = (
        record.get("email")
        or record.get("email_school")
        or record.get("email_personal")
        or record.get("name")
        or ""
    ).strip().lower()
    return "p_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def team_id(path: str) -> str:
    return "t_" + hashlib.sha256(path.encode("utf-8")).hexdigest()[:12]


def team_rows(department: str) -> list[dict]:
    """Every ancestor node of one department string, root first.

    Keyed by full path rather than by name: two teams can share a leaf name
    under different parents, and collapsing them would merge two teams into
    one on the chart.
    """
    parts = team_path(department)
    parent: str | None = None
    out: list[dict] = []
    for index, part in enumerate(parts):
        path = " | ".join(parts[: index + 1])
        identifier = team_id(path)
        out.append(
            {
                "team_id": identifier,
                "name": part,
                "parent_team_id": parent,
                "path": path,
                "depth": index,
            }
        )
        parent = identifier
    return out


# The identities that may carry a person across a change of key: emails, and
# only emails (HK, 2026-10-08 -- 사번은 곤란, 이메일로 하자). An email names
# one human and the sheet already holds three of them per person; a cluster
# login can be shared, and following it would fold two people into one.
STICKY_KINDS = ("email_official", "email_personal", "email_school")


def resolve_person_id(record: dict, known: dict[tuple[str, str], str] | None = None) -> str:
    """The person this row already is, if any of its identities says so.

    `person_id` hashes whichever email happens to be filled in, so filling in
    a new one changed the key: on 2026-10-08 the sheet gained official emails
    and 171 people became new people overnight, each leaving a copy of
    themselves behind as 시트에 없음. An identity already on file is the
    stronger fact -- the same personal email is the same person -- so it wins,
    in `IDENTITY_FIELDS` order -- official, then personal, then school. With
    no email on file the hash still decides.
    """
    if known:
        for field, kind in IDENTITY_FIELDS:
            if kind not in STICKY_KINDS:
                continue
            value = (record.get(field) or "").strip()
            if value and (kind, value) in known:
                return known[(kind, value)]
    return person_id(record)


def plan(
    records: list[dict],
    observation_id: int,
    known: dict[tuple[str, str], str] | None = None,
) -> dict[str, Any]:
    """The rows one observation implies, with no database involved.

    `known` is the identity table as it stood before this observation, which
    keeps a person's id when the sheet changes which email they are keyed by.
    """
    persons: dict[str, Any] = {}
    states: list[dict] = []
    teams: dict[str, dict] = {}
    identities: dict[tuple[str, str], str] = {}
    for record in records:
        identifier = resolve_person_id(record, known)
        persons[identifier] = record.get("name")
        nodes = team_rows(record.get("department") or "")
        for node in nodes:
            teams[node["team_id"]] = node
        states.append(
            {
                "observation_id": observation_id,
                "person_id": identifier,
                "nickname": record.get("nickname") or None,
                "title": record.get("title") or None,
                "employment_type": record.get("employment_type") or None,
                "affiliation": record["affiliation"],
                "access_level": record["access_level"],
                "status": record["status"],
                "team_id": nodes[-1]["team_id"] if nodes else None,
                "department_raw": record.get("department") or None,
                # Verbatim, honorific included. The chart shows the sheet's
                # wording; only matching a professor to their own lab
                # normalises it.
                "advisor": record.get("advisor") or None,
            }
        )
        for field, kind in IDENTITY_FIELDS:
            value = (record.get(field) or "").strip()
            if value:
                identities[(kind, value)] = identifier
    return {
        "person": persons,
        # Sorted by depth so a parent is always inserted before its child --
        # `org_team.parent_team_id` references the same table.
        "team": sorted(teams.values(), key=lambda node: node["depth"]),
        "person_state": states,
        "identity": identities,
    }


def duplicates(records: list[dict]) -> list[tuple[str, list[str]]]:
    """Rows that fold onto one person, reported rather than merged quietly.

    Two rows with the same email are either a mistake in the sheet or a
    person listed twice on purpose, and only a person can tell which. Merging
    silently would hide both.
    """
    seen: dict[str, list[str]] = {}
    for record in records:
        seen.setdefault(person_id(record), []).append(
            f"{record.get('name')} · "
            f"{record.get('department') or '(department empty)'} · {record.get('status')}"
        )
    return [(identifier, rows) for identifier, rows in seen.items() if len(rows) > 1]
