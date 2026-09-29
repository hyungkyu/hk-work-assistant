# hook-allow: synthetic-credentials
"""The second door: a password that signs in as an ordinary company user.

The owner wants to open his own backoffice twice — once as himself and once as
everybody else — so that what a colleague can see is a thing he can look at
rather than a thing he has to reason about. These tests pin the two halves of
that: the door exists and lands on `company_user`, and it never widens into the
owner's session by accident.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Iterator

import pytest
from fastapi import HTTPException

from rlwrld_worklog import admin_web
from rlwrld_worklog.admin_store import AdminStore
from rlwrld_worklog.admin_web import SESSION_COOKIE

ADMIN_PASSWORD = "a-long-enough-password"
STAFF_PASSWORD = "a-different-long-password"


class FakeRequest:
    def __init__(
        self,
        *,
        cookies: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        body: Any = None,
    ) -> None:
        self.cookies = cookies or {}
        self.headers = headers or {}
        self._body = body

    async def json(self) -> Any:
        return self._body


class FakeResponse:
    def __init__(self) -> None:
        self.cookie: str | None = None

    def set_cookie(self, *args: Any, **kwargs: Any) -> None:
        self.cookie = kwargs.get("value", args[1] if len(args) > 1 else None)


@pytest.fixture()
def config_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    root = tmp_path / "config"
    monkeypatch.setenv("APP_CONFIG_ROOT", str(root))
    monkeypatch.setenv("EMERGENCY_LOGIN_ENABLED", "true")
    admin_web.store.cache_clear()
    admin_web.store().set_admin_password(ADMIN_PASSWORD)
    yield root
    admin_web.store.cache_clear()


def login(password: str) -> dict[str, Any] | None:
    response = FakeResponse()
    asyncio.run(admin_web.emergency_login(FakeRequest(body={"password": password}), response))
    return admin_web.store().read_session(response.cookie)


def owner_request(body: Any = None) -> FakeRequest:
    token, csrf = admin_web.store().create_session(
        subject="owner", email="hyungkyu.ryu@rlwrld.ai", role="super_admin", auth_method="google"
    )
    return FakeRequest(cookies={SESSION_COOKIE: token}, headers={"x-csrf-token": csrf}, body=body)


# --- the store ------------------------------------------------------------


def test_the_staff_password_is_a_separate_file_from_the_owners(tmp_path: Path) -> None:
    store = AdminStore(tmp_path / "config")
    store.set_admin_password(ADMIN_PASSWORD)
    store.set_staff_password(STAFF_PASSWORD)

    assert store.password_path != store.staff_password_path
    assert store.staff_password_path.exists()
    assert store.verify_admin_password(ADMIN_PASSWORD)
    assert store.verify_staff_password(STAFF_PASSWORD)
    # Neither opens the other's lock.
    assert not store.verify_admin_password(STAFF_PASSWORD)
    assert not store.verify_staff_password(ADMIN_PASSWORD)


def test_the_staff_password_may_be_replaced_where_the_owners_may_not(tmp_path: Path) -> None:
    """The whole difference between the two doors.

    The owner's password is set once at bootstrap; this one gets handed around,
    so it has to be rotatable without reinstalling.
    """
    store = AdminStore(tmp_path / "config")
    store.set_admin_password(ADMIN_PASSWORD)
    with pytest.raises(RuntimeError):
        store.set_admin_password("another-long-password")

    store.set_staff_password(STAFF_PASSWORD)
    store.set_staff_password("a-rotated-long-password")
    assert not store.verify_staff_password(STAFF_PASSWORD)
    assert store.verify_staff_password("a-rotated-long-password")


def test_clearing_the_staff_password_closes_the_door(tmp_path: Path) -> None:
    store = AdminStore(tmp_path / "config")
    store.set_admin_password(ADMIN_PASSWORD)
    store.set_staff_password(STAFF_PASSWORD)

    assert store.clear_staff_password() is True
    assert not store.staff_password_set()
    assert not store.verify_staff_password(STAFF_PASSWORD)
    # Closing a door that is already closed is not an error, and says so.
    assert store.clear_staff_password() is False


def test_no_staff_password_means_nothing_opens_that_door(tmp_path: Path) -> None:
    store = AdminStore(tmp_path / "config")
    store.set_admin_password(ADMIN_PASSWORD)
    assert not store.staff_password_set()
    assert not store.verify_staff_password("")
    assert not store.verify_staff_password(STAFF_PASSWORD)


def test_the_staff_password_file_is_readable_only_by_its_owner(tmp_path: Path) -> None:
    import stat

    store = AdminStore(tmp_path / "config")
    store.set_admin_password(ADMIN_PASSWORD)
    store.set_staff_password(STAFF_PASSWORD)
    mode = stat.S_IMODE(store.staff_password_path.stat().st_mode)
    assert mode == 0o600, oct(mode)


def test_the_stored_staff_record_does_not_contain_the_password(tmp_path: Path) -> None:
    store = AdminStore(tmp_path / "config")
    store.set_admin_password(ADMIN_PASSWORD)
    store.set_staff_password(STAFF_PASSWORD)
    raw = store.staff_password_path.read_text(encoding="utf-8")
    assert STAFF_PASSWORD not in raw
    assert json.loads(raw)["algorithm"] == "scrypt"


# --- the door -------------------------------------------------------------


def test_the_owners_password_still_lands_on_the_owners_session(config_root: Path) -> None:
    """Regression: the second door must not have moved the first one."""
    session = login(ADMIN_PASSWORD)
    assert session is not None
    assert session["role"] == "super_admin"
    assert session["auth_method"] == "local_emergency"


def test_the_staff_password_lands_on_an_ordinary_company_session(config_root: Path) -> None:
    admin_web.store().set_staff_password(STAFF_PASSWORD)
    session = login(STAFF_PASSWORD)
    assert session is not None
    assert session["role"] == "company_user"
    assert session["auth_method"] == "local_emergency_staff"


def test_a_staff_session_is_refused_by_the_owner_only_routes(config_root: Path) -> None:
    """The lock is the server's. The icon on screen is only a label for it."""
    admin_web.store().set_staff_password(STAFF_PASSWORD)
    response = FakeResponse()
    asyncio.run(
        admin_web.emergency_login(FakeRequest(body={"password": STAFF_PASSWORD}), response)
    )
    request = FakeRequest(cookies={SESSION_COOKIE: response.cookie})

    with pytest.raises(HTTPException) as refused:
        admin_web.require_super_admin_session(request)
    assert refused.value.status_code == 403


