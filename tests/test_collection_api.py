"""The 수집 현황 API: authenticated, read-only, and refuses paths.

Like ``test_work_api``, these call the route callables with a minimal request
stub, which still exercises authentication, argument validation and the
read-only contract the endpoints are responsible for.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

import pytest
from fastapi import HTTPException

from rlwrld_worklog import admin_web, collection_status, collection_web, work_web
from rlwrld_worklog.admin_web import SESSION_COOKIE
from rlwrld_worklog.collection_rules import ACTIVE_RULE_VERSION


class FakeRequest:
    def __init__(self, *, cookies: dict[str, str] | None = None) -> None:
        self.cookies = cookies or {}
        self.headers: dict[str, str] = {}


@pytest.fixture()
def archive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    archive_root = tmp_path / "archive"
    config_root = tmp_path / "config"
    (archive_root / "manifests" / "slack" / "production").mkdir(parents=True)
    (archive_root / "legacy" / "claude" / "weekly").mkdir(parents=True)
    monkeypatch.setenv("RAW_ARCHIVE_ROOT", str(archive_root))
    monkeypatch.setenv("LEDGER_ROOT", str(archive_root / "staging" / "ledger"))
    monkeypatch.setenv("APP_CONFIG_ROOT", str(config_root))
    monkeypatch.setenv("LEGACY_ROOT", str(archive_root / "legacy" / "claude" / "weekly"))
    admin_web.store.cache_clear()
    work_web.work_store.cache_clear()
    collection_status.clear_caches()
    manifest = {
        "schema_version": 2,
        "source": "slack",
        "environment": "production",
        "run_id": "20260901T000000Z-abcdef",
        "status": "success",
        "capture_profile": "live-slack-web-api/v1",
        "capture_density": "full",
        "dry_run": False,
        "started_at": "2026-09-01T00:00:00+00:00",
        "finished_at": "2026-09-01T00:05:00+00:00",
        "checkpoint_advanced": True,
        "files": [{"path": "raw/a", "compressed_bytes": 12}],
        "skips": [],
        "errors": [],
        "collection_rule_version": ACTIVE_RULE_VERSION,
    }
    (
        archive_root / "manifests" / "slack" / "production" / "20260901T000000Z-abcdef.json"
    ).write_text(json.dumps(manifest), encoding="utf-8")
    yield archive_root
    admin_web.store.cache_clear()
    work_web.work_store.cache_clear()
    collection_status.clear_caches()


@pytest.fixture()
def owner(archive: Path) -> FakeRequest:
    token, _ = admin_web.store().create_session(
        subject="owner", email="hyungkyu.ryu@rlwrld.ai", role="super_admin", auth_method="google"
    )
    return FakeRequest(cookies={SESSION_COOKIE: token})


@pytest.fixture()
def company_user(archive: Path) -> FakeRequest:
    token, _ = admin_web.store().create_session(
        subject="staff", email="staff@rlwrld.ai", role="company_user", auth_method="google"
    )
    return FakeRequest(cookies={SESSION_COOKIE: token})


ROUTES = (
    (collection_web.collection_rules, {}),
    (collection_web.collection_overview, {}),
    (collection_web.collection_runs, {}),
    (collection_web.collection_coverage, {}),
)


@pytest.mark.parametrize("route,kwargs", ROUTES)
def test_anonymous_callers_are_refused(archive: Path, route: Any, kwargs: dict[str, Any]) -> None:
    with pytest.raises(HTTPException) as error:
        route(FakeRequest(), **kwargs)
    assert error.value.status_code == 401


@pytest.mark.parametrize("route,kwargs", ROUTES)
def test_a_company_user_without_backoffice_rights_is_refused(
    company_user: FakeRequest, route: Any, kwargs: dict[str, Any]
) -> None:
    with pytest.raises(HTTPException) as error:
        route(company_user, **kwargs)
    assert error.value.status_code == 403


def test_the_super_administrator_sees_the_rule_registry(owner: FakeRequest) -> None:
    payload = collection_web.collection_rules(owner)
    assert payload["active_version"] == ACTIVE_RULE_VERSION
    assert payload["digests_pinned"] is True
    from rlwrld_worklog.collection_rules import RULES

    assert [rule["version"] for rule in payload["rules"]] == [r.version for r in RULES]
    for rule in payload["rules"]:
        assert rule["digest"].startswith("sha256:")
        assert rule["effective"]["basis"]


def test_the_overview_reports_the_archive_it_read(owner: FakeRequest, archive: Path) -> None:
    payload = collection_web.collection_overview(owner)
    assert payload["roots"]["archive_root"] == str(archive)
    assert [card["source"] for card in payload["cards"]] == [
        "slack",
        "notion",
        "google-calendar",
    ]
    assert payload["recent_runs"][0]["run_id"] == "20260901T000000Z-abcdef"
    assert payload["recent_runs"][0]["rule"]["attribution"] == "declared"


def test_runs_can_be_scoped_to_one_source_and_environment(owner: FakeRequest) -> None:
    payload = collection_web.collection_runs(owner, source="slack", environment="production")
    assert payload["returned"] == 1
    assert collection_web.collection_runs(owner, source="notion")["returned"] == 0


def test_coverage_defaults_to_the_last_thirty_kst_days(owner: FakeRequest) -> None:
    payload = collection_web.collection_coverage(owner)
    assert len(payload["rows"]) == collection_web.DEFAULT_COVERAGE_DAYS
    assert payload["timezone"].startswith("Asia/Seoul")
    assert set(payload["sources"]) == {"slack", "notion", "google_calendar"}


def test_coverage_accepts_an_explicit_range_and_grouping(owner: FakeRequest) -> None:
    payload = collection_web.collection_coverage(
        owner, start="2026-09-01", end="2026-09-03", source=["slack"], group="weekday"
    )
    assert payload["start"] == "2026-09-01" and payload["end"] == "2026-09-03"
    assert payload["sources"] == ["slack"]
    assert payload["group"] == "weekday"
    assert len(payload["weekday_rows"]) == 3


@pytest.mark.parametrize(
    "environment",
    ["../../etc", "..", "a/b", "/etc", ".ssh", "-x", "x" * 200, "spa ce"],
)
def test_an_environment_that_is_not_one_safe_segment_is_refused(
    owner: FakeRequest, environment: str
) -> None:
    for route in (collection_web.collection_overview, collection_web.collection_runs):
        with pytest.raises(HTTPException) as error:
            route(owner, environment=environment)
        assert error.value.status_code == 400
    with pytest.raises(HTTPException) as error:
        collection_web.collection_coverage(owner, environment=environment)
    assert error.value.status_code == 400


def test_an_unsupported_source_is_refused(owner: FakeRequest) -> None:
    with pytest.raises(HTTPException) as error:
        collection_web.collection_runs(owner, source="../../secrets")
    assert error.value.status_code == 400


@pytest.mark.parametrize("value", ["not-a-date", "2026-13-40", "2026/09/01", "../2026-09-01"])
def test_a_malformed_date_is_refused(owner: FakeRequest, value: str) -> None:
    with pytest.raises(HTTPException) as error:
        collection_web.collection_coverage(owner, start=value)
    assert error.value.status_code == 400


def test_every_reporting_route_is_a_read_only_get() -> None:
    """Reads never mutate. `refresh` is the one exception and it is a POST.

    It drops derived caches, which is server state, so it may not masquerade
    as a GET -- and it carries CSRF like every other mutation.
    """
    from rlwrld_worklog.web import app

    schema = app.openapi()["paths"]
    collection_paths = [path for path in schema if path.startswith("/api/v1/admin/collection")]
    assert sorted(collection_paths) == [
        "/api/v1/admin/collection/coverage",
        "/api/v1/admin/collection/overview",
        "/api/v1/admin/collection/refresh",
        "/api/v1/admin/collection/rules",
        "/api/v1/admin/collection/runs",
    ]
    for path in collection_paths:
        expected = {"post"} if path.endswith("/refresh") else {"get"}
        assert set(schema[path]) == expected, f"{path} must be {expected}"


def test_refresh_requires_a_super_administrator_and_a_csrf_token(
    archive: Path, owner: FakeRequest
) -> None:
    with pytest.raises(HTTPException) as anonymous:
        collection_web.collection_refresh(FakeRequest())
    assert anonymous.value.status_code == 401

    # The owner fixture carries no CSRF header.
    with pytest.raises(HTTPException) as missing_csrf:
        collection_web.collection_refresh(owner)
    assert missing_csrf.value.status_code == 403


def test_refresh_drops_only_the_caches_the_screen_uses(
    archive: Path, owner: FakeRequest
) -> None:
    token, csrf = admin_web.store().create_session(
        subject="owner", email="hyungkyu.ryu@rlwrld.ai", role="super_admin", auth_method="google"
    )
    request = FakeRequest(cookies={SESSION_COOKIE: token})
    request.headers = {"x-csrf-token": csrf}
    payload = collection_web.collection_refresh(request, screen="coverage")
    assert payload["screen"] == "coverage"
    assert set(payload["caches_cleared"]) == set(
        collection_status.CACHE_GROUPS["coverage"]
    )
    everything = collection_web.collection_refresh(request, screen="all")
    assert set(everything["caches_cleared"]) >= set(
        collection_status.CACHE_GROUPS["coverage"]
    )


def test_the_query_patterns_reject_a_path_before_the_handler_runs() -> None:
    from rlwrld_worklog.web import app

    parameters = {
        parameter["name"]: parameter
        for parameter in app.openapi()["paths"]["/api/v1/admin/collection/runs"]["get"][
            "parameters"
        ]
    }
    assert parameters["environment"]["schema"]["anyOf"][0]["pattern"] == (
        "^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$"
    )
    assert parameters["source"]["schema"]["anyOf"][0]["pattern"] == (
        "^(slack|notion|google-calendar)$"
    )


def test_an_unqualified_request_reports_production_not_every_environment(
    archive: Path, owner: FakeRequest
) -> None:
    """A smoke run must never fill in a gap in the production picture."""
    assert collection_web.DEFAULT_ENVIRONMENT == "production"
    assert collection_web._environment(None) == "production"
    assert collection_web._environment("") == "production"
    payload = collection_web.collection_overview(owner)
    assert payload["environment"] == "production"
    assert payload["environment_scope"] == "production"


def test_the_test_view_stays_reachable_but_only_when_asked_for(
    archive: Path, owner: FakeRequest
) -> None:
    assert collection_web._environment("all") is None
    assert collection_web._environment("test") == "test"
    widened = collection_web.collection_coverage(owner, environment="all")
    assert widened["environment"] is None
    assert widened["environment_scope"] == "all"
    scoped = collection_web.collection_coverage(owner, environment="test")
    assert scoped["environment_scope"] == "test"
