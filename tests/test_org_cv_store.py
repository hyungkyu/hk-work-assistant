"""인사정보 상세 in the database (HK, 2026-10-14): 원본 -> 디비 -> 화면.

Run inside a rolled-back transaction.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("WORKLOG_TEST_DATABASE_URL"),
    reason="set WORKLOG_TEST_DATABASE_URL to a throwaway database",
)


@pytest.fixture()
def cursor():
    import psycopg

    from rlwrld_worklog.ledger.load import apply_migrations

    url = os.environ["WORKLOG_TEST_DATABASE_URL"]
    apply_migrations(
        database_url=url,
        migrations_dir=Path(__file__).resolve().parents[1] / "sql" / "migrations",
        dry_run=False,
    )
    connection = psycopg.connect(url)
    connection.autocommit = False
    try:
        yield connection.cursor()
    finally:
        connection.rollback()
        connection.close()


def _number() -> str:
    return f"9{uuid.uuid4().int % 10**8:08d}"


def _write(root: Path, number: str, **data) -> None:
    (root / f"{number}.json").write_text(json.dumps({"employee_no": number, **data}, ensure_ascii=False))


def test_the_screen_reads_the_database_and_an_edit_survives_a_reread(cursor, tmp_path) -> None:
    from rlwrld_worklog.org.cv_store import edit_row, load_if_newer, read_profile

    number = _number()
    _write(tmp_path, number, education=[
        {"level": "대학교", "school": "건국대학교", "major": "지리학", "from": "2006.03", "to": "2011.01"},
        {"school": "안산동산고등학교", "from": "2004.03", "to": "2006.02"}])
    assert load_if_newer(cursor, [number], tmp_path) == [number]
    found = read_profile(cursor, [number])
    assert [item["level"] for item in found["education"]] == ["고등학교", "대학교"], "고등학교 first"
    university = found["education"][1]
    assert university["school"]["name"] == "건국대학교" and university["date_from"] == "2006.03"

    edit_row(cursor, "cv_education", university["row_id"], "major", "지리학 (지리교육 복수전공)")
    _write(tmp_path, number, education=[{"level": "대학교", "school": "건국대학교", "major": "틀리게 읽음"}])
    os.utime(tmp_path / f"{number}.json", (2e9, 2e9))
    assert load_if_newer(cursor, [number], tmp_path) == [], "an edited person is not reloaded"
    assert read_profile(cursor, [number])["education"][1]["major"] == "지리학 (지리교육 복수전공)"


def test_merging_two_companies_shows_both_people_under_one_name(cursor, tmp_path) -> None:
    from rlwrld_worklog.org.cv_store import load_if_newer, merge_entity, read_profile, related

    tag = uuid.uuid4().hex[:6]
    a, b = _number(), _number()
    _write(tmp_path, a, experience=[{"org": f"삼성전자{tag}", "title": "엔지니어", "from": "2004.06", "to": "2009.11"}])
    _write(tmp_path, b, experience=[{"org": f"삼성{tag}", "title": "연구원", "from": "2008.01", "to": "2012.01"}])
    load_if_newer(cursor, [a, b], tmp_path)
    electronics = read_profile(cursor, [a])["experience"][0]["org"]
    samsung = read_profile(cursor, [b])["experience"][0]["org"]
    assert len(related(cursor, electronics["id"])["members"]) == 1

    merge_entity(cursor, electronics["id"], samsung["id"])
    shown = read_profile(cursor, [a])["experience"][0]["org"]
    assert shown["name"] == f"삼성{tag}" and shown["read_as"] == f"삼성전자{tag}", "merged, original kept"
    members = related(cursor, samsung["id"])["members"]
    assert {m["employee_no"] for m in members} == {a, b}


def test_only_listed_fields_can_be_edited(cursor, tmp_path) -> None:
    from rlwrld_worklog.org.cv_store import edit_row, load_if_newer, read_profile

    number = _number()
    _write(tmp_path, number, education=[{"level": "대학교", "school": "X대학교"}])
    load_if_newer(cursor, [number], tmp_path)
    row = read_profile(cursor, [number])["education"][0]["row_id"]
    with pytest.raises(ValueError):
        edit_row(cursor, "cv_education", row, "employee_no", "1")
    with pytest.raises(ValueError):
        edit_row(cursor, "cv_education", row, "level", "유치원")


def test_a_bundle_carries_many_people_and_a_newer_one_wins(cursor, tmp_path) -> None:
    from rlwrld_worklog.org.cv_store import load_if_newer, read_profile

    a, b = _number(), _number()
    (tmp_path / "bundle-all.json").write_text(json.dumps([
        {"employee_no": a, "headline": "묶음 A"}, {"employee_no": b, "headline": "묶음 B"}],
        ensure_ascii=False))
    assert sorted(load_if_newer(cursor, [a, b], tmp_path)) == sorted([a, b])
    assert read_profile(cursor, [b])["headline"] == "묶음 B"
    _write(tmp_path, a, headline="낱개 A")
    os.utime(tmp_path / f"{a}.json", (2e9, 2e9))
    assert load_if_newer(cursor, [a], tmp_path) == [a]
    assert read_profile(cursor, [a])["headline"] == "낱개 A"
