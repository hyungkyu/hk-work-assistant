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


def test_a_missing_xlsx_reader_says_how_to_install_it(monkeypatch) -> None:
    """A batch that dies on an import must say what fixes it.

    On 2026-09-14 the roster sync failed with a bare ModuleNotFoundError:
    openpyxl was declared in pyproject.toml and never installed into the
    virtualenv the batch actually runs from. A traceback in a log nobody
    opens is not a report.
    """
    import builtins

    from rlwrld_worklog.org import sheet

    real_import = builtins.__import__

    def without_openpyxl(name, *args, **kwargs):
        if name == "openpyxl":
            raise ModuleNotFoundError("No module named 'openpyxl'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_openpyxl)
    with pytest.raises(RuntimeError) as error:
        sheet.rows_from_workbook(b"", "roster_seed_2")
    assert "pip install openpyxl" in str(error.value)


def _workbook(tabs: dict[str, list[list[str]]]) -> bytes:
    """A real .xlsx in memory, so the reader is exercised and not mocked."""
    import io

    import openpyxl

    book = openpyxl.Workbook()
    book.remove(book.active)
    for name, rows in tabs.items():
        sheet = book.create_sheet(name)
        for row in rows:
            sheet.append(row)
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def test_the_external_tab_is_found_under_the_spelling_the_sheet_uses() -> None:
    """The sheet says `roaster_seed_ext` -- "roaster", not "roster".

    That is the sheet's name for the tab and not ours to correct: renaming it
    would break every formula and every other reader pointed at it. The source
    we store stays canonical; only the lookup accepts either spelling.
    """
    from rlwrld_worklog.org.sheet import read_all, resolve_tabs

    data = _workbook(
        {
            "roster_seed_2": [["이름", "조직"], ["직원", "RLWRLD | Model Team"]],
            "roaster_seed_ext": [
                ["이름", "소속 학교 연구실 지도교수님"],
                ["학생", "주한별 교수님"],
            ],
            "roster_seed.csv_old": [["이름"], ["옛날 사람"]],
        }
    )
    assert resolve_tabs(data) == {
        "roster_seed_2": "roster_seed_2",
        "roster_seed_ext": "roaster_seed_ext",
    }
    by_tab = read_all(data)
    # Keyed by the name the database knows, not the one the sheet used.
    assert set(by_tab) == {"roster_seed_2", "roster_seed_ext"}
    assert by_tab["roster_seed_ext"][0]["advisor"] == "주한별 교수님"
    assert by_tab["roster_seed_ext"][0]["source"] == "roster_seed_ext"


def test_a_tab_that_is_gone_is_reported_with_what_the_workbook_has() -> None:
    """Somebody renamed or deleted it: a real event, not zero people."""
    from rlwrld_worklog.org.sheet import read_all

    data = _workbook({"roster_seed_2": [["이름"], ["직원"]]})
    with pytest.raises(KeyError) as error:
        read_all(data)
    message = str(error.value)
    assert "roaster_seed_ext" in message and "roster_seed_2" in message


def test_a_sibling_of_the_company_root_is_folded_under_it() -> None:
    """The sheet writes "RLWRLD BOD | US"; the board is part of the company.

    The first real run produced three roots because that string is a sibling
    of "RLWRLD | ..." rather than a child. HK asked for two roots, and the
    board is not a peer of the company it belongs to.
    """
    assert rooted_path(
        "RLWRLD BOD | US", affiliation="internal", source="roster_seed_2"
    ) == [COMPANY_ROOT, "BOD", "US"]
    assert rooted_path(
        "RLWRLD BOD", affiliation="internal", source="roster_seed_2"
    ) == [COMPANY_ROOT, "BOD"]


def test_the_company_root_itself_is_not_folded_into_itself() -> None:
    assert rooted_path(
        "RLWRLD | Model Team", affiliation="internal", source="roster_seed_2"
    ) == [COMPANY_ROOT, "Model Team"]


def test_a_header_below_a_title_row_is_still_found() -> None:
    """The student tab opens with a title line above its column names.

    Reading the first non-empty row as the header made that tab read as zero
    students with no error -- the worst shape a failure can take.
    """
    from rlwrld_worklog.org.sheet import rows_from_workbook

    data = _workbook(
        {
            "roster_seed_2": [
                ["Virtual Lab 학생 명단", "", ""],
                [],
                ["이름", "소속 학교 연구실 지도교수님", "email (school)"],
                ["학생하나", "신진우 교수님", "one@school.invalid"],
            ]
        }
    )
    rows = rows_from_workbook(data, "roster_seed_2")
    assert [row["이름"] for row in rows] == ["학생하나"]


def test_a_tab_whose_columns_are_unrecognisable_is_an_error_not_an_empty_list() -> None:
    from rlwrld_worklog.org.sheet import rows_from_workbook

    data = _workbook({"roster_seed_2": [["가", "나"], ["1", "2"]]})
    with pytest.raises(KeyError, match="no header row"):
        rows_from_workbook(data, "roster_seed_2")


# ------------------------------------- the external tab is a form response

# The real header of `roaster_seed_ext`: a Google Form's questions, not column
# names. Kept verbatim so a change to the form shows up here as a failing test
# rather than as a tab that quietly reads as empty.
EXT_HEADER = [
    "Timestamp",
    "성 + 이름 (한글 ex, 류형규)",
    "Given Name(ex, jaekyoung)  slurm 계정에 사용되므로 아래 내용을 주의해서 작성하세요."
    "  - 한글 이름이 있는 경우 반드시 그 이름을 영어로 쓰세요",
    "Last Name(ex, bae)  slurm 계정에 사용되므로 아래 내용을 주의해서 작성하세요."
    "  - 한글 이름이 있는 경우 반드시 그 이름을 영어로 쓰세요",
    "Email Address",
    "학교 이메일",
    "소속 학교 연구실 지도교수님",
    "참여 과제 세부 역할",
    "참여 과제명",
    "참여 연구원들과 공유할 수 있는 링크드인 주소가 있으면 알려주세요.",
    "참여 연구원들과 공유할 수 있는 깃헙 주소가 있으면 알려주세요",
    "GPU 계정 생성이 필요하신 분은 SSH public key를 알려주세요",
    "휴대폰 번호(숫자만)",
    "어떻게 프로젝트에 참여하게 됐나요?",
    "노션 권한 부여 완료",
    "슬랙 초대 완료",
    "Slurm 계정 생성 완료",
    "Deactivated",
    "ssh 키 제출 여부",
    "리얼월드 인턴",
    "NDA 작성 완료 여부",
    "slurm uid",
    "slurm/naver cloud  username",
    "로드맵 포함 여부",
    "3층 출입등록 여부",
    "Column 1",
]


def ext_row(**overrides) -> dict:
    values = {
        "성 + 이름 (한글 ex, 류형규)": "학생하나",
        "Email Address": "one@personal.invalid",
        "학교 이메일": "one@school.invalid",
        "소속 학교 연구실 지도교수님": "신진우 교수님",
        "참여 연구원들과 공유할 수 있는 깃헙 주소가 있으면 알려주세요": "student-one",
        "GPU 계정 생성이 필요하신 분은 SSH public key를 알려주세요": "ssh-rsa AAAAB3Nza",
        "휴대폰 번호(숫자만)": "01012345678",
        "slurm/naver cloud  username": "onestudent",
        "Deactivated": "",
        "리얼월드 인턴": "",
    }
    values.update(overrides)
    return {name: values.get(name, "") for name in EXT_HEADER}


def test_the_form_questions_are_read_as_the_columns_they_are() -> None:
    """Exact matching found none of them, and the tab read as 191 empty rows."""
    [record] = normalize_rows([ext_row()], source="roster_seed_ext")
    assert record["name"] == "학생하나"
    assert record["advisor"] == "신진우 교수님"
    assert record["email_school"] == "one@school.invalid"
    assert record["email_personal"] == "one@personal.invalid"
    assert record["github"] == "student-one"
    assert record["slurm_id"] == "onestudent"
    assert record["affiliation"] == "student"


def test_the_name_column_is_not_confused_with_the_slurm_name_questions() -> None:
    """Given Name and Last Name both say "한글 이름이 있는 경우" in their text.

    A bare "이름" fragment would capture those instead of the actual name
    column, and everybody would be called by their romanised first name.
    """
    from rlwrld_worklog.org.normalize import normalize_header

    assert normalize_header(EXT_HEADER[1]) == "name"
    assert normalize_header(EXT_HEADER[2]) is None
    assert normalize_header(EXT_HEADER[3]) is None


def test_a_phone_number_and_an_ssh_key_are_never_read() -> None:
    """Neither belongs in this system, and an allowlist can grow carelessly."""
    from rlwrld_worklog.org.normalize import normalize_header

    assert normalize_header("휴대폰 번호(숫자만)") is None
    assert normalize_header("GPU 계정 생성이 필요하신 분은 SSH public key를 알려주세요") is None

    [record] = normalize_rows([ext_row()], source="roster_seed_ext")
    assert "01012345678" not in str(record)
    assert "ssh-rsa" not in str(record)


def test_the_numeric_slurm_uid_is_not_mistaken_for_the_account_name() -> None:
    from rlwrld_worklog.org.normalize import normalize_header

    assert normalize_header("slurm uid") is None
    assert normalize_header("slurm/naver cloud  username") == "slurm_id"


def test_a_deactivated_student_is_retired_and_a_blank_box_is_not() -> None:
    """An unticked box is not a statement."""
    assert normalize_rows([ext_row(Deactivated="Y")], source="roster_seed_ext")[0][
        "status"
    ] == "retired"
    assert normalize_rows([ext_row()], source="roster_seed_ext")[0]["status"] == "active"


def test_an_intern_box_sets_the_employment_the_sheet_declares() -> None:
    ticked = normalize_rows([ext_row(**{"리얼월드 인턴": "Y"})], source="roster_seed_ext")[0]
    assert ticked["employment_type"] == "인턴"
    assert ticked["access_level"] == "limited"
    # Unticked says nothing, so access is unknown rather than restricted.
    blank = normalize_rows([ext_row()], source="roster_seed_ext")[0]
    assert blank["employment_type"] == ""
    assert blank["access_level"] == "unknown"


def test_a_students_slurm_account_becomes_an_identity() -> None:
    """It is how externals are identified at all -- HK, at the outset."""
    records = normalize_rows([ext_row()], source="roster_seed_ext")
    written = plan(records, observation_id=1)
    assert ("slurm", "onestudent") in written["identity"]
    assert ("github", "student-one") in written["identity"]


def test_the_form_tab_reads_end_to_end_from_a_real_workbook() -> None:
    """Title row, header row, then people -- the shape that actually failed."""
    from rlwrld_worklog.org.sheet import read_all

    data = _workbook(
        {
            "roster_seed_2": [["이름", "조직"], ["직원", "RLWRLD | Model Team"]],
            "roaster_seed_ext": [
                ["Virtual Lab 참여 연구원", "", ""],
                EXT_HEADER,
                [ext_row()[name] for name in EXT_HEADER],
                [ext_row(**{"성 + 이름 (한글 ex, 류형규)": "학생둘",
                            "소속 학교 연구실 지도교수님": "주한별 교수님"})[name]
                 for name in EXT_HEADER],
            ],
        }
    )
    by_tab = read_all(data)
    students = by_tab["roster_seed_ext"]
    assert [person["name"] for person in students] == ["학생하나", "학생둘"]
    assert [person["advisor"] for person in students] == ["신진우 교수님", "주한별 교수님"]


# --- Banding: how a node lists its own people (HK, 2026-09-14) -------------
#
# 조직도에서는 정규직 -> 계약직/인턴 으로 구분해서 표시해줘. 버추얼랩은 교수
# -> 학생 순으로. 학생 중에는 회사원이면서 학생도 있으니까, 회사원 표시를.

from rlwrld_worklog.org.chart import (  # noqa: E402
    GROUP_CONTRACT,
    GROUP_PROFESSOR,
    GROUP_REGULAR,
    GROUP_STUDENT,
    GROUP_UNKNOWN,
    annotate,
    build_tree,
    company_mark,
)


def _person(name, path, **fields):
    person = {"name": name, "chart_path": list(path)}
    person.update(fields)
    return person


def test_company_people_band_regular_before_contract() -> None:
    people = [
        _person("가", ["RLWRLD", "Model Team"], employment_type="인턴"),
        _person("나", ["RLWRLD", "Model Team"], employment_type="정규직"),
        _person("다", ["RLWRLD", "Model Team"], employment_type="계약직"),
        _person("라", ["RLWRLD", "Model Team"], employment_type="준정규직"),
    ]
    annotate(people)
    tree = build_tree(people)
    node = tree[0]["children"][0]
    assert [band["name"] for band in node["groups"]] == [GROUP_REGULAR, GROUP_CONTRACT]
    assert [person["name"] for person in node["members"]] == ["나", "라", "가", "다"]


def test_a_lab_lists_the_professor_before_the_students() -> None:
    people = [
        _person("학생1", ["RLWRLD Virtual Lab", "주한별 교수님"], affiliation="student"),
        _person("주한별", ["RLWRLD Virtual Lab", "주한별 교수님"], affiliation="professor"),
        _person("가학생", ["RLWRLD Virtual Lab", "주한별 교수님"], affiliation="student"),
    ]
    annotate(people)
    node = build_tree(people)[0]["children"][0]
    assert [band["name"] for band in node["groups"]] == [GROUP_PROFESSOR, GROUP_STUDENT]
    assert [person["name"] for person in node["members"]] == ["주한별", "가학생", "학생1"]


def test_a_student_who_is_also_on_the_payroll_is_marked() -> None:
    """회사원이면서 학생. The mark is the engagement, not a yes/no."""
    people = [
        _person("방문", ["RLWRLD Virtual Lab", "L"], affiliation="student", employment_type="방문 연구원"),
        _person("인턴", ["RLWRLD Virtual Lab", "L"], affiliation="student", employment_type="인턴"),
        _person("순수", ["RLWRLD Virtual Lab", "L"], affiliation="student", employment_type=""),
    ]
    annotate(people)
    marks = {person["name"]: person["company_mark"] for person in people}
    assert marks == {"방문": "방문 연구원", "인턴": "인턴", "순수": None}


def test_the_mark_tolerates_the_spacing_the_two_tabs_differ_on() -> None:
    assert company_mark({"employment_type": "방문연구원"}) == "방문 연구원"
    assert company_mark({"employment_type": "방문 연구원"}) == "방문 연구원"


def test_an_unstated_employment_type_is_its_own_band_not_a_contract() -> None:
    """Silence is not a demotion: 미상 is visible, and last."""
    people = [_person("무명", ["RLWRLD", "X"], employment_type="")]
    annotate(people)
    assert people[0]["member_group"] == GROUP_UNKNOWN
    assert build_tree(people)[0]["children"][0]["groups"] == [
        {"name": GROUP_UNKNOWN, "people": 1}
    ]


def test_the_renderer_titles_the_bands_and_shows_the_mark() -> None:
    from rlwrld_worklog.render import render_org_chart

    people = [
        _person("교수님", ["RLWRLD Virtual Lab", "L"], affiliation="professor"),
        _person("겸직", ["RLWRLD Virtual Lab", "L"], affiliation="student", employment_type="정규직"),
    ]
    annotate(people)
    html = render_org_chart({"tree": build_tree(people), "headcount": {}})
    assert GROUP_PROFESSOR in html
    assert GROUP_STUDENT in html
    assert "정규직" in html
