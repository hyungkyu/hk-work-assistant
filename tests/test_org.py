"""The organisation: normalising the sheet, and the shape of the chart.

Nothing here touches the network or a real roster. The rows are the shapes the
sheet actually has -- the header names are the ones in `HEADER_ALIASES`,
inherited from the legacy sync -- with invented people.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rlwrld_worklog.org.chart import (  # noqa: E402
    COMPANY_ROOT,
    LAB_ROOT,
    UNASSIGNED,
    build_tree,
    headcount,
    place_professors,
    rooted_path,
)
from rlwrld_worklog.org.normalize import bare_name, normalize_rows  # noqa: E402
from rlwrld_worklog.org.plan import duplicates, person_id, plan, team_rows  # noqa: E402


def row(**overrides):
    base = {
        "이름": "테스터",
        "닉네임": "tester",
        "조직": "RLWRLD | Model Team",
        "고용형태": "정규직",
        "재직구분": "재직",
        "email (official)": "tester@rlwrld.invalid",
    }
    base.update(overrides)
    return base


# ------------------------------------------------------------- normalising


def test_headers_are_matched_by_name_in_either_language() -> None:
    [record] = normalize_rows([row()], source="roster_seed_2")
    assert record["name"] == "테스터"
    assert record["nickname"] == "tester"
    assert record["department"] == "RLWRLD | Model Team"


def test_a_row_without_a_name_is_not_a_person() -> None:
    assert normalize_rows([row(이름="")], source="roster_seed_2") == []


def test_placeholder_cells_are_empty_not_data() -> None:
    """`#N/A` as a nickname would otherwise become somebody's nickname."""
    [record] = normalize_rows([row(닉네임="#N/A", **{"email (official)": "-"})],
                              source="roster_seed_2")
    assert record["nickname"] == ""
    assert record["email"] == ""


def test_a_visiting_researcher_is_a_student_with_staff_access() -> None:
    """Both, and deliberately: they reach the student space and the internal one.

    HK, 2026-09-04. Counting them twice is the definition, and a later reader
    must not "fix" it as a duplicate.
    """
    [record] = normalize_rows([row(고용형태="방문 연구원")], source="roster_seed_2")
    assert record["affiliation"] == "student"
    assert record["access_level"] == "staff_equivalent"


def test_an_unknown_employment_type_is_unknown_access_not_limited() -> None:
    """Asserting the restrictive answer is still asserting what we did not see."""
    [record] = normalize_rows([row(고용형태="")], source="roster_seed_2")
    assert record["access_level"] == "unknown"


def test_a_row_with_neither_department_nor_employment_is_unknown() -> None:
    [record] = normalize_rows([row(조직="", 고용형태="")], source="roster_seed_2")
    assert record["affiliation"] == "unknown"


def test_a_virtual_lab_row_is_a_professor_and_the_external_tab_is_students() -> None:
    [professor] = normalize_rows(
        [row(조직="RLWRLD | Model Team | Virtual Lab | Allex")], source="roster_seed_2"
    )
    assert professor["affiliation"] == "professor"
    [student] = normalize_rows([row(조직="")], source="roster_seed_ext")
    assert student["affiliation"] == "student"


def test_retirement_is_read_from_the_status_column() -> None:
    [record] = normalize_rows([row(재직구분="퇴사(2026-03)")], source="roster_seed_2")
    assert record["status"] == "retired"


# ------------------------------------------------------------------- keys


def test_a_persons_key_survives_a_change_of_team_and_employment() -> None:
    """Otherwise a promotion makes somebody a new person and loses their past."""
    before = normalize_rows([row(고용형태="방문 연구원", 조직="RLWRLD | Model Team")],
                            source="roster_seed_2")[0]
    after = normalize_rows([row(고용형태="정규직", 조직="RLWRLD | Platform Team")],
                           source="roster_seed_2")[0]
    assert person_id(before) == person_id(after)


def test_a_person_with_no_email_is_keyed_by_name() -> None:
    record = normalize_rows([row(**{"email (official)": ""})], source="roster_seed_ext")[0]
    assert person_id(record).startswith("p_")


