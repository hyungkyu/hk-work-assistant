import pytest


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


@pytest.mark.skipif(
    not __import__("os").environ.get("WORKLOG_TEST_DATABASE_URL"),
    reason="set WORKLOG_TEST_DATABASE_URL to a throwaway database",
)
def test_a_person_on_both_tabs_is_marked_primary_and_concurrent() -> None:
    """HK, 2026-10-13: 양쪽에 소속일 경우 원소속 겸소속으로 표현해야할텐데.

    The company row is 원소속, the Virtual Lab row 겸소속, and each names the
    other place -- otherwise the same name on two nodes reads as a duplicate,
    which is exactly what the merge was done to stop it looking like. Built
    from a real chart, because the logic sits in the middle of `org_chart`.
    """
    import os
    import uuid
    from pathlib import Path

    import psycopg

    from rlwrld_worklog.ledger.load import apply_migrations
    from rlwrld_worklog.org.chart import org_chart

    url = os.environ["WORKLOG_TEST_DATABASE_URL"]
    apply_migrations(
        database_url=url,
        migrations_dir=Path(__file__).resolve().parents[1] / "sql" / "migrations",
        dry_run=False,
    )
    person = f"p_cc_{uuid.uuid4().hex[:8]}"
    alone = f"p_cc_{uuid.uuid4().hex[:8]}"
    # Left the lab, now on staff: a lab row marked absent. Not 겸직, and the
    # case that matters -- without the absent filter they would be called it.
    graduated = f"p_cc_{uuid.uuid4().hex[:8]}"
    # 정규직 on the company tab and in a lab: the company is home (HK: 명규).
    employed = f"p_cc_{uuid.uuid4().hex[:8]}"
    observations = []
    try:
        with psycopg.connect(url) as connection, connection.cursor() as cursor:
            for source, dept, affiliation in (
                ("roster_seed_2", "RLWRLD | Model Team", "internal"),
                ("roster_seed_ext", "KAIST", "student"),
            ):
                cursor.execute(
                    "INSERT INTO roster_observation (observed_at, source, row_count) "
                    "VALUES (now() + interval '3 days', %s, 1) RETURNING observation_id",
                    (source,),
                )
                observation = cursor.fetchone()[0]
                observations.append(observation)
                if source == "roster_seed_2":
                    cursor.execute(
                        "INSERT INTO org_person (person_id, name, first_seen, last_seen) "
                        "VALUES (%s,'겸직자',%s,%s), (%s,'한쪽만',%s,%s), (%s,'졸업생',%s,%s), "
                        "(%s,'정규직겸직',%s,%s)",
                        (
                            person, observation, observation,
                            alone, observation, observation,
                            graduated, observation, observation,
                            employed, observation, observation,
                        ),
                    )
                    for someone in (alone, graduated):
                        cursor.execute(
                            "INSERT INTO org_person_state (observation_id, person_id, affiliation, "
                            "access_level, status, department_raw) VALUES "
                            "(%s,%s,'internal','staff_equivalent','active',%s)",
                            (observation, someone, dept),
                        )
                else:
                    cursor.execute(
                        "INSERT INTO org_person_state (observation_id, person_id, affiliation, "
                        "access_level, status, department_raw) VALUES "
                        "(%s,%s,'student','limited','absent_from_sheet',%s)",
                        (observation, graduated, dept),
                    )
                for someone, engaged in ((person, "방문 연구원"), (employed, "정규직")):
                    cursor.execute(
                        "INSERT INTO org_person_state (observation_id, person_id, affiliation, "
                        "access_level, status, department_raw, advisor, employment_type) VALUES "
                        "(%s,%s,%s,'staff_equivalent','active',%s,%s,%s)",
                        (
                            observation, someone, affiliation, dept,
                            "주한별" if affiliation == "student" else None,
                            engaged if affiliation == "internal" else None,
                        ),
                    )
            connection.commit()

        chart = org_chart(url)

        def cards(tree):
            for node in tree:
                yield from node.get("members", [])
                yield from cards(node.get("children", []))

        everyone = [
            card for card in cards(chart["tree"])
            if card["person_id"] in (person, alone, graduated, employed)
        ]
        roles = sorted(
            (card["source"], card.get("affiliation_role")) for card in everyone if card["person_id"] == person
        )
        # A visiting researcher is a student first (HK, 2026-10-13: 인턴/방문
        # 연구원은 학교가 원소속) ...
        assert roles == [("roster_seed_2", "겸소속"), ("roster_seed_ext", "원소속")]
        # ... and somebody the company employs outright is the company's.
        employed_roles = sorted(
            (card["source"], card.get("affiliation_role"))
            for card in everyone
            if card["person_id"] == employed
        )
        assert employed_roles == [("roster_seed_2", "원소속"), ("roster_seed_ext", "겸소속")]
        assert all(
            card.get("affiliation_role") is None
            for card in everyone
            if card["person_id"] in (alone, graduated)
        ), "on one tab only -- including somebody who left the other -- is marked nothing"
        lab = next(
            card for card in everyone
            if card["person_id"] == person and card["source"] == "roster_seed_ext"
        )
        assert "Model Team" in lab["also_at"], "the lab card names the company place"
    finally:
        with psycopg.connect(url) as connection, connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM org_person_state WHERE person_id = ANY(%s)",
                ([person, alone, graduated, employed],),
            )
            cursor.execute(
                "DELETE FROM org_person WHERE person_id = ANY(%s)",
                ([person, alone, graduated, employed],),
            )
            cursor.execute(
                "DELETE FROM roster_observation WHERE observation_id = ANY(%s)", (observations,)
            )
            connection.commit()


def test_the_concurrent_mark_is_drawn_on_both_renderers() -> None:
    from rlwrld_worklog.render import _person_card

    primary = _person_card(
        {"name": "가", "affiliation": "internal", "affiliation_role": "원소속", "also_at": "KAIST | A Lab"}
    )
    concurrent = _person_card(
        {"name": "가", "affiliation": "student", "affiliation_role": "겸소속", "also_at": "Model Team"}
    )
    assert "원소속" in primary and "겸소속: KAIST | A Lab" in primary
    assert "겸소속" in concurrent and "원소속: Model Team" in concurrent

    from pathlib import Path

    script = (
        Path(__file__).resolve().parents[1] / "src" / "rlwrld_worklog" / "static" / "admin.js"
    ).read_text(encoding="utf-8")
    assert "person.affiliation_role" in script
