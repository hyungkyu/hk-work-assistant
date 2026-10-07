"""Refreshing the roadmap from Notion without undoing anybody.

The parsing and the diff are pure, so most of this needs no database and no
network. The write is exercised against a real Postgres when
`ROADMAP_TEST_DATABASE_URL` is set.
"""

from __future__ import annotations

import itertools
import os
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from rlwrld_worklog import roadmap, roadmap_refresh as refresh

# The integration run sets only WORKLOG_TEST_DATABASE_URL; reading only our own
# name meant these tests were skipped everywhere, the integration run included.
DATABASE_URL = os.environ.get("ROADMAP_TEST_DATABASE_URL") or os.environ.get("WORKLOG_TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not DATABASE_URL, reason="ROADMAP_TEST_DATABASE_URL / WORKLOG_TEST_DATABASE_URL is not set")


_ids = itertools.count()


def blk(kind: str, text: str = "", *children: dict[str, Any], struck: bool = False, id: str | None = None) -> dict[str, Any]:
    runs = [{"plain_text": text, "annotations": {"strikethrough": struck}}] if text else []
    block: dict[str, Any] = {"id": id or f"b{next(_ids)}", "type": kind, kind: {"rich_text": runs}}
    if children:
        block["children"] = list(children)
        block["has_children"] = True
    return block


def bullet(text: str, *children: dict[str, Any], **kw: Any) -> dict[str, Any]:
    return blk("bulleted_list_item", text, *children, **kw)


def section(title: str) -> dict[str, Any]:
    return blk("heading_3", title)


def period(title: str) -> dict[str, Any]:
    return blk("heading_4", title)


def para(text: str) -> dict[str, Any]:
    return blk("paragraph", text)


def filed(items: list[refresh.ParsedItem]) -> list[tuple[str, str, str]]:
    return [(item.team, refresh.horizon_for(item), item.text_ko) for item in items]


# --- reading the page -----------------------------------------------------


@pytest.mark.parametrize(
    "heading, written, expected",
    [
        ("2026/10 (이번달)", date(2026, 9, 30), "2026-10"),
        ("이번달 (10월)", date(2026, 9, 30), "2026-10"),
        ("다음달 (11월)", date(2026, 9, 30), "2026-11"),
        ("이번달", date(2026, 9, 1), "2026-09"),
        ("다음달", date(2026, 9, 2), "2026-10"),
        # Written at the very end of a month with no month named: the team means
        # the month that is about to start, as every 09-30 section spelled out.
        ("이번달", date(2026, 9, 30), "2026-10"),
        ("다음달", date(2026, 9, 30), "2026-11"),
        ("다음달", date(2026, 12, 2), "2027-01"),
        ("1월", date(2026, 12, 28), "2027-01"),
        ("장기계획", date(2026, 9, 1), "later"),
        ("장기계획 (12월 중심)", date(2026, 9, 30), "later"),
        ("메모", date(2026, 9, 30), None),
    ],
)
def test_a_period_heading_becomes_an_absolute_month(heading: str, written: date, expected: str | None) -> None:
    assert refresh.period_for(heading, written) == expected


def test_months_accumulate_and_the_newest_section_replaces_a_month() -> None:
    """HK, 2026-10-07: 9, 10, 11 and 향후, growing as the teams write."""
    blocks = [
        section("20260930 HW"),
        period("이번달 (10월)"), bullet("10월 갱신본"),
        period("다음달 (11월)"), bullet("11월 계획"),
        period("장기계획"), bullet("새 장기계획"),
        section("20260901 HW"),
        period("이번달"), bullet("9월 계획"),
        period("다음달"), bullet("9월에 적은 10월 예고"),
        period("장기계획"), bullet("옛 장기계획"),
    ]
    assert filed(refresh.parse_updates(blocks)) == [
        ("hw", "2026-09", "9월 계획"),
        ("hw", "2026-10", "10월 갱신본"),
        ("hw", "2026-11", "11월 계획"),
        ("hw", "later", "새 장기계획"),
    ]


def test_one_teams_newer_section_does_not_replace_another_teams_month() -> None:
    blocks = [
        section("20260930 HW"), period("이번달 (10월)"), bullet("HW 10월"),
        section("20260901 LOOP"), period("다음달"), bullet("LOOP 10월"),
    ]
    assert filed(refresh.parse_updates(blocks)) == [
        ("hw", "2026-10", "HW 10월"), ("loop", "2026-10", "LOOP 10월"),
    ]


def test_a_bullet_with_children_is_a_label_and_each_child_an_entry() -> None:
    """`• Desktop` over four things Desktop will do is four entries, as the seed has it."""
    blocks = [
        section("20260901 LOOP"), period("이번달"),
        bullet("Desktop", bullet("셋업 지원"), bullet("데이터 수집")),
        bullet("Cloud Inference"),
    ]
    assert [item.text_ko for item in refresh.parse_updates(blocks)] == [
        "Desktop — 셋업 지원", "Desktop — 데이터 수집", "Cloud Inference",
    ]


def test_a_third_level_folds_into_its_parent() -> None:
    blocks = [
        section("20260930 HW"), period("이번달 (10월)"),
        bullet("ODM 프로젝트", bullet("요구사항 정리", bullet("핸드 ↔ 손목 통합"), bullet("헤드 외장 디자인"))),
    ]
    assert [item.text_ko for item in refresh.parse_updates(blocks)] == [
        "ODM 프로젝트 — 요구사항 정리 — 핸드 ↔ 손목 통합, 헤드 외장 디자인",
    ]


def test_a_paragraph_heads_the_first_entry_under_it() -> None:
    blocks = [
        section("20260901 LOOP"), period("다음달"),
        para("고객에게 제공 가능한 버전 준비"), bullet("QA"), bullet("설치 도구"),
    ]
    assert [item.text_ko for item in refresh.parse_updates(blocks)] == [
        "고객에게 제공 가능한 버전 준비 — QA", "설치 도구",
    ]


def test_an_empty_slot_is_not_a_plan() -> None:
    """`Wheeled Humanoid:` with nothing after it, and an empty bullet, are slots."""
    blocks = [
        section("20260902 Robotics Platform"), period("이번달"),
        bullet("신규 HW Integration (일정이 아직 미정이라 추가하면서 업데이트 하겠습니다)",
               bullet("Wheeled Humanoid:"), bullet("Hand: "), bullet("")),
        bullet("RLHND", bullet("")),
    ]
    items = refresh.parse_updates(blocks)
    assert filed(items) == [
        ("rp", "undated", "신규 HW Integration (일정이 아직 미정이라 추가하면서 업데이트 하겠습니다)"),
        ("rp", "2026-09", "RLHND"),
    ]


def test_a_struck_line_is_done_and_a_struck_label_finishes_its_children() -> None:
    blocks = [
        section("20260930 HW"), period("이번달 (10월)"),
        bullet("ALDEX 엄지 설계", struck=True),
        bullet("HW Test", bullet("지그 구축", struck=True), bullet("파지력 측정")),
        bullet("HW Sourcing", bullet("Ruiyan Hand"), struck=True),
    ]
    assert [(item.text_ko, item.done) for item in refresh.parse_updates(blocks)] == [
        ("ALDEX 엄지 설계", True),
        ("HW Test — 지그 구축", True),
        ("HW Test — 파지력 측정", False),
        ("HW Sourcing — Ruiyan Hand", True),
    ]


def test_an_item_is_keyed_by_its_own_block() -> None:
    """A position in a cell shifted whenever a line was inserted above it."""
    blocks = [section("20260930 HW"), period("이번달 (10월)"), bullet("HW Test", bullet("지그", id="leaf-1"))]
    item = refresh.parse_updates(blocks)[0]
    assert (item.key, item.block_id) == ("leaf-1", "leaf-1")


def test_a_team_name_nobody_told_us_about_is_skipped_not_guessed() -> None:
    """A wrong team is worse than a missing one, and shows up as a loud gap."""
    blocks = [section("20260930 새로운 팀"), period("이번달 (10월)"), bullet("하나")]
    assert refresh.parse_updates(blocks) == []


def test_a_section_intro_and_an_unknown_period_are_not_items() -> None:
    blocks = [
        section("20260930 Robotics Platform"), para("4Q 로드맵 정리"),
        period("메모"), bullet("메모 줄"),
        period("2026/10 (이번달)"), bullet("하나"),
    ]
    assert filed(refresh.parse_updates(blocks)) == [("rp", "2026-10", "하나")]


def test_items_come_out_months_first_in_page_order() -> None:
    blocks = [
        section("20260930 HW"), period("장기계획"), bullet("장기"), period("다음달 (11월)"), bullet("11"),
        section("20260901 HW"), period("이번달"), bullet("9-a"), bullet("9-b"),
    ]
    items = refresh.parse_updates(blocks)
    assert [item.text_ko for item in items] == ["9-a", "9-b", "11", "장기"]
    assert [item.sort for item in items] == [0, 1, 2, 3]


class PagedClient:
    """Shaped like `NotionClient.iter_block_children`: it yields Notion's list
    responses, not blocks. A fake that yielded blocks is how a refresh that
    always read an empty page passed its tests (2026-09-30)."""

    def __init__(self) -> None:
        self.asked: list[str] = []

    def iter_block_children(self, block_id: str):
        self.asked.append(block_id)
        if block_id == "page":
            yield {"results": [para("안내"), blk("heading_2", "팀별 로드맵(202609)"),
                               {"id": "t1", "type": "table", "has_children": True}], "has_more": True}
            yield {"results": [blk("heading_2", "팀별 로드맵 갱신 내역"), section("20260930 HW"),
                               period("이번달 (10월)"),
                               {**bullet("HW Test"), "id": "parent", "has_children": True}],
                   "has_more": False}
        elif block_id == "parent":
            yield {"results": [bullet("지그 구축")], "has_more": False}
        else:
            yield {"results": [], "has_more": False}


def test_the_update_log_is_read_with_its_nested_bullets_and_the_table_is_not() -> None:
    client = PagedClient()
    items = refresh.read_items(client, "page")
    assert filed(items) == [("hw", "2026-10", "HW Test — 지그 구축")]
    assert "t1" not in client.asked, "the stale snapshot table is never opened"


def test_a_page_without_the_update_log_reads_as_empty() -> None:
    class Bare:
        def iter_block_children(self, block_id: str):
            yield {"results": [para("내용 없음")], "has_more": False}

    assert refresh.read_items(Bare(), "page") == []


# --- the diff -------------------------------------------------------------


def existing(key: str, text: str, team: str = "rp") -> dict[str, Any]:
    return {
        "item_key": key, "team_id": team, "product_id": 1, "horizon": "2026-09", "kind": "dev",
        "text_ko": text, "hash": roadmap.text_hash(text),
        "horizon_override": False, "product_override": False, "kind_override": False,
    }


def parsed(key: str, text: str, team: str = "rp") -> refresh.ParsedItem:
    return refresh.ParsedItem(key=key, team=team, column="2026-09", text_ko=text, block_id="b")


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
    later["horizon"] = "2026-10"
    item = refresh.ParsedItem(key="b:next:0", team="rp", column="2026-10", text_ko="HRDexDB — Object 추가", block_id="b")
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


def test_a_strike_added_to_an_unchanged_line_is_a_completion() -> None:
    item = parsed("a", "ALDEX 엄지 설계")
    item.done = True
    diff = refresh.diff_items([item], [existing("a", "ALDEX 엄지 설계")])
    assert diff.same == [("a", item)]
    assert [change.type for change in diff.as_changes()] == ["completed"]


def test_a_strike_taken_away_is_a_reopening() -> None:
    row = existing("a", "ALDEX 엄지 설계")
    row["done"] = True
    diff = refresh.diff_items([parsed("a", "ALDEX 엄지 설계")], [row])
    assert [change.type for change in diff.as_changes()] == ["reopened"]


def test_an_unchanged_line_records_no_change() -> None:
    diff = refresh.diff_items([parsed("a", "가")], [existing("a", "가")])
    assert diff.as_changes() == []


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
            key=f"blk:{row['hz']}:{index}", team=row["team"], column="2026-09",
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
    roadmap.update_item(seeded, item["key"], {"horizon": "undated"})

    incoming = [
        refresh.ParsedItem(
            key=item["key"], team=item["team"], column="2026-09",
            text_ko=item["text_ko"], block_id="blk",
        )
    ]
    refresh.apply_refresh(seeded, incoming, label="핀 확인")
    after = {row["key"]: row for row in roadmap.read_mapping(seeded)["items"]}
    assert after[item["key"]]["horizon"] == "undated"
    assert "horizon" in after[item["key"]]["overrides"]


@needs_db
def test_a_shift_down_the_cell_re_keys_without_a_collision_and_keeps_each_override(seeded: str) -> None:
    """A line inserted at the top hands each row the key its neighbour still
    holds. Keys are unique, so writing them one by one used to abort the refresh."""
    mapping = roadmap.read_mapping(seeded)
    first, second = mapping["items"][0], mapping["items"][1]
    roadmap.update_item(seeded, second["key"], {"horizon": "undated"})
    incoming = [
        refresh.ParsedItem(key="blk:now:0", team=first["team"], column="2026-09", text_ko="새로 끼운 줄", block_id="blk"),
        refresh.ParsedItem(key=second["key"], team=first["team"], column="2026-09", text_ko=first["text_ko"], block_id="blk"),
        refresh.ParsedItem(key="blk:now:2", team=second["team"], column="2026-09", text_ko=second["text_ko"], block_id="blk"),
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
            key=item["key"], team=item["team"], column="2026-09",
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
            key=keep["key"], team=keep["team"], column="2026-09",
            text_ko=keep["text_ko"] + " (고침)", block_id="blk",
        ),
        refresh.ParsedItem(key="blk:now:99", team="rp", column="2026-09", text_ko="새 줄", block_id="blk"),
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
        refresh.ParsedItem(key="blk:now:0", team="rp", column="2026-09", text_ko="처음 보는 줄", block_id="blk")
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
            key=target["id"], team=target["team"], column="2026-09",
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
        [refresh.ParsedItem(key="blk:now:0", team="rp", column="2026-09", text_ko="새 줄", block_id="blk")],
        label="신규 번역",
    )
    row = next(i for i in roadmap.read_roadmap(seeded)["items"] if i["t"]["ko"] == "새 줄")
    assert row["stale"] is True


