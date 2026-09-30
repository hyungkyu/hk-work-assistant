"""Refreshing the roadmap from Notion without undoing anybody.

The parsing and the diff are pure, so most of this needs no database and no
network. The write is exercised against a real Postgres when
`ROADMAP_TEST_DATABASE_URL` is set.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from rlwrld_worklog import roadmap, roadmap_refresh as refresh

# The integration run sets only WORKLOG_TEST_DATABASE_URL; reading only our own
# name meant these tests were skipped everywhere, the integration run included.
DATABASE_URL = os.environ.get("ROADMAP_TEST_DATABASE_URL") or os.environ.get("WORKLOG_TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not DATABASE_URL, reason="ROADMAP_TEST_DATABASE_URL / WORKLOG_TEST_DATABASE_URL is not set")


def cell(text: str) -> list[dict[str, Any]]:
    return [{"plain_text": text}]


def table_row(block_id: str, *values: str) -> dict[str, Any]:
    return {"id": block_id, "type": "table_row", "table_row": {"cells": [cell(v) for v in values]}}


HEADER = table_row("head", "팀", "이번달", "다음달", "장기계획")


# --- reading the page -----------------------------------------------------


def test_a_bullet_with_children_is_a_label_and_each_child_an_entry() -> None:
    """`• Desktop` over four things Desktop will do is four entries, as the seed has it."""
    entries = refresh.cell_entries("• RRC 신규\n    ◦ Task orchestrator\n    ◦ new session\n• HRDexDB")
    assert entries == ["RRC 신규 — Task orchestrator", "RRC 신규 — new session", "HRDexDB"]


def test_the_page_indent_and_a_hand_typed_indent_read_the_same() -> None:
    """The page indents by two and six; a hand-typed cell by nought and four."""
    page = refresh.cell_entries("  • Web\n      ◦ 데이터셋 조회\n  • 끝")
    typed = refresh.cell_entries("• Web\n    ◦ 데이터셋 조회\n• 끝")
    assert page == typed == ["Web — 데이터셋 조회", "끝"]


def test_a_third_level_folds_into_its_parent() -> None:
    entries = refresh.cell_entries("• RRC\n    ◦ Vision\n        ▪ 6D pose\n        ▪ HRDexDB 쿼리")
    assert entries == ["RRC — Vision — 6D pose, HRDexDB 쿼리"]


def test_a_heading_joins_the_first_entry_under_it() -> None:
    entries = refresh.cell_entries("고객에게 제공 가능한 버전 준비\n\n  • QA\n  • 설치 도구")
    assert entries == ["고객에게 제공 가능한 버전 준비 — QA", "설치 도구"]


def test_the_live_page_cell_that_came_back_empty_is_read() -> None:
    """Every bullet under a heading is indented. The old reader took each one
    for a sub-bullet of the heading and folded LOOP's whole month into one row."""
    text = (
        "내부에서 테스트 가능한 첫 버전 준비\n\n"
        "  • Desktop 과 Web 2가지 형태로 서비스 개발 진행\n"
        "  • 공통\n      ◦ Email/Password 기반 사용자 인증\n"
        "  • Web\n      ◦ 데이터셋 조회\n      ◦ 체크포인트 성능(성공률) 비교"
    )
    assert refresh.cell_entries(text) == [
        "내부에서 테스트 가능한 첫 버전 준비 — Desktop 과 Web 2가지 형태로 서비스 개발 진행",
        "공통 — Email/Password 기반 사용자 인증",
        "Web — 데이터셋 조회",
        "Web — 체크포인트 성능(성공률) 비교",
    ]


def test_blank_lines_and_stray_whitespace_do_not_become_items() -> None:
    assert refresh.cell_entries("\n\n   \n• 하나\n\n• 둘\n  \n") == ["하나", "둘"]


def test_a_cell_without_bullets_is_still_read() -> None:
    """A heading with nothing under it is an entry by itself."""
    assert refresh.cell_entries("데이터\n운영") == ["데이터", "운영"]