def test_two_rows_that_fold_onto_one_person_are_reported_not_merged() -> None:
    records = normalize_rows([row(), row(조직="RLWRLD | Platform Team")], source="roster_seed_2")
    found = duplicates(records)
    assert len(found) == 1 and len(found[0][1]) == 2


def test_team_rows_are_keyed_by_path_so_two_teams_can_share_a_name() -> None:
    left = team_rows("RLWRLD | Model Team | Infra")
    right = team_rows("RLWRLD | Platform Team | Infra")
    assert left[-1]["name"] == right[-1]["name"] == "Infra"
    assert left[-1]["team_id"] != right[-1]["team_id"]


def test_the_plan_orders_teams_so_a_parent_exists_before_its_child() -> None:
    """`org_team.parent_team_id` references the same table."""
    records = normalize_rows(
        [row(조직="RLWRLD | Model Team | Virtual Lab | Allex")], source="roster_seed_2"
    )
    written = plan(records, observation_id=1)
    depths = [node["depth"] for node in written["team"]]
    assert depths == sorted(depths)


def test_the_plan_collects_every_account_as_an_identity() -> None:
    records = normalize_rows(
        [row(github_id="tester", slurm_id="tester01", slack_uid="U01")], source="roster_seed_2"
    )
    written = plan(records, observation_id=1)
    assert ("github", "tester") in written["identity"]
    assert ("slurm", "tester01") in written["identity"]
    assert ("slack", "U01") in written["identity"]


# ------------------------------------------------------------- chart shape


def test_a_lab_is_the_advisors_lab_not_the_project_name() -> None:
    """HK, 2026-09-11: 연구실 means the advisor's lab.

    The external tab's column G carries "주한별 교수님". The internal tab's
    Virtual Lab paths carry project names (Modular VLA, Allex) -- a different
    axis, which cannot answer whose lab somebody is in. The node keeps the
    sheet's wording, honorific and all.
    """
    assert rooted_path(
        "", affiliation="student", source="roster_seed_ext", advisor="주한별 교수님"
    ) == [LAB_ROOT, "주한별 교수님"]


def test_a_student_with_no_advisor_is_named_not_hidden() -> None:
    assert rooted_path("", affiliation="student", source="roster_seed_ext") == [
        LAB_ROOT,
        UNASSIGNED,
    ]


def test_a_professor_waits_under_the_lab_root_until_placed() -> None:
    """Their own lab node cannot be known from their row alone."""
    assert rooted_path(
        "RLWRLD | Model Team | Virtual Lab | Modular VLA",
        affiliation="professor",
        source="roster_seed_2",
    ) == [LAB_ROOT]


def test_a_professor_is_placed_on_the_lab_named_after_them() -> None:
    people = [
        _person("주한별", [LAB_ROOT], affiliation="professor"),
        _person("학생하나", [LAB_ROOT, "주한별 교수님"], affiliation="student"),
    ]
    assert place_professors(people) == []
    assert people[0]["chart_path"] == [LAB_ROOT, "주한별 교수님"]


def test_a_professor_whose_lab_has_no_students_is_reported_not_filed_by_guess() -> None:
    """Either a lab with nobody in the sheet yet, or two tabs spelling one name."""
    people = [
        _person("임종우", [LAB_ROOT], affiliation="professor"),
        _person("학생하나", [LAB_ROOT, "주한별 교수님"], affiliation="student"),
    ]
    assert place_professors(people) == ["임종우"]
    assert people[0]["chart_path"] == [LAB_ROOT]


def test_honorifics_and_spacing_do_not_stop_a_professor_matching_their_lab() -> None:
    assert bare_name("주한별 교수님") == bare_name("주 한별") == "주한별"
    people = [
        _person("주 한별", [LAB_ROOT], affiliation="professor"),
        _person("학생", [LAB_ROOT, "주한별 교수님"], affiliation="student"),
    ]
    place_professors(people)
    assert people[0]["chart_path"] == [LAB_ROOT, "주한별 교수님"]