@needs_db
def test_an_unchanged_line_moved_to_another_month_is_refiled(seeded: str) -> None:
    """Same block, same words, slipped from October to November. Writing nothing
    for an unchanged line would leave it in October."""
    item = roadmap.read_mapping(seeded)["items"][0]
    moved = refresh.ParsedItem(key=item["key"], team=item["team"], column="2026-11",
                               text_ko=item["text_ko"], block_id="blk")
    result = refresh.apply_refresh(seeded, [moved], label="이월")
    assert result["unchanged"] == 1
    after = {row["key"]: row for row in roadmap.read_mapping(seeded)["items"]}
    assert after[item["key"]]["horizon"] == "2026-11"


@needs_db
def test_a_struck_line_is_stored_done_and_said_in_the_history(seeded: str) -> None:
    item = roadmap.read_mapping(seeded)["items"][0]
    struck = refresh.ParsedItem(key=item["key"], team=item["team"], column="2026-09",
                                text_ko=item["text_ko"], block_id="blk", done=True)
    result = refresh.apply_refresh(seeded, [struck], label="완료")
    assert result["toggled"] == 1
    payload = roadmap.read_roadmap(seeded)
    row = next(r for r in payload["items"] if r["id"] == item["key"])
    assert row["done"] is True
    assert any(change["type"] == "completed" for change in payload["history"][0]["changes"])
