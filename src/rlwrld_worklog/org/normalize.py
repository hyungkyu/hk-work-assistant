"""Sheet rows to standard records.

The header aliases are inherited from the legacy `sync_roster_from_sheets.py`,
and headers are matched by name only, never by position, so a reordered column
or a renamed one that still has a known alias keeps working.

The three axes are never mixed:

    affiliation      who they are        internal · professor · student · unknown
    employment_type  how they are hired  정규직 · 인턴 · 방문 연구원 · 준정규직 …
    access_level     what they can reach staff_equivalent · limited · unknown

A 방문 연구원 is `affiliation=student` and `access_level=staff_equivalent`:
they reach the student space and the internal one, so they count in both by
definition (HK, 2026-09-04).

The legacy version collapsed `product_team / cross_team / standby` into one
field. That was never an organisation -- it was a publication scope -- and
mixing it in is what made the legacy roster unable to answer either question.
"""

from __future__ import annotations

from typing import Any, Iterable

HEADER_ALIASES = {
    "include_in_report": "include_in_report",
    "이름": "name",
    "name": "name",
    "닉네임": "nickname",
    "nickname": "nickname",
    "조직": "department",
    "department": "department",
    "외부호칭(명함)": "title",
    "외부호칭": "title",
    "title": "title",
    "고용형태": "employment_type",
    "employment_type": "employment_type",
    "재직구분": "status",
    "status": "status",
    "email (official)": "email",
    "email(official)": "email",
    "email": "email",
    "email (personal)": "email_personal",
    "email(personal)": "email_personal",
    "email_personal": "email_personal",
    "email (school)": "email_school",
    "email(school)": "email_school",
    "email_school": "email_school",
    "email_alt": "email_alt",
    "github_id": "github",
    "github": "github",
    "slack_uid": "slack_uid",
    "notion_user_id": "notion_user_id",
    "notion_folder_id": "notion_folder_id",
    "slurm_id": "slurm_id",
    "google_docs_id": "google_docs_id",
    "sharepoint_id": "sharepoint_id",
}

# The join keys. Without personal and school emails, GitHub, Slack, Notion and
# Calendar identifiers cannot be attached to a person at all. They live in the
# identity table only, and are never exported to a report, a dashboard or a
# handover record.
IDENTITY_FIELDS = [
    ("email", "email_official"),
    ("email_personal", "email_personal"),
    ("email_school", "email_school"),
    ("github", "github"),
    ("slack_uid", "slack"),
    ("notion_user_id", "notion"),
    ("slurm_id", "slurm"),
]

STAFF_EQUIVALENT = {"정규직", "준정규직", "방문 연구원", "자문직", "사외이사"}
RETIRED_MARK = "퇴사"

# Values that mean "this cell is empty", written by a spreadsheet formula or a
# person. Treated as empty rather than as data, because `#N/A` as a nickname
# would become a nickname.
_PLACEHOLDER = {"#n/a", "-", "n/a", "none"}


def normalize_header(header: str) -> str | None:
    key = str(header or "").strip()
    return HEADER_ALIASES.get(key.lower(), HEADER_ALIASES.get(key))


def _clean(value: Any) -> str:
    text = str(value or "").strip()
    return "" if text.lower() in _PLACEHOLDER else text


def normalize_rows(rows: Iterable[dict], *, source: str) -> list[dict]:
    """Clean a tab's rows. A row with no name is not a person and is dropped."""
    out: list[dict] = []
    for row in rows:
        record: dict[str, Any] = {}
        for key, value in row.items():
            standard = normalize_header(key) or (
                key if key in HEADER_ALIASES.values() else None
            )
            if standard:
                record[standard] = _clean(value)
        if not record.get("name"):
            continue
        record["source"] = source
        record["affiliation"] = affiliation_of(record, source)
        record["access_level"] = access_of(record)
        record["status"] = status_of(record)
        out.append(record)
    return out


def affiliation_of(record: dict, source: str) -> str:
    if source == "roster_seed_ext":
        return "student"
    department = record.get("department") or ""
    employment = record.get("employment_type") or ""
    if not department and not employment:
        # Undecidable. Recorded as unknown rather than guessed: an org chart
        # that quietly files people under a default is worse than one that
        # shows a gap, because the gap is the thing somebody can fix.
        return "unknown"
    if "Virtual Lab" in department:
        return "professor"
    if employment == "방문 연구원":
        return "student"
    return "internal"


def access_of(record: dict) -> str:
    employment = record.get("employment_type") or ""
    if not employment:
        # Not `limited`. An unknown employment type is unknown access, and
        # asserting the restrictive answer is still asserting something we
        # did not observe.
        return "unknown"
    return "staff_equivalent" if employment in STAFF_EQUIVALENT else "limited"


def status_of(record: dict) -> str:
    return "retired" if RETIRED_MARK in (record.get("status") or "") else "active"


def team_path(department: str) -> list[str]:
    """'RLWRLD | Model Team | Virtual Lab' to its hierarchy node names."""
    return [part.strip() for part in str(department or "").split("|") if part.strip()]