def test_one_advisor_gathers_every_student_under_one_node() -> None:
    """Three students of one advisor are one lab, not three."""
    people = [
        _person(name, rooted_path("", affiliation="student", source="roster_seed_ext",
                                  advisor="신진우 교수님"), affiliation="student")
        for name in ("가", "나", "다")
    ]
    tree = build_tree(people)
    lab_root = [node for node in tree if node["name"] == LAB_ROOT][0]
    assert [child["name"] for child in lab_root["children"]] == ["신진우 교수님"]
    assert lab_root["children"][0]["people"] == 3


def test_a_company_row_keeps_the_path_it_had() -> None:
    assert rooted_path(
        "RLWRLD | Platform Team | Infra & Data", affiliation="internal", source="roster_seed_2"
    ) == ["RLWRLD", "Platform Team", "Infra & Data"]


def test_a_company_row_with_no_department_lands_somewhere_visible() -> None:
    assert rooted_path("", affiliation="internal", source="roster_seed_2") == [
        COMPANY_ROOT,
        UNASSIGNED,
    ]


def _person(name: str, path: list[str], **extra):
    base = {
        "name": name,
        "chart_path": path,
        "affiliation": "internal",
        "access_level": "staff_equivalent",
        "status": "active",
    }
    base.update(extra)
    return base


def test_the_tree_has_two_roots_with_the_company_first() -> None:
    tree = build_tree(
        [
            _person("가", ["RLWRLD", "Model Team"]),
            _person("나", [LAB_ROOT, "Allex"], affiliation="student"),
            _person("다", [LAB_ROOT, "Modular VLA"], affiliation="student"),
        ]
    )
    assert [node["name"] for node in tree] == [COMPANY_ROOT, LAB_ROOT]
    assert tree[1]["people"] == 2
    assert [child["name"] for child in tree[1]["children"]] == ["Allex", "Modular VLA"]


def test_a_nodes_count_includes_everyone_below_it_but_lists_only_its_own() -> None:
    tree = build_tree(
        [
            _person("가", ["RLWRLD", "Model Team"]),
            _person("나", ["RLWRLD", "Model Team", "Foundation"]),
        ]
    )
    company = tree[0]
    model = company["children"][0]
    assert (company["people"], model["people"]) == (2, 2)
    assert [person["name"] for person in model["members"]] == ["가"]
    assert [person["name"] for person in model["children"][0]["members"]] == ["나"]


def test_headcount_keeps_the_two_answers_apart() -> None:
    counts = headcount(
        [
            _person("가", ["RLWRLD"], affiliation="internal", access_level="staff_equivalent"),
            _person("나", [LAB_ROOT], affiliation="student", access_level="staff_equivalent"),
            _person("다", [LAB_ROOT], affiliation="student", access_level="limited"),
        ]
    )
    assert counts["people"] == 3
    assert counts["by_affiliation"] == {"internal": 1, "student": 2}
    assert counts["by_access"] == {"limited": 1, "staff_equivalent": 2}


@pytest.mark.parametrize("bad", ("RLWRLD||Model Team", " RLWRLD |  Model Team "))
def test_stray_separators_and_spacing_do_not_create_empty_nodes(bad: str) -> None:
    assert rooted_path(bad, affiliation="internal", source="roster_seed_2") == [
        "RLWRLD",
        "Model Team",
    ]


def test_the_advisor_column_is_read_by_name_in_either_spelling() -> None:
    [record] = normalize_rows(
        [{"이름": "학생", "소속 학교 연구실 지도교수님": "최성준 교수님"}],
        source="roster_seed_ext",
    )
    assert record["advisor"] == "최성준 교수님"
    [short] = normalize_rows(
        [{"이름": "학생", "지도교수": "조민수 교수님"}], source="roster_seed_ext"
    )
    assert short["advisor"] == "조민수 교수님"


def test_the_plan_carries_the_advisor_so_the_chart_can_group_by_lab() -> None:
    records = normalize_rows(
        [{"이름": "학생", "소속 학교 연구실 지도교수님": "신진우 교수님"}],
        source="roster_seed_ext",
    )
    written = plan(records, observation_id=1)
    assert written["person_state"][0]["advisor"] == "신진우 교수님"
