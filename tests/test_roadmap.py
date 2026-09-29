# hook-allow: synthetic-credentials
"""The roadmap payload, and the route that serves it.

`build_payload` is pure -- rows in, one dict out -- so the shape the screen
reads can be pinned without a database. The database half is exercised against
a real Postgres when `DATABASE_URL` is set and skipped otherwise, because a
test that quietly passes with no database behind it is worse than one that
says it was skipped.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterator

import pytest
from fastapi import HTTPException

from rlwrld_worklog import admin_web, roadmap, roadmap_web
from rlwrld_worklog.admin_web import SESSION_COOKIE


class FakeRequest:
    def __init__(self, cookies: dict[str, str] | None = None) -> None:
        self.cookies = cookies or {}
        self.headers: dict[str, str] = {}


TEAMS = [
    {"id": "rp", "label_ko": "Robotics Platform", "label_en": "Robotics Platform", "label_ja": "Robotics Platform"},
    {"id": "hw", "label_ko": "HW", "label_en": "HW", "label_ja": "HW"},
]
FAMILIES = [
    {"id": "RRP", "label_ko": "RRP", "label_en": "RRP", "label_ja": "RRP", "color": "#68a8ff"},
    {"id": "BENCH", "label_ko": "DexBench", "label_en": "DexBench", "label_ja": "DexBench", "color": "#ff9bd2"},
]
PRODUCTS = [
    {"id": 1, "name": "RRC", "family_id": "RRP", "owner_team_id": "rp", "detail_url": "https://example.invalid/rp"},
    # Deliberately owned by rp while an hw row uses it: a product spans teams.
    {"id": 2, "name": "Simulation", "family_id": "BENCH", "owner_team_id": "rp", "detail_url": None},
]


def item(**overrides: Any) -> dict[str, Any]:
    base = {
        "item_key": "1", "team_id": "rp", "product_id": 1, "horizon": "now", "kind": "dev",
        "text_ko": "가", "text_en": "a", "text_ja": "ア", "source_url": None,
        "hash": "abc1234567",
        "horizon_override": False, "product_override": False, "kind_override": False,
    }
    base.update(overrides)
    return base


def build(items: list[dict[str, Any]]) -> dict[str, Any]:
    return roadmap.build_payload(teams=TEAMS, families=FAMILIES, products=PRODUCTS, items=items)


# --- the payload ----------------------------------------------------------


def test_the_payload_carries_all_three_languages() -> None:
    payload = build([item()])
    assert payload["items"][0]["t"] == {"ko": "가", "en": "a", "ja": "ア"}


def test_the_team_comes_from_the_row_not_from_the_product() -> None:
    """The fact this schema exists to keep straight.

    `Simulation` is owned by Robotics Platform for roll-up purposes and is
    worked on by HW too. If the payload read the team off the product, that HW
    row would be filed under the wrong team on the by-team screen.
    """
    payload = build([item(item_key="2", team_id="hw", product_id=2)])
    row = payload["items"][0]
    assert row["team"] == "hw"
    assert row["prod"] == "Simulation"
    # The owner still shows up, as the label on the product's detail link.
    assert row["detLabel"]["ko"] == "Robotics Platform"


def test_a_product_without_a_detail_link_falls_back_to_the_official_page() -> None:
    payload = build([item(item_key="3", product_id=2)])
    assert payload["items"][0]["det"] == roadmap.OFFICIAL_SOURCE


def test_a_row_naming_a_product_that_is_gone_is_refused_not_dropped() -> None:
    """Dropping it would make the screen disagree with the count beside it."""
    with pytest.raises(ValueError) as refused:
        build([item(product_id=999)])
    assert "unknown product" in str(refused.value)


def test_overrides_are_reported_so_the_screen_can_grey_the_control() -> None:
    payload = build([item(horizon_override=True, kind_override=True)])
    assert payload["items"][0]["locked"] == ["horizon", "kind"]
    assert build([item()])["items"][0]["locked"] == []


def test_the_payload_keeps_the_shape_the_screen_already_read() -> None:
    payload = build([item()])
    for key in (
        "ui", "groups", "teamNotes", "horizons", "kinds",
        "teams", "families", "items", "history", "snapshot", "source", "generated",
    ):
        assert key in payload, key
    assert [h["id"] for h in payload["horizons"]] == ["now", "next", "soon", "someday"]
    assert [k["id"] for k in payload["kinds"]] == ["dev", "ops"]


def test_an_empty_roadmap_still_answers_with_a_snapshot_field() -> None:
    """"no snapshot" and "no field" are different, and only one is a bug."""
    payload = build([])
    assert payload["items"] == []
    assert payload["snapshot"] == {"id": "", "label": "", "prevUrl": None}


def test_the_hash_is_the_korean_original() -> None:
    assert roadmap.text_hash("가") == roadmap.text_hash("가")
    assert roadmap.text_hash("가") != roadmap.text_hash("나")
    assert len(roadmap.text_hash("가")) == 10


def test_history_is_grouped_by_snapshot_newest_first() -> None:
    from datetime import datetime, timezone

    newer = datetime(2026, 9, 29, tzinfo=timezone.utc)
    older = datetime(2026, 9, 28, tzinfo=timezone.utc)
    rows = [
        {"taken_at": newer, "label": "b", "prev_html_url": "./p2", "type": "changed",
         "team_id": "rp", "after_text": "새 본문", "before_text": "옛 본문"},
        {"taken_at": newer, "label": "b", "prev_html_url": "./p2", "type": "added",
         "team_id": "hw", "after_text": "추가", "before_text": None},
        {"taken_at": older, "label": "a", "prev_html_url": None, "type": "removed",
         "team_id": "rp", "after_text": None, "before_text": "삭제"},
    ]
    grouped = roadmap._group_history(rows)
    assert [len(entry["changes"]) for entry in grouped] == [2, 1]
    assert grouped[0]["prev"] == "./p2"
    assert grouped[1]["changes"][0] == {"type": "removed", "team": "rp", "text": "삭제"}


# --- the route ------------------------------------------------------------


@pytest.fixture()
def config_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    root = tmp_path / "config"
    monkeypatch.setenv("APP_CONFIG_ROOT", str(root))
    admin_web.store.cache_clear()
    yield root
    admin_web.store.cache_clear()


def session_for(role: str) -> FakeRequest:
    token, _ = admin_web.store().create_session(
        subject="someone", email="someone@rlwrld.ai", role=role, auth_method="google"
    )
    return FakeRequest({SESSION_COOKIE: token})


def test_the_roadmap_is_open_to_a_company_user(
    config_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first backoffice screen that is not the owner's."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://nobody@localhost:1/none")
    monkeypatch.setattr(roadmap_web, "_database_url", lambda: "sentinel")
    monkeypatch.setattr("rlwrld_worklog.roadmap.read_roadmap", lambda url: {"url": url})
    assert roadmap_web.roadmap_route(session_for("company_user")) == {"url": "sentinel"}


def test_a_session_that_is_not_signed_in_is_refused(config_root: Path) -> None:
    with pytest.raises(HTTPException) as refused:
        roadmap_web.roadmap_route(FakeRequest())
    assert refused.value.status_code == 401


def test_no_database_says_so_rather_than_answering_empty(
    config_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(HTTPException) as refused:
        roadmap_web.roadmap_route(session_for("super_admin"))
    assert refused.value.status_code == 503


# --- against a real database ---------------------------------------------

DATABASE_URL = os.environ.get("ROADMAP_TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not DATABASE_URL, reason="ROADMAP_TEST_DATABASE_URL is not set")


@needs_db
def test_a_seeded_roadmap_reads_back_identical() -> None:
    """The round trip the migration exists for."""
    import json

    source = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "roadmap_seed.json"
    payload = json.loads(source.read_text(encoding="utf-8"))
    counts = roadmap.seed_from_payload(DATABASE_URL, payload)
    assert counts["items"] == len(payload["items"])

    got = roadmap.read_roadmap(DATABASE_URL)
    before = {str(row["id"]): row for row in payload["items"]}
    after = {row["id"]: row for row in got["items"]}
    assert set(before) == set(after)
    for key, want in before.items():
        have = after[key]
        for field in ("team", "hz", "kind", "fam", "prod", "hash"):
            assert want[field] == have[field], (key, field)
        assert want["t"] == have["t"]


@needs_db
def test_seeding_twice_does_not_double_the_rows() -> None:
    import json

    source = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "roadmap_seed.json"
    payload = json.loads(source.read_text(encoding="utf-8"))
    roadmap.seed_from_payload(DATABASE_URL, payload)
    first = roadmap.read_roadmap(DATABASE_URL)
    roadmap.seed_from_payload(DATABASE_URL, payload)
    second = roadmap.read_roadmap(DATABASE_URL)
    assert len(first["items"]) == len(second["items"])
    assert len(first["families"]) == len(second["families"])


def test_missing_tables_name_the_migration_rather_than_500(
    config_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2026-09-29: this was an Internal Server Error, which says nothing."""
    import psycopg

    def boom(url: str) -> dict[str, Any]:
        raise psycopg.errors.UndefinedTable('relation "roadmap_team" does not exist')

    monkeypatch.setattr(roadmap_web, "_database_url", lambda: "sentinel")
    monkeypatch.setattr("rlwrld_worklog.roadmap.read_roadmap", boom)
    with pytest.raises(HTTPException) as refused:
        roadmap_web.roadmap_route(session_for("company_user"))
    assert refused.value.status_code == 503
    assert "0012_roadmap.sql" in refused.value.detail
    assert "roadmap seed" in refused.value.detail


