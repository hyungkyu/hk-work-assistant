"""The organisation routes: authenticated, read-only, and honest about gaps.

These call the route callables with the same minimal request stub the other
API suites use, which still exercises authentication, argument validation and
the read-only contract — the part these routes are responsible for.

The contract worth stating: nothing here collects or generates. The org chart
changes when the roster sync runs and a person's day changes when the digest
batch builds it (HK, 2026-09-11: 이건 코드여야지, 네가 하면 안됨). The one
mutation is a person answering whose an unknown account is.
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


def test_every_reading_route_is_a_get_and_only_resolve_is_a_post() -> None:
    """A screen that could regenerate its own contents would break the protocol."""
    from rlwrld_worklog.web import app

    schema = app.openapi()["paths"]
    org_paths = {
        path: sorted(methods) for path, methods in schema.items() if path.startswith("/api/v1/admin/org")
    }
    assert org_paths == {
        "/api/v1/admin/org/chart": ["get"],
        "/api/v1/admin/org/status": ["get"],
        "/api/v1/admin/org/unmapped": ["get"],
        "/api/v1/admin/org/unmapped/resolve": ["post"],
        "/api/v1/admin/org/digest/status": ["get"],
        "/api/v1/admin/org/digest/{person_id}": ["get"],
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
    body = org_web.person_day_route(owner, person_id="p_nobody", day="2026-09-10")
    assert body == {"person_id": "p_nobody", "day": "2026-09-10", "built": False}
