"""The organisation routes: authenticated, read-only, and honest about gaps.

These call the route callables with the same minimal request stub the other
API suites use, which still exercises authentication, argument validation and
the read-only contract — the part these routes are responsible for.

The contract worth stating: nothing here generates. The org chart changes when
the roster sync runs -- on the daily batch, or when a person presses 새로고침
(HK, 2026-10-07) -- and a person's day changes when the digest batch builds it
(HK, 2026-09-11: 이건 코드여야지, 네가 하면 안됨). The two mutations are a
person answering whose an unknown account is, and a person asking for that
sync now.
"""

from __future__ import annotations

import os
from typing import Any

import pytest
from fastapi import HTTPException

from rlwrld_worklog import admin_web, org_web
from rlwrld_worklog.admin_web import SESSION_COOKIE


class FakeRequest:
    def __init__(self, *, cookies: dict[str, str] | None = None, headers=None) -> None:
        self.cookies = cookies or {}
        self.headers: dict[str, str] = headers or {}


@pytest.fixture()
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_CONFIG_ROOT", str(tmp_path / "config"))
    yield tmp_path


@pytest.fixture()
def owner(config) -> FakeRequest:
    token, _ = admin_web.store().create_session(
        subject="owner", email="hyungkyu.ryu@rlwrld.ai", role="super_admin", auth_method="google"
    )
    return FakeRequest(cookies={SESSION_COOKIE: token})


ROUTES = (
    (org_web.org_chart_route, {}),
    (org_web.refresh_route, {}),
    (org_web.person_profile_route, {"person_id": "p_000000000000"}),
    (org_web.org_status_route, {}),
    (org_web.unmapped_route, {}),
    (org_web.digest_status_route, {}),
    (org_web.person_day_route, {"person_id": "p_x", "day": "2026-09-10"}),
)


@pytest.mark.parametrize("route,kwargs", ROUTES)
def test_anonymous_callers_are_refused(config, route: Any, kwargs: dict[str, Any]) -> None:
    with pytest.raises(HTTPException) as error:
        route(FakeRequest(), **kwargs)
    assert error.value.status_code == 401


def test_an_unconfigured_database_is_said_not_shown_as_emptiness(
    owner: FakeRequest, monkeypatch
) -> None:
    """"Not configured" and "nobody works here" must never look the same."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(HTTPException) as error:
        org_web.org_chart_route(owner)
    assert error.value.status_code == 503
    assert "DATABASE_URL" in error.value.detail


def test_resolving_needs_a_person_or_an_explicit_judgement(
    owner: FakeRequest, monkeypatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://unreachable/nowhere")
    monkeypatch.setattr(org_web, "_require_csrf", lambda *_args, **_kwargs: None)
    payload = org_web.ResolveRequest(kind="slurm", value="someone")
    with pytest.raises(HTTPException) as error:
        org_web.resolve_route(owner, payload)
    assert error.value.status_code == 400


def test_a_refresh_without_a_google_token_says_where_to_set_it(
    owner: FakeRequest, monkeypatch
) -> None:
    """Checked before Drive is called: a missing token is a setup gap, not an outage."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://unreachable/nowhere")
    monkeypatch.setattr(org_web, "_require_csrf", lambda *_args, **_kwargs: None)
    with pytest.raises(HTTPException) as error:
        org_web.refresh_route(owner)
    assert error.value.status_code == 503
    assert "연결" in error.value.detail


def test_the_chart_names_the_sheet_it_comes_from(owner: FakeRequest, monkeypatch) -> None:
    from rlwrld_worklog.org import chart
    from rlwrld_worklog.org.sheet import ROSTER_SHEET_ID

    monkeypatch.setenv("DATABASE_URL", "postgresql://unreachable/nowhere")
    monkeypatch.setattr(chart, "org_chart", lambda *_args, **_kwargs: {"tree": [], "people": 0})
    body = org_web.org_chart_route(owner)
    assert body["roster_url"] == f"https://docs.google.com/spreadsheets/d/{ROSTER_SHEET_ID}/edit"


def test_every_reading_route_is_a_get_and_only_resolve_is_a_post() -> None:
    """A screen that could regenerate its own contents would break the protocol.

    Refresh is a deliberate exception: a person pressing a button, CSRF and
    all, to run the same sync the batch runs. Building a day is the second
    (HK, 2026-10-12: 클릭하면 디비에서 실시간 생성하면 되지 않아?) -- the same
    build the nightly batch runs, owner-only, CSRF, for a day not yet built.
    """
    from rlwrld_worklog.web import app

    schema = app.openapi()["paths"]
    org_paths = {
        path: sorted(methods) for path, methods in schema.items() if path.startswith("/api/v1/admin/org")
    }
    assert org_paths == {
        "/api/v1/admin/org/chart": ["get"],
        "/api/v1/admin/org/refresh": ["post"],
        "/api/v1/admin/org/person/{person_id}": ["get"],
        "/api/v1/admin/org/status": ["get"],
        "/api/v1/admin/org/unmapped": ["get"],
        "/api/v1/admin/org/unmapped/resolve": ["post"],
        # The same answer as above, for the unambiguous ones at once (HK,
        # 2026-10-13): one email match to one person, or a source's own bot flag.
        "/api/v1/admin/org/unmapped/resolve-bulk": ["post"],
        "/api/v1/admin/org/digest/status": ["get"],
        "/api/v1/admin/org/digest/{person_id}": ["get"],
        "/api/v1/admin/org/digest/{person_id}/range": ["get"],
        "/api/v1/admin/org/digest/build": ["post"],
        # 인사정보 상세: who else was at a company, school, lab, project or paper.
        "/api/v1/admin/org/cv/related/{entity_id}": ["get"],
        "/api/v1/admin/org/cv/entities": ["get"],
        # Correcting what was read from the original, and merging names
        # (HK, 2026-10-14): owner only, CSRF, each a person's own act.
        "/api/v1/admin/org/cv/edit": ["post"],
        "/api/v1/admin/org/cv/entity": ["post"],
    }