def test_an_unreachable_database_says_that_instead(
    config_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import psycopg

    def boom(url: str) -> dict[str, Any]:
        raise psycopg.OperationalError("connection is bad")

    monkeypatch.setattr(roadmap_web, "_database_url", lambda: "sentinel")
    monkeypatch.setattr("rlwrld_worklog.roadmap.read_roadmap", boom)
    with pytest.raises(HTTPException) as refused:
        roadmap_web.roadmap_route(session_for("company_user"))
    assert refused.value.status_code == 503
    assert "not reachable" in refused.value.detail


# --- the mapping editor, against a real database --------------------------


@pytest.fixture()
def seeded() -> str:
    import json

    source = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "roadmap_seed.json"
    roadmap.seed_from_payload(DATABASE_URL, json.loads(source.read_text(encoding="utf-8")))
    return DATABASE_URL


@needs_db
def test_the_mapping_counts_are_computed_not_stored(seeded: str) -> None:
    """A product spans teams; a second place to write that down would drift."""
    mapping = roadmap.read_mapping(seeded)
    by_name = {product["name"]: product for product in mapping["products"]}
    simulation = by_name["Simulation"]
    assert sorted(simulation["teams"]) == ["hw", "rp"]
    assert simulation["items"] == simulation["dev"] + simulation["ops"]
    assert sum(product["items"] for product in mapping["products"]) == len(mapping["items"])


@needs_db
def test_changing_a_products_family_moves_every_row_with_it(seeded: str) -> None:
    mapping = roadmap.read_mapping(seeded)
    product = next(p for p in mapping["products"] if p["name"] == "RRC")
    roadmap.update_product(seeded, product["id"], {"family_id": "BENCH"})
    after = roadmap.read_roadmap(seeded)
    families = {row["fam"] for row in after["items"] if row["prod"] == "RRC"}
    assert families == {"BENCH"}


@needs_db
def test_a_product_cannot_be_moved_to_a_family_that_does_not_exist(seeded: str) -> None:
    product = roadmap.read_mapping(seeded)["products"][0]
    with pytest.raises(roadmap.RoadmapEditError) as refused:
        roadmap.update_product(seeded, product["id"], {"family_id": "NOPE"})
    assert "no such family or team" in str(refused.value)


@needs_db
def test_a_product_in_use_is_not_deleted(seeded: str) -> None:
    """A roadmap that loses rows quietly is what this screen exists to stop."""
    product = next(p for p in roadmap.read_mapping(seeded)["products"] if p["items"])
    with pytest.raises(roadmap.RoadmapEditError) as refused:
        roadmap.delete_product(seeded, product["id"])
    assert "still use this product" in str(refused.value)
    assert any(p["id"] == product["id"] for p in roadmap.read_mapping(seeded)["products"])


@needs_db
def test_an_unused_product_can_be_created_and_deleted(seeded: str) -> None:
    created = roadmap.create_product(
        seeded, {"name": "임시 제품", "family_id": "RRP", "owner_team_id": "rp"}
    )
    assert any(p["id"] == created["id"] for p in roadmap.read_mapping(seeded)["products"])
    roadmap.delete_product(seeded, created["id"])
    assert not any(p["id"] == created["id"] for p in roadmap.read_mapping(seeded)["products"])


@needs_db
def test_two_products_cannot_share_a_name(seeded: str) -> None:
    with pytest.raises(roadmap.RoadmapEditError) as refused:
        roadmap.create_product(seeded, {"name": "RRC", "family_id": "RRP", "owner_team_id": "rp"})
    assert "already exists" in str(refused.value)


@needs_db
def test_editing_an_item_raises_the_flag_that_protects_it(seeded: str) -> None:
    """The point of the whole schema: a hand-made correction survives a refresh."""
    item = roadmap.read_mapping(seeded)["items"][0]
    assert item["overrides"] == []
    changed = roadmap.update_item(seeded, item["key"], {"kind": "ops", "horizon": "someday"})
    assert changed["kind"] == "ops"
    assert changed["horizon"] == "someday"
    assert changed["overrides"] == ["horizon", "kind"]
    # And it is reported to the screen, so the control can say it was set by hand.
    payload = roadmap.read_roadmap(seeded)
    row = next(r for r in payload["items"] if r["id"] == item["key"])
    assert row["locked"] == ["horizon", "kind"]


@needs_db
def test_an_item_cannot_be_given_a_horizon_that_does_not_exist(seeded: str) -> None:
    item = roadmap.read_mapping(seeded)["items"][0]
    with pytest.raises(roadmap.RoadmapEditError) as refused:
        roadmap.update_item(seeded, item["key"], {"horizon": "someyear"})
    assert "horizon must be one of" in str(refused.value)


@needs_db
def test_moving_an_item_to_another_product_is_recorded_as_a_hand_move(seeded: str) -> None:
    mapping = roadmap.read_mapping(seeded)
    item = mapping["items"][0]
    other = next(p for p in mapping["products"] if p["id"] != item["product_id"])
    changed = roadmap.update_item(seeded, item["key"], {"product_id": other["id"]})
    assert changed["product_id"] == other["id"]
    assert "product" in changed["overrides"]


@needs_db
def test_an_unknown_field_is_refused_rather_than_ignored(seeded: str) -> None:
    item = roadmap.read_mapping(seeded)["items"][0]
    with pytest.raises(roadmap.RoadmapEditError) as refused:
        roadmap.update_item(seeded, item["key"], {"text_ko": "몰래 고치기"})
    assert "unknown fields" in str(refused.value)


@needs_db
def test_the_mapping_screen_is_the_owners(seeded: str, config_root: Path) -> None:
    with pytest.raises(HTTPException) as refused:
        roadmap_web.mapping_route(session_for("company_user"))
    assert refused.value.status_code == 403


def test_the_refresh_is_the_owners_not_the_companys(config_root: Path) -> None:
    """Reading the screen and rewriting its rows are different acts."""
    with pytest.raises(HTTPException) as refused:
        roadmap_web.refresh_route(session_for("company_user"))
    assert refused.value.status_code == 403


def test_a_refresh_without_a_notion_token_says_so(
    config_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = session_for("super_admin")
    session = admin_web.store().read_session(request.cookies[SESSION_COOKIE])
    request.headers["x-csrf-token"] = session["csrf"]
    with pytest.raises(HTTPException) as refused:
        roadmap_web.refresh_route(request)
    assert refused.value.status_code == 503
    assert "Notion token" in refused.value.detail


def test_a_page_that_returns_no_rows_changes_nothing(
    config_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Writing an empty refresh would delete the roadmap and call it deliberate."""
    admin_web.store().save_secret("notion_token", "ntn_synthetic_token_for_tests")
    request = session_for("super_admin")
    session = admin_web.store().read_session(request.cookies[SESSION_COOKIE])
    request.headers["x-csrf-token"] = session["csrf"]
    monkeypatch.setattr("rlwrld_worklog.roadmap_refresh.fetch_table_rows", lambda c, p: [])
    applied: list[Any] = []
    monkeypatch.setattr(
        "rlwrld_worklog.roadmap_refresh.apply_refresh",
        lambda *a, **k: applied.append(a) or {},
    )
    with pytest.raises(HTTPException) as refused:
        roadmap_web.refresh_route(request)
    assert refused.value.status_code == 502
    assert applied == [], "nothing may be written"