def test_a_wrong_password_is_refused_the_same_way_whichever_door_exists(
    config_root: Path,
) -> None:
    """A caller learns that the value opened nothing, not which one it missed."""
    admin_web.store().set_staff_password(STAFF_PASSWORD)
    with pytest.raises(HTTPException) as first:
        login("not-either-of-them")
    admin_web.store().clear_staff_password()
    with pytest.raises(HTTPException) as second:
        login("not-either-of-them")
    assert first.value.status_code == second.value.status_code == 401
    assert first.value.detail == second.value.detail


def test_the_session_endpoint_says_whether_a_second_door_exists(config_root: Path) -> None:
    request = FakeRequest()
    assert admin_web.admin_session_status(request)["staff_password_set"] is False
    admin_web.store().set_staff_password(STAFF_PASSWORD)
    assert admin_web.admin_session_status(request)["staff_password_set"] is True


def test_only_the_owner_may_set_the_staff_password(config_root: Path) -> None:
    admin_web.store().set_staff_password(STAFF_PASSWORD)
    response = FakeResponse()
    asyncio.run(
        admin_web.emergency_login(FakeRequest(body={"password": STAFF_PASSWORD}), response)
    )
    staff = FakeRequest(
        cookies={SESSION_COOKIE: response.cookie},
        headers={"x-csrf-token": "whatever"},
        body={"password": "a-third-long-password"},
    )
    with pytest.raises(HTTPException) as refused:
        asyncio.run(admin_web.set_staff_password(staff))
    assert refused.value.status_code == 403


def test_the_owner_can_set_replace_and_clear_it_through_the_route(config_root: Path) -> None:
    result = asyncio.run(admin_web.set_staff_password(owner_request({"password": STAFF_PASSWORD})))
    assert result == {"ok": True, "staff_password_set": True, "cleared": False}
    assert admin_web.store().verify_staff_password(STAFF_PASSWORD)

    result = asyncio.run(admin_web.set_staff_password(owner_request({"password": ""})))
    assert result == {"ok": True, "staff_password_set": False, "cleared": True}
    assert not admin_web.store().staff_password_set()


def test_a_too_short_staff_password_is_refused_not_stored(config_root: Path) -> None:
    with pytest.raises(HTTPException) as refused:
        asyncio.run(admin_web.set_staff_password(owner_request({"password": "short"})))
    assert refused.value.status_code == 400
    assert not admin_web.store().staff_password_set()


def test_setting_the_staff_password_is_in_the_audit_trail(config_root: Path) -> None:
    asyncio.run(admin_web.set_staff_password(owner_request({"password": STAFF_PASSWORD})))
    asyncio.run(admin_web.set_staff_password(owner_request({"password": ""})))
    actions = [entry["action"] for entry in admin_web.store().read_audit(limit=50)]
    assert "admin.staff_password_set" in actions
    assert "admin.staff_password_cleared" in actions
    trail = admin_web.store().audit_path.read_text(encoding="utf-8")
    assert STAFF_PASSWORD not in trail


def test_a_staff_login_is_named_as_such_in_the_audit_trail(config_root: Path) -> None:
    admin_web.store().set_staff_password(STAFF_PASSWORD)
    login(STAFF_PASSWORD)
    entries = [e for e in admin_web.store().read_audit(limit=50) if e["action"] == "admin.login"]
    assert entries
    assert entries[0]["details"]["method"] == "local_emergency_staff"
    assert entries[0]["details"]["role"] == "company_user"