def test_the_table_becomes_one_row_per_bullet_per_column() -> None:
    rows = [HEADER, table_row("r1", "LOOP", "• 하나\n• 둘", "• 셋", "")]
    items = refresh.parse_table(rows)
    assert [(i.team, i.column, i.text_ko) for i in items] == [
        ("loop", "now", "하나"), ("loop", "now", "둘"), ("loop", "next", "셋"),
    ]
    assert items[0].key == "r1:now:0"
    assert items[0].block_id == "r1"


def test_a_team_name_nobody_told_us_about_is_skipped_not_guessed() -> None:
    """A wrong team is worse than a missing one, and shows up as a loud gap."""
    rows = [HEADER, table_row("r1", "새로운 팀", "• 하나", "", "")]
    assert refresh.parse_table(rows) == []


def test_the_long_column_splits_on_whether_a_quarter_is_written_down() -> None:
    rows = [HEADER, table_row("r1", "HW", "", "", "• 4Q 안에 끝낸다\n• 언젠가 한다")]
    items = refresh.parse_table(rows)
    assert [refresh.horizon_for(item) for item in items] == ["soon", "someday"]


def test_the_source_saying_the_date_is_undecided_means_someday() -> None:
    rows = [HEADER, table_row("r1", "HW", "• 신규 HW (일정이 아직 미정이라 추가하겠습니다)", "", "")]
    item = refresh.parse_table(rows)[0]
    assert refresh.horizon_for(item) == "someday"


def test_this_month_and_next_month_are_taken_as_written() -> None:
    rows = [HEADER, table_row("r1", "HW", "• 하나", "• 둘", "")]
    assert [refresh.horizon_for(i) for i in refresh.parse_table(rows)] == ["now", "next"]


def test_only_the_snapshot_table_is_read() -> None:
    """The write-ups below it are how the table got that way, not more rows."""

    rows = refresh.fetch_table_rows(PagedClient(), "page")
    assert [row["id"] for row in rows] == ["t1-row"]


class PagedClient:
    """Shaped like `NotionClient.iter_block_children`: it yields Notion's list
    responses, not blocks. A fake that yielded blocks is how a refresh that
    always read an empty page passed its tests (2026-09-30)."""

    def iter_block_children(self, block_id: str):
        if block_id == "page":
            yield {"results": [{"id": "p1", "type": "paragraph"}], "has_more": True}
            yield {"results": [{"id": "t1", "type": "table"}, {"id": "t2", "type": "table"}], "has_more": False}
        elif block_id == "t1":
            yield {"results": [table_row("t1-row", "HW", "• 하나", "", "")], "has_more": False}
        else:
            yield {"results": [], "has_more": False}


def test_the_table_is_found_on_a_later_page_of_the_listing() -> None:
    assert refresh.fetch_table_rows(PagedClient(), "page")[0]["id"] == "t1-row"


# --- the diff -------------------------------------------------------------


def existing(key: str, text: str, team: str = "rp") -> dict[str, Any]:
    return {
        "item_key": key, "team_id": team, "product_id": 1, "horizon": "now", "kind": "dev",
        "text_ko": text, "hash": roadmap.text_hash(text),
        "horizon_override": False, "product_override": False, "kind_override": False,
    }


def parsed(key: str, text: str, team: str = "rp") -> refresh.ParsedItem:
    return refresh.ParsedItem(key=key, team=team, column="now", text_ko=text, block_id="b")


def test_the_same_row_twice_is_not_a_change() -> None:
    diff = refresh.diff_items([parsed("a", "가")], [existing("a", "가")])
    assert (diff.added, diff.changed, diff.removed, diff.carried) == ([], [], [], [])


def test_reworded_words_under_the_same_key_are_a_change() -> None:
    diff = refresh.diff_items(
        [parsed("a", "시스템 성능, 오류 모니터링 환경 구성")],
        [existing("a", "시스템 성능·오류 모니터링 환경 구성")],
    )
    assert [key for key, _ in diff.changed] == ["a"]
    assert not diff.added and not diff.removed