REQUIRES_DATABASE = pytest.mark.skipif(
    not os.environ.get("WORKLOG_TEST_DATABASE_URL"),
    reason="set WORKLOG_TEST_DATABASE_URL to a throwaway database",
)


@REQUIRES_DATABASE
def test_a_day_with_no_digest_says_it_was_not_built(owner: FakeRequest, monkeypatch) -> None:
    """Not built and nothing happened are different facts.

    Showing them the same way would make an unfinished backfill look like a
    quiet week, which is exactly the kind of false calm this system keeps
    finding in itself.
    """
    monkeypatch.setenv("DATABASE_URL", os.environ["WORKLOG_TEST_DATABASE_URL"])
    from rlwrld_worklog.ledger.load import apply_migrations
    from pathlib import Path

    repository = Path(__file__).resolve().parents[1]
    apply_migrations(
        database_url=os.environ["WORKLOG_TEST_DATABASE_URL"],
        migrations_dir=repository / "sql" / "migrations",
        dry_run=False,
    )
    # A day no test's digests cover. 2026-09-10 used to be used here, and it
    # is covered by other tests' rows -- which, once a quiet day stopped
    # reading as unbuilt (2026-10-13), correctly made it a quiet day.
    body = org_web.person_day_route(owner, person_id="p_nobody", day="2031-06-01")
    assert body == {"person_id": "p_nobody", "day": "2031-06-01", "built": False, "quiet": False}


@REQUIRES_DATABASE
def test_a_person_profile_shows_the_roster_and_its_accounts_only_to_the_owner(
    owner: FakeRequest, monkeypatch
) -> None:
    """인사정보 from the roster, the other two sections declared but unconnected.

    The accounts -- emails, 사번, Slack, GitHub -- used to be withheld from
    everybody. HK, 2026-10-13: 인사 정보에 슬랙, 깃헙, 노션, 슬럼, 회사 이메일
    등 모든 로스터 정보를 보여줘, and who may see them: 나만. So the owner
    gets them and a colleague, even with the chart opened to them, still
    receives no join key at all.
    """
    import io
    import json as _json

    import openpyxl

    from rlwrld_worklog.ledger.load import apply_migrations
    from rlwrld_worklog.org.plan import person_id
    from rlwrld_worklog.org.sync import sync_workbook
    from pathlib import Path

    url = os.environ["WORKLOG_TEST_DATABASE_URL"]
    monkeypatch.setenv("DATABASE_URL", url)
    repository = Path(__file__).resolve().parents[1]
    apply_migrations(database_url=url, migrations_dir=repository / "sql" / "migrations", dry_run=False)

    def workbook(team: str) -> bytes:
        book = openpyxl.Workbook()
        book.remove(book.active)
        internal = book.create_sheet("roster_seed_2")
        internal.append(["이름", "닉네임", "조직", "고용형태", "재직구분", "email (official)"])
        internal.append(["프로필시험", "pt", team, "인턴", "", "profile.test@rlwrld.ai"])
        external = book.create_sheet("roaster_seed_ext")
        external.append(["성 + 이름", "소속 학교 연구실 지도교수님"])
        external.append(["프로필학생", "주한별 교수님"])
        buffer = io.BytesIO()
        book.save(buffer)
        return buffer.getvalue()

    sync_workbook(url, workbook("RLWRLD | Model Team"), reobserve=True)
    sync_workbook(url, workbook("RLWRLD | Platform Team"), reobserve=True)
    identifier = person_id({"email": "profile.test@rlwrld.ai"})

    body = org_web.person_profile_route(owner, person_id=identifier)
    fields = {field["key"]: field["value"] for field in body["hr"]["fields"]}
    assert fields["department_raw"] == "RLWRLD | Platform Team"
    assert fields["nickname"] == "pt" and fields["status"] == "재직"
    moved = [entry for entry in body["hr"]["history"] if not entry["first"]]
    assert moved and moved[-1]["changes"][0]["after"] == "RLWRLD | Platform Team"
    for section in (body["hr"]["extra"], body["recent_work"], body["detail"]):
        assert section["connected"] is False

    accounts = {group["kind"]: group for group in body["accounts"]}
    assert accounts["email_official"]["label"] == "회사 이메일"
    assert [item["value"] for item in accounts["email_official"]["values"]] == [
        "profile.test@rlwrld.ai"
    ]

    # A colleague who may open the chart: same panel, no accounts, no '@'.
    monkeypatch.setattr(
        org_web, "require_page_access", lambda page, **_: (lambda request: {"role": "company_user"})
    )
    seen = org_web.person_profile_route(owner, person_id=identifier)
    assert "accounts" not in seen
    assert "@" not in _json.dumps(seen, ensure_ascii=False, default=str)

    with pytest.raises(HTTPException) as error:
        org_web.person_profile_route(owner, person_id="p_000000000000")
    assert error.value.status_code == 404
