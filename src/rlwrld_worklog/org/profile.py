"""One person, as the panel that opens from the org chart shows them.

Three sections, in the order HK named them on 2026-10-12:

    인사정보          what the roster says, now and over time
    최근 하고 있는 일   a summary of recent work -- source to be named
    상세 정보          the detail behind it -- source to be named

Only the first has a source today. The other two are declared rather than
left out: each comes back `connected: false` with the reason, so the panel
has a fixed place for them and nothing has to be invented to fill it. When
HK names an API or a page for one (2026-10-12: API 혹은 HTML 을 호출할 수
있도록 할께 -- 정해지는대로 알려줄께), that section starts carrying data;
the panel and the route do not change shape.

What the roster section leaves out, and why:

* Emails and account ids. They are join keys, kept in `org_identity` and
  never exported to a report, a dashboard or a handover record (see
  `normalize.IDENTITY_FIELDS`).
* Hire and leave dates. Never stored (HK, 2026-09-04), so there is nothing
  to show; the history below dates changes by when the sheet said them.
"""

from __future__ import annotations

from typing import Any

# The roster fields the panel shows, in order, with the label it shows them
# under. Anything not listed here is not shown.
ROSTER_FIELDS = (
    ("nickname", "닉네임"),
    ("title", "외부호칭(명함)"),
    ("department_raw", "조직"),
    ("employment_type", "고용형태"),
    ("status", "재직구분"),
    ("advisor", "지도교수"),
    ("affiliation", "소속 구분"),
    ("access_level", "접근 수준"),
)

# The fields whose change is history worth listing. Affiliation and access
# follow from these, so listing them too would say each change twice.
HISTORY_FIELDS = ("department_raw", "employment_type", "status", "title", "advisor", "nickname")

STATUS_LABEL = {"active": "재직", "retired": "퇴사", "absent_from_sheet": "시트에 없음"}

TAB_LABEL = {"roster_seed_2": "내부 탭", "roster_seed_ext": "연구실 학생 탭"}

_STATES_SQL = """
    SELECT o.observation_id, o.observed_at, o.source, s.nickname, s.title,
           s.employment_type, s.affiliation, s.access_level, s.status,
           s.department_raw, s.advisor
      FROM org_person_state s
      JOIN roster_observation o ON o.observation_id = s.observation_id
     WHERE s.person_id = %s
     ORDER BY o.observation_id
"""

_COLUMNS = (
    "observation_id", "observed_at", "source", "nickname", "title", "employment_type",
    "affiliation", "access_level", "status", "department_raw", "advisor",
)


def history(states: list[dict]) -> list[dict]:
    """The observations at which something the sheet says about them changed.

    The first observation is listed as where the record starts. After that
    only changes: the sheet is read daily and mostly says the same thing, so
    listing every observation would bury the three that matter.
    """
    out: list[dict] = []
    previous: dict | None = None
    for state in states:
        if previous is None:
            out.append({"observed_at": state["observed_at"], "first": True, "changes": []})
        else:
            changes = [
                {"field": field, "before": previous.get(field), "after": state.get(field)}
                for field in HISTORY_FIELDS
                if (previous.get(field) or None) != (state.get(field) or None)
            ]
            if changes:
                out.append({"observed_at": state["observed_at"], "first": False, "changes": changes})
        previous = state
    return out


def not_connected(section: str) -> dict[str, Any]:
    return {
        "connected": False,
        "reason": "아직 연결된 출처가 없습니다. 출처가 정해지면 이 자리에 나옵니다.",
        "section": section,
    }


def person_profile(database_url: str, person_id: str) -> dict[str, Any] | None:
    """The panel's data for one person, or None when there is no such person."""
    import psycopg

    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT name FROM org_person WHERE person_id = %s", (person_id,))
            found = cursor.fetchone()
            if not found:
                return None
            cursor.execute(_STATES_SQL, (person_id,))
            states = [dict(zip(_COLUMNS, row)) for row in cursor.fetchall()]

    for state in states:
        state["observed_at"] = state["observed_at"].isoformat() if state["observed_at"] else None
    current = states[-1] if states else {}
    fields = [
        {"key": key, "label": label, "value": current.get(key)}
        for key, label in ROSTER_FIELDS
        if current.get(key)
    ]
    for field in fields:
        if field["key"] == "status":
            field["value"] = STATUS_LABEL.get(field["value"], field["value"])

    return {
        "person_id": person_id,
        "name": found[0],
        "hr": {
            "source": "roster",
            "tab": TAB_LABEL.get(current.get("source"), current.get("source")),
            "observed_at": current.get("observed_at"),
            "fields": fields,
            "history": history(states),
            # Further HR information, from a source HK will provide.
            "extra": not_connected("hr_extra"),
        },
        "recent_work": not_connected("recent_work"),
        "detail": not_connected("detail"),
    }