def test_different_words_under_the_same_key_are_not_the_same_row() -> None:
    """The key is a position in a cell. Taking it on trust writes one item's
    text onto another's row and hands it that row's overrides."""
    diff = refresh.diff_items([parsed("a", "Fleet-Serve 실로봇 연동 테스트")], [existing("a", "인프라팀 연내 로드맵의 사내 공유")])
    assert [item.key for item in diff.added] == ["a"]
    assert [row["item_key"] for row in diff.removed] == ["a"]
    assert not diff.changed


def test_a_line_inserted_at_the_top_shifts_no_row_onto_its_neighbour() -> None:
    old = [existing("c:now:0", "Storage Quota 적용"), existing("c:now:1", "에이전트 친화적인 인프라 환경 제공")]
    new = [
        parsed("c:now:0", "계정·권한·사용량 관리 방식 설계"),
        parsed("c:now:1", "Storage Quota 적용"),
        parsed("c:now:2", "에이전트 친화적인 인프라 환경 제공"),
    ]
    diff = refresh.diff_items(new, old)
    assert sorted((old_key, item.key) for old_key, item in diff.carried) == [
        ("c:now:0", "c:now:1"), ("c:now:1", "c:now:2"),
    ]
    assert [item.text_ko for item in diff.added] == ["계정·권한·사용량 관리 방식 설계"]
    assert not diff.changed and not diff.removed


def test_a_label_in_front_is_still_the_same_row() -> None:
    """The seed wrote `데이터셋 조회`; the page nests it under `• Web`."""
    diff = refresh.diff_items([parsed("blk:now:3", "Web — 데이터셋 조회")], [existing("65", "데이터셋 조회")])
    assert [(key, item.key) for key, item in diff.changed] == [("65", "blk:now:3")]
    assert not diff.added and not diff.removed


def test_a_short_label_alone_does_not_claim_a_row() -> None:
    diff = refresh.diff_items([parsed("x", "HRDexDB")], [existing("a", "HRDexDB 쿼리를 통해 6D Pose 를 얻는 기능")])
    assert diff.added and diff.removed and not diff.changed


def test_rows_are_only_paired_within_a_team() -> None:
    diff = refresh.diff_items([parsed("x", "데이터셋 조회", team="loop")], [existing("a", "데이터셋 조회 기능", team="rp")])
    assert diff.added and diff.removed and not diff.changed


def test_between_two_alike_rows_the_one_in_the_same_column_wins() -> None:
    now = existing("1", "HRDexDB object 추가")
    later = existing("2", "Object 추가")
    later["horizon"] = "next"
    item = refresh.ParsedItem(key="b:next:0", team="rp", column="next", text_ko="HRDexDB — Object 추가", block_id="b")
    diff = refresh.diff_items([item], [now, later])
    assert [key for key, _ in diff.changed] == ["2"]


def test_the_history_keeps_what_a_changed_row_said_before() -> None:
    diff = refresh.diff_items([parsed("n", "Web — 데이터셋 조회")], [existing("65", "데이터셋 조회")])
    change = next(c for c in diff.as_changes() if c.type == "changed")
    assert (change.key, change.before, change.after) == ("n", "데이터셋 조회", "Web — 데이터셋 조회")


def test_a_new_key_with_the_same_words_is_carried_not_replaced() -> None:
    """The first refresh after the import re-keys every row. Without this it
    would report every one of them as a deletion and an addition, and take
    every override down with them."""
    diff = refresh.diff_items([parsed("block:now:0", "가")], [existing("7", "가")])
    assert diff.carried == [("7", diff.carried[0][1])]
    assert not diff.added and not diff.removed and not diff.changed


def test_a_row_that_is_gone_from_the_page_is_removed() -> None:
    diff = refresh.diff_items([], [existing("a", "가")])
    assert [row["item_key"] for row in diff.removed] == ["a"]


def test_a_row_the_page_gained_is_added() -> None:
    diff = refresh.diff_items([parsed("b", "나")], [existing("a", "가")])
    assert [item.key for item in diff.added] == ["b"]
    assert [row["item_key"] for row in diff.removed] == ["a"]


