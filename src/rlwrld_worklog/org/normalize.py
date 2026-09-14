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
    # The external tab's column G. For a Virtual Lab student the lab is their
    # advisor's lab, so this is what names the node they belong under -- the
    # project names on the internal tab (Modular VLA, Allex) are a different
    # axis and do not answer "whose lab is this person in".
    "소속 학교 연구실 지도교수님": "advisor",
    "소속 학교 연구실 지도교수": "advisor",
    "지도교수님": "advisor",
    "지도교수": "advisor",
    "advisor": "advisor",
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

# The external tab is a Google Form response sheet, so its columns are not
# names but the questions people were asked -- "성 + 이름 (한글 ex, 류형규)",
# "slurm/naver cloud  username", and a paragraph of instructions inside the
# header cell itself. Exact matching found none of them, and the tab read as
# 191 rows with no recognisable columns.
#
# So these match on a distinctive fragment, in order, and only when exact
# matching missed. The fragments are chosen to be unambiguous within this
# sheet: "성 + 이름" and not "이름", because the Given Name and Last Name
# questions both contain "한글 이름이 있는 경우" in their instructions and a
# bare "이름" would capture them instead.
HEADER_PATTERNS: tuple[tuple[str, str], ...] = (
    ("성 + 이름", "name"),
    ("학교 이메일", "email_school"),
    ("email address", "email_personal"),
    ("지도교수", "advisor"),
    ("깃헙 주소", "github"),
    # HK's own description of how externals are identified, from the first
    # conversation about this data: "ROASTER_SEED_EXT 에서 이름 (슬럼/네이버
    # 클라우드 이름) 으로 식별될거야". This column is that name. The separate
    # "slurm uid" column holds a number, which identifies nothing a person
    # would recognise, so it is left alone.
    ("naver cloud", "slurm_id"),
    ("리얼월드 인턴", "intern"),
    ("deactivated", "deactivated"),
)

# Columns deliberately never read, listed so the omission is a decision on
# the page rather than an accident of the allowlist. The form asks for a
# phone number and an SSH public key; neither belongs in this system, and an
# allowlist that grew carelessly would swallow both.
NEVER_READ = ("휴대폰", "ssh public key")

STAFF_EQUIVALENT = {"정규직", "준정규직", "방문 연구원", "자문직", "사외이사"}
RETIRED_MARK = "퇴사"

# Values that mean "this cell is empty", written by a spreadsheet formula or a
# person. Treated as empty rather than as data, because `#N/A` as a nickname
# would become a nickname.
_PLACEHOLDER = {"#n/a", "-", "n/a", "none"}


def normalize_header(header: str) -> str | None:
    """The standard field one sheet column means, or None if we do not read it.

    Exact names first, then the fragment rules the form-response tab needs.
    A column that matches nothing is not an error: most of that form is
    questions this system has no business storing.
    """
    key = str(header or "").strip()
    exact = HEADER_ALIASES.get(key.lower(), HEADER_ALIASES.get(key))
    if exact:
        return exact
    # Collapse the whitespace the form headers carry -- newlines inside a
    # header cell, and doubled spaces in "slurm/naver cloud  username".
    folded = " ".join(key.split()).casefold()
    if not folded:
        return None
    for fragment in NEVER_READ:
        if fragment in folded:
            return None
    for fragment, standard in HEADER_PATTERNS:
        if fragment.casefold() in folded:
            return standard
    return None


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
        record["employment_type"] = employment_of(record, source)
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


# Values the form's yes/no columns use for "yes". Anything else, including
# blank, is not a yes -- an unticked box is not a statement.
_AFFIRMATIVE = {"y", "yes", "true", "o", "완료", "예", "1", "v", "체크"}


def _is_yes(value: Any) -> bool:
    return str(value or "").strip().casefold() in _AFFIRMATIVE


def status_of(record: dict) -> str:
    """Active unless the sheet says otherwise, in whichever way it says it.

    The internal tab writes 퇴사 in 재직구분. The form-response tab has a
    `Deactivated` box instead, which means the same thing about a student.
    Both are read; neither is inferred from the other's absence.
    """
    if _is_yes(record.get("deactivated")):
        return "retired"
    return "retired" if RETIRED_MARK in (record.get("status") or "") else "active"


def employment_of(record: dict, source: str) -> str:
    """What the sheet says about how somebody is engaged, and nothing more.

    The form asks whether a student is a 리얼월드 인턴; when it is ticked,
    that is their employment type. When it is not, the sheet has not said,
    and this returns empty so `access_of` reports `unknown` rather than
    asserting a restriction nobody wrote down.
    """
    declared = record.get("employment_type") or ""
    if declared:
        return declared
    if source == "roster_seed_ext" and _is_yes(record.get("intern")):
        return "인턴"
    return ""


# Honorifics a sheet writes after an advisor's name. Stripped for matching a
# professor to their own lab, never for display: the chart shows the sheet's
# wording, and only the comparison is normalised.
_HONORIFICS = ("교수님", "교수", "선생님", "박사님", "박사")


def bare_name(value: str | None) -> str:
    """An advisor or professor name with any honorific and spacing removed."""
    text = str(value or "").strip()
    for honorific in _HONORIFICS:
        if text.endswith(honorific):
            text = text[: -len(honorific)].strip()
            break
    return text.replace(" ", "")


def team_path(department: str) -> list[str]:
    """'RLWRLD | Model Team | Virtual Lab' to its hierarchy node names."""
    return [part.strip() for part in str(department or "").split("|") if part.strip()]
