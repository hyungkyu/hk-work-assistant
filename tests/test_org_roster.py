

# ----------------------------------------------------------------- 사번

"""HK, 2026-10-12: 제일 첫줄에 사번을 넣었어.

The key has been broken twice by the same thing: it was derived from data
that moves. Names are shared -- two roster rows are both 류형규, which stopped
the nightly batch -- and emails are filled in late, which on 2026-10-08 turned
171 people into new people overnight. A 사번 is issued once and names one
person, so it goes first.

What these tests are really about is the migration. Making the new key win
outright would repeat 10-08 exactly, at a larger scale.
"""


def test_the_employee_number_column_is_read() -> None:
    from rlwrld_worklog.org.normalize import normalize_header

    for spelling in ("사번", "사원번호", "employee_no", "EMPLOYEE_ID"):
        assert normalize_header(spelling) == "employee_no", spelling


def test_a_number_typed_into_a_cell_is_the_same_as_one_typed_as_text() -> None:
    """Otherwise the column added to end duplication would create it.

    A spreadsheet hands back 1001 as the float 1001.0, so the same 사번
    would key two different people depending on how the cell was typed.
    """
    from rlwrld_worklog.org.normalize import normalize_rows

    rows = normalize_rows(
        [
            {"사번": "1001.0", "이름": "가", "email": "a@rlwrld.ai"},
            {"사번": " 1001 ", "이름": "나", "email": "b@rlwrld.ai"},
        ],
        source="roster_seed_2",
    )
    assert [row["employee_no"] for row in rows] == ["1001", "1001"]


def test_a_person_already_on_file_keeps_their_id_and_gains_the_number() -> None:
    """The whole point of the ordering: nobody is re-created by this change.

    On 2026-10-08 a new key won and 171 people left copies of themselves
    behind as 시트에 없음. An identity already on file is the stronger fact,
    so the 사번 attaches to the person who is already there.
    """
    from rlwrld_worklog.org.plan import resolve_person_id

    known = {("email_official", "hyungkyu.ryu@rlwrld.ai"): "p_existing"}
    found = resolve_person_id(
        {"employee_no": "1001", "email": "hyungkyu.ryu@rlwrld.ai", "name": "류형규"},
        known,
    )
    assert found == "p_existing"


def test_somebody_new_is_keyed_by_their_number() -> None:
    from rlwrld_worklog.org.plan import person_id

    with_number = person_id({"employee_no": "1001", "name": "류형규"})
    same_name = person_id({"employee_no": "1002", "name": "류형규"})
    assert with_number != same_name, "two people may share a name"


def test_the_number_outranks_an_email_that_was_reassigned() -> None:
    """A leaver's address handed to somebody new must not fold them together."""
    from rlwrld_worklog.org.plan import resolve_person_id

    known = {
        ("employee_no", "1002"): "p_new_joiner",
        ("email_official", "team@rlwrld.ai"): "p_leaver",
    }
    found = resolve_person_id(
        {"employee_no": "1002", "email": "team@rlwrld.ai", "name": "새사람"}, known
    )
    assert found == "p_new_joiner"


def test_the_roster_sheet_can_be_moved_without_a_patch(monkeypatch) -> None:
    """HK, 2026-10-12: 로스터 소스 파일 주소가 바뀌었다.

    A spreadsheet id is a deployment fact, and it had to be a code change.
    The cost of that is not the patch -- it is the hours between the sheet
    moving and the batch finding out, during which the sync reports success
    against a file nobody is editing any more.
    """
    import importlib

    from rlwrld_worklog.org import sheet

    monkeypatch.setenv("WORKLOG_ROSTER_SHEET_ID", "moved-somewhere-else")
    importlib.reload(sheet)
    try:
        assert sheet.ROSTER_SHEET_ID == "moved-somewhere-else"
    finally:
        monkeypatch.delenv("WORKLOG_ROSTER_SHEET_ID")
        importlib.reload(sheet)


def test_an_employee_number_is_a_declared_identity_kind() -> None:
    """The first sync against the new sheet died on this.

    `org_identity.kind` is an allowlist, which is what keeps a typo from
    becoming a table of junk -- so the fix is to declare the kind, not to
    drop the rule. The code that writes it shipped one patch ahead of the
    migration that permits it, which is the ordering that fails in
    production and nowhere else.
    """
    from pathlib import Path

    migrations = Path(__file__).resolve().parents[1] / "sql" / "migrations"
    allowed = "".join(
        path.read_text(encoding="utf-8")
        for path in sorted(migrations.glob("*.sql"))
        if "org_identity_kind_check" in path.read_text(encoding="utf-8")
    )
    assert "'employee_no'" in allowed

    from rlwrld_worklog.org.normalize import IDENTITY_FIELDS

    kinds = {kind for _, kind in IDENTITY_FIELDS}
    assert kinds <= {
        "employee_no",
        "email_official",
        "email_personal",
        "email_school",
        "github",
        "slack",
        "notion",
        "slurm",
    }, "a kind the database would refuse"