def test_two_rows_with_the_same_words_do_not_both_claim_one_old_row() -> None:
    diff = refresh.diff_items([parsed("x", "가"), parsed("y", "가")], [existing("a", "가")])
    assert len(diff.carried) == 1
    assert len(diff.added) == 1


def test_the_change_list_names_the_team_and_the_words() -> None:
    diff = refresh.diff_items(
        [parsed("a", "데이터셋 조회 (Web)"), parsed("c", "다", team="hw")],
        [existing("a", "데이터셋 조회"), existing("b", "사라짐", team="loop")],
    )
    kinds = {change.type for change in diff.as_changes()}
    assert kinds == {"changed", "added", "removed"}
    removed = next(c for c in diff.as_changes() if c.type == "removed")
    assert removed.team == "loop" and removed.before == "사라짐"


# --- writing it -----------------------------------------------------------


@pytest.fixture()
def seeded() -> str:
    import json

    source = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "roadmap_seed.json"
    roadmap.seed_from_payload(DATABASE_URL, json.loads(source.read_text(encoding="utf-8")))
    return DATABASE_URL


@needs_db
def test_a_refresh_that_matches_the_seed_changes_nothing_and_re_keys_quietly(seeded: str) -> None:
    before = roadmap.read_roadmap(seeded)
    incoming = [
        refresh.ParsedItem(
            key=f"blk:{row['hz']}:{index}", team=row["team"], column="now",
            text_ko=row["t"]["ko"], block_id="blk",
        )
        for index, row in enumerate(before["items"])
    ]
    result = refresh.apply_refresh(seeded, incoming, label="같은 내용")
    assert result["added"] == 0
    assert result["changed"] == 0
    assert result["removed"] == 0
    assert result["carried"] == len(before["items"])
    after = roadmap.read_roadmap(seeded)
    assert len(after["items"]) == len(before["items"])
    assert {row["t"]["ko"] for row in after["items"]} == {row["t"]["ko"] for row in before["items"]}


@needs_db
def test_a_refresh_leaves_a_hand_pinned_horizon_alone(seeded: str) -> None:
    """The promise the mapping screen makes."""
    mapping = roadmap.read_mapping(seeded)
    item = mapping["items"][0]
    roadmap.update_item(seeded, item["key"], {"horizon": "someday"})

    incoming = [
        refresh.ParsedItem(
            key=item["key"], team=item["team"], column="now",
            text_ko=item["text_ko"], block_id="blk",
        )
    ]
    refresh.apply_refresh(seeded, incoming, label="핀 확인")
    after = {row["key"]: row for row in roadmap.read_mapping(seeded)["items"]}
    assert after[item["key"]]["horizon"] == "someday"
    assert "horizon" in after[item["key"]]["overrides"]


@needs_db
def test_a_shift_down_the_cell_re_keys_without_a_collision_and_keeps_each_override(seeded: str) -> None:
    """A line inserted at the top hands each row the key its neighbour still
    holds. Keys are unique, so writing them one by one used to abort the refresh."""
    mapping = roadmap.read_mapping(seeded)
    first, second = mapping["items"][0], mapping["items"][1]
    roadmap.update_item(seeded, second["key"], {"horizon": "someday"})
    incoming = [
        refresh.ParsedItem(key="blk:now:0", team=first["team"], column="now", text_ko="새로 끼운 줄", block_id="blk"),
        refresh.ParsedItem(key=second["key"], team=first["team"], column="now", text_ko=first["text_ko"], block_id="blk"),
        refresh.ParsedItem(key="blk:now:2", team=second["team"], column="now", text_ko=second["text_ko"], block_id="blk"),
    ]
    refresh.apply_refresh(seeded, incoming, label="밀림")
    after = {row["text_ko"]: row for row in roadmap.read_mapping(seeded)["items"]}
    assert after[first["text_ko"]]["key"] == second["key"]
    assert after[second["text_ko"]]["key"] == "blk:now:2"
    assert "horizon" in after[second["text_ko"]]["overrides"]
    assert "horizon" not in after[first["text_ko"]]["overrides"]


