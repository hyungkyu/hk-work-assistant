"""권한 체계 (HK, 2026-10-15): 나 (전체) / 민감 정보 취급자 / 일반."""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("WORKLOG_TEST_DATABASE_URL"),
    reason="set WORKLOG_TEST_DATABASE_URL to a throwaway database",
)


@pytest.fixture()
def url():
    from rlwrld_worklog.ledger.load import apply_migrations

    database = os.environ["WORKLOG_TEST_DATABASE_URL"]
    apply_migrations(
        database_url=database,
        migrations_dir=Path(__file__).resolve().parents[1] / "sql" / "migrations",
        dry_run=False,
    )
    return database


def test_the_owner_reaches_everything_and_a_colleague_only_what_the_role_was_granted(url) -> None:
    from rlwrld_worklog import access

    email = f"hr-{uuid.uuid4().hex[:6]}@rlwrld.ai"
    owner = {"role": "super_admin", "email": "hyungkyu.ryu@rlwrld.ai"}
    colleague = {"role": "company_user", "email": email}
    try:
        assert access.allows(owner, "area:cv_detail", url)
        assert access.role_of(colleague, url) == "general", "no row is 일반"
        assert not access.allows(colleague, "area:cv_detail", url)

        access.set_user_role(url, email.upper(), "sensitive", "test")
        assert access.role_of(colleague, url) == "sensitive", "the email is matched case aside"
        assert access.allows(colleague, "area:cv_detail", url), "민감 정보 취급자 starts with 인사정보 상세"
        assert not access.allows(colleague, "area:cv_edit", url), "editing is not part of it unless granted"
    finally:
        access.set_user_role(url, email, None, "test")


def test_a_grant_cannot_name_a_screen_the_server_does_not_guard(url) -> None:
    from rlwrld_worklog import access

    with pytest.raises(ValueError):
        access.set_grant(url, "general", "page:settings", True, "test")
    with pytest.raises(ValueError):
        access.set_grant(url, "general", "area:nothing_like_this", True, "test")
    with pytest.raises(ValueError):
        access.set_user_role(url, "someone@rlwrld.ai", "owner", "test")


def test_without_a_database_a_colleague_gets_nothing() -> None:
    from rlwrld_worklog import access

    assert access.allows(None, "area:cv_detail") is False
