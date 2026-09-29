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
