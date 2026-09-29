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

DATABASE_URL = os.environ.get("ROADMAP_TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not DATABASE_URL, reason="ROADMAP_TEST_DATABASE_URL is not set")


def cell(text: str) -> list[dict[str, Any]]:
    return [{"plain_text": text}]


def table_row(block_id: str, *values: str) -> dict[str, Any]:
    return {"id": block_id, "type": "table_row", "table_row": {"cells": [cell(v) for v in values]}}


HEADER = table_row("head", "팀", "이번달", "다음달", "장기계획")


# --- reading the page -----------------------------------------------------


def test_a_sub_bullet_qualifies_the_line_above_it_rather_than_being_its_own() -> None:
    groups = refresh.split_cell("• RRC 신규\n    ◦ Task orchestrator\n    ◦ new session\n• HRDexDB")
    assert [len(group) for group in groups] == [3, 1]
    assert refresh.fold(groups[0]) == "RRC 신규 — Task orchestrator, new session"
    assert refresh.fold(groups[1]) == "HRDexDB"


def test_blank_lines_and_stray_whitespace_do_not_become_items() -> None:
    assert refresh.split_cell("\n\n   \n• 하나\n\n• 둘\n  \n") == [["하나"], ["둘"]]


def test_a_cell_without_bullets_is_still_read() -> None:
    """Infra's cell writes its headings as bare lines, not bullets."""
    assert refresh.split_cell("데이터\n운영") == [["데이터"], ["운영"]]


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

    class FakeClient:
        def iter_block_children(self, block_id: str) -> list[dict[str, Any]]:
            if block_id == "page":
                return [
                    {"id": "p1", "type": "paragraph"},
                    {"id": "t1", "type": "table"},
                    {"id": "t2", "type": "table"},
                ]
            return [table_row(f"{block_id}-row", "HW", "• 하나", "", "")] if block_id == "t1" else []

    rows = refresh.fetch_table_rows(FakeClient(), "page")
    assert [row["id"] for row in rows] == ["t1-row"]


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


def test_changed_words_under_the_same_key_are_a_change() -> None:
    diff = refresh.diff_items([parsed("a", "나")], [existing("a", "가")])
    assert [key for key, _ in diff.changed] == ["a"]
    assert not diff.added and not diff.removed


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
        [parsed("a", "나"), parsed("c", "다", team="hw")],
        [existing("a", "가"), existing("b", "사라짐", team="loop")],
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