@needs_db
def test_a_refresh_never_moves_a_row_between_products(seeded: str) -> None:
    """The source has no opinion about products, so a refresh must not either."""
    mapping = roadmap.read_mapping(seeded)
    item = mapping["items"][0]
    incoming = [
        refresh.ParsedItem(
            key=item["key"], team=item["team"], column="now",
            text_ko=item["text_ko"] + " (수정)", block_id="blk",
        )
    ]
    refresh.apply_refresh(seeded, incoming, label="제품 불변")
    after = {row["key"]: row for row in roadmap.read_mapping(seeded)["items"]}
    assert after[item["key"]]["product_id"] == item["product_id"]
    assert after[item["key"]]["kind"] == item["kind"]


@needs_db
def test_the_change_history_comes_back_to_the_screen(seeded: str) -> None:
    mapping = roadmap.read_mapping(seeded)
    keep, drop = mapping["items"][0], mapping["items"][1]
    incoming = [
        refresh.ParsedItem(
            key=keep["key"], team=keep["team"], column="now",
            text_ko=keep["text_ko"] + " (고침)", block_id="blk",
        ),
        refresh.ParsedItem(key="blk:now:99", team="rp", column="now", text_ko="새 줄", block_id="blk"),
    ]
    result = refresh.apply_refresh(seeded, incoming, label="이력", prev_html_url="./history/x.html")
    assert result["changed"] == 1 and result["added"] == 1
    assert result["removed"] == len(mapping["items"]) - 1

    history = roadmap.read_roadmap(seeded)["history"]
    assert history, "the drawer reads this"
    newest = history[0]
    assert newest["prev"] == "./history/x.html"
    types = {change["type"] for change in newest["changes"]}
    assert {"added", "changed", "removed"} <= types
    assert any(change["text"] == "새 줄" for change in newest["changes"])
    assert any(drop["text_ko"] == change["text"] for change in newest["changes"])


@needs_db
def test_a_row_born_in_a_refresh_lands_somewhere_visible(seeded: str) -> None:
    """Guessing its product would be a wrong answer dressed as a right one."""
    incoming = [
        refresh.ParsedItem(key="blk:now:0", team="rp", column="now", text_ko="처음 보는 줄", block_id="blk")
    ]
    refresh.apply_refresh(seeded, incoming, label="신규")
    payload = roadmap.read_roadmap(seeded)
    row = next(item for item in payload["items"] if item["t"]["ko"] == "처음 보는 줄")
    assert row["prod"] == "미분류"


@needs_db
def test_a_changed_row_says_its_translation_is_behind(seeded: str) -> None:
    """English that reads as current and is not is worse than English missing."""
    before = roadmap.read_roadmap(seeded)
    assert not any(row["stale"] for row in before["items"]), "the seed is translated"

    target = before["items"][0]
    incoming = [
        refresh.ParsedItem(
            key=target["id"], team=target["team"], column="now",
            text_ko=target["t"]["ko"] + " (원문 수정)", block_id="blk",
        )
    ]
    refresh.apply_refresh(seeded, incoming, label="번역 확인")
    after = roadmap.read_roadmap(seeded)
    row = next(item for item in after["items"] if item["id"] == target["id"])
    assert row["stale"] is True
    assert row["t"]["ko"].endswith("(원문 수정)")
    # The old English is kept rather than replaced with Korean: it is still
    # the best English there is, it is just behind.
    assert row["t"]["en"] == target["t"]["en"]


@needs_db
def test_a_row_born_in_a_refresh_is_marked_untranslated(seeded: str) -> None:
    refresh.apply_refresh(
        seeded,
        [refresh.ParsedItem(key="blk:now:0", team="rp", column="now", text_ko="새 줄", block_id="blk")],
        label="신규 번역",
    )
    row = next(i for i in roadmap.read_roadmap(seeded)["items"] if i["t"]["ko"] == "새 줄")
    assert row["stale"] is True
