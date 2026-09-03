# hook-allow: synthetic-credentials
"""Route-level tests for the delegated-work API.

The project has no HTTP test client dependency, so these call the route
callables with a minimal request stub.  That still exercises the authentication,
CSRF, error translation, and audit behaviour the endpoints are responsible for.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Iterator

import pytest
from fastapi import HTTPException

from rlwrld_worklog import admin_web, work_web
from rlwrld_worklog.admin_store import AdminStore
from rlwrld_worklog.admin_web import SESSION_COOKIE


class FakeRequest:
    """Only what the work routes touch: cookies, headers, and a JSON body."""

    def __init__(
        self,
        *,
        cookies: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        body: Any = None,
        raw_body: str | None = None,
    ) -> None:
        self.cookies = cookies or {}
        self.headers = headers or {}
        self._body = body
        self._raw_body = raw_body

    async def json(self) -> Any:
        if self._raw_body is not None:
            return json.loads(self._raw_body)
        return self._body


@pytest.fixture()
def config_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    root = tmp_path / "config"
    monkeypatch.setenv("APP_CONFIG_ROOT", str(root))
    admin_web.store.cache_clear()
    work_web.work_store.cache_clear()
    yield root
    admin_web.store.cache_clear()
    work_web.work_store.cache_clear()


@pytest.fixture()
def owner(config_root: Path) -> dict[str, str]:
    token, csrf = admin_web.store().create_session(
        subject="owner", email="hyungkyu.ryu@rlwrld.ai", role="super_admin", auth_method="google"
    )
    return {"token": token, "csrf": csrf}


def authorized(
    owner: dict[str, str], *, body: Any = None, csrf: str | None = "use", raw_body: str | None = None
) -> FakeRequest:
    headers = {} if csrf is None else {"x-csrf-token": owner["csrf"] if csrf == "use" else csrf}
    return FakeRequest(
        cookies={SESSION_COOKIE: owner["token"]}, headers=headers, body=body, raw_body=raw_body
    )


def create(owner: dict[str, str], **fields: Any) -> dict[str, Any]:
    payload = {"title": "위임 작업", "assigned_to": "codex"}
    payload.update(fields)
    return asyncio.run(work_web.create_item(authorized(owner, body={"fields": payload})))["item"]


def test_anonymous_and_company_users_cannot_reach_the_board(config_root: Path) -> None:
    with pytest.raises(HTTPException) as anonymous:
        work_web.list_items(FakeRequest())
    assert anonymous.value.status_code == 401

    token, _ = admin_web.store().create_session(
        subject="member", email="member@rlwrld.ai", role="company_user", auth_method="google"
    )
    with pytest.raises(HTTPException) as member:
        work_web.list_items(FakeRequest(cookies={SESSION_COOKIE: token}))
    assert member.value.status_code == 403

    with pytest.raises(HTTPException) as tampered:
        work_web.list_items(FakeRequest(cookies={SESSION_COOKIE: "not.a.session"}))
    assert tampered.value.status_code == 401


def test_mutations_require_the_session_csrf_token(owner: dict[str, str]) -> None:
    body = {"fields": {"title": "CSRF", "assigned_to": "codex"}}
    for csrf in (None, "wrong-token"):
        with pytest.raises(HTTPException) as error:
            asyncio.run(work_web.create_item(authorized(owner, body=body, csrf=csrf)))
        assert error.value.status_code == 403
        assert error.value.detail == "invalid CSRF token"

    item = create(owner)
    for call in (
        lambda: work_web.update_item(item["id"], authorized(owner, body={"fields": {"status": "ready"}}, csrf=None)),
        lambda: work_web.archive_item(item["id"], authorized(owner, body={}, csrf=None)),
    ):
        with pytest.raises(HTTPException) as error:
            asyncio.run(call())
        assert error.value.status_code == 403


def test_create_list_get_update_and_archive(owner: dict[str, str]) -> None:
    item = create(owner, priority="high", next_action="스키마 확정")
    # requested_by defaults to the authenticated super administrator.
    assert item["requested_by"] == "hyungkyu.ryu@rlwrld.ai"
    assert item["revision"] == 1

    listing = work_web.list_items(authorized(owner))
    assert listing["count"] == 1
    assert listing["items"][0]["id"] == item["id"]
    assert work_web.get_item(item["id"], authorized(owner))["item"]["title"] == "위임 작업"

    updated = asyncio.run(
        work_web.update_item(
            item["id"],
            authorized(owner, body={"fields": {"status": "in_progress"}, "expected_revision": 1}),
        )
    )["item"]
    assert updated["status"] == "in_progress"
    assert updated["revision"] == 2

    archived = asyncio.run(
        work_web.archive_item(item["id"], authorized(owner, body={"expected_revision": 2}))
    )["item"]
    assert archived["archived_at"] is not None
    assert work_web.list_items(authorized(owner))["count"] == 0
    assert work_web.list_items(authorized(owner), include_archived=True)["count"] == 1


def test_error_translation_is_explicit(owner: dict[str, str], config_root: Path) -> None:
    with pytest.raises(HTTPException) as missing_title:
        asyncio.run(work_web.create_item(authorized(owner, body={"fields": {"assigned_to": "codex"}})))
    assert missing_title.value.status_code == 400

    with pytest.raises(HTTPException) as unknown_field:
        asyncio.run(work_web.create_item(authorized(owner, body={"fields": {"title": "t", "assigned_to": "codex", "sneaky": 1}})))
    assert unknown_field.value.status_code == 400
    assert "unknown fields" in unknown_field.value.detail

    with pytest.raises(HTTPException) as not_found:
        work_web.get_item("wi_" + "0" * 16, authorized(owner))
    assert not_found.value.status_code == 404

    item = create(owner)
    asyncio.run(work_web.update_item(item["id"], authorized(owner, body={"fields": {"status": "ready"}})))
    with pytest.raises(HTTPException) as conflict:
        asyncio.run(
            work_web.update_item(
                item["id"],
                authorized(owner, body={"fields": {"status": "done"}, "expected_revision": 1}),
            )
        )
    assert conflict.value.status_code == 409

    with pytest.raises(HTTPException) as bad_expectation:
        asyncio.run(
            work_web.update_item(
                item["id"],
                authorized(owner, body={"fields": {"status": "done"}, "expected_revision": "1"}),
            )
        )
    assert bad_expectation.value.status_code == 400

    with pytest.raises(HTTPException) as bad_body:
        asyncio.run(work_web.create_item(authorized(owner, raw_body="[]")))
    assert bad_body.value.status_code == 400


def test_corrupt_store_returns_service_unavailable(owner: dict[str, str]) -> None:
    create(owner)
    work_web.work_store().items_path.write_text("truncated", encoding="utf-8")

    with pytest.raises(HTTPException) as listing:
        work_web.list_items(authorized(owner))
    assert listing.value.status_code == 503
    assert "left unchanged" in listing.value.detail

    with pytest.raises(HTTPException) as creating:
        asyncio.run(work_web.create_item(authorized(owner, body={"fields": {"title": "t", "assigned_to": "codex"}})))
    assert creating.value.status_code == 503
    assert work_web.work_store().items_path.read_text(encoding="utf-8") == "truncated"


def test_mutations_are_audited_without_item_text(owner: dict[str, str], config_root: Path) -> None:
    item = create(owner, detail="xoxp-sensitive-looking-detail")
    asyncio.run(
        work_web.update_item(item["id"], authorized(owner, body={"fields": {"status": "in_progress"}}))
    )
    asyncio.run(work_web.archive_item(item["id"], authorized(owner, body={})))

    audit = AdminStore(config_root).read_audit()
    actions = [entry["action"] for entry in audit]
    assert actions[:3] == ["work.archived", "work.updated", "work.created"]
    assert all(entry["actor"] == "hyungkyu.ryu@rlwrld.ai" for entry in audit[:3])
    serialized = json.dumps(audit, ensure_ascii=False)
    assert "xoxp-" not in serialized
    assert "위임 작업" not in serialized

    history = work_web.work_history(authorized(owner))["items"]
    assert [entry["action"] for entry in history] == ["work.archived", "work.updated", "work.created"]
    meta = work_web.work_meta(authorized(owner))
    assert meta["statuses"][0] == "in_progress"
    assert meta["queue_stages"] == ["in_progress", "ready", "todo", "backlog"]


def test_history_and_listing_stay_read_only_for_the_session(owner: dict[str, str]) -> None:
    # Read endpoints must work without a CSRF header; mutations must not.
    assert work_web.list_items(authorized(owner, csrf=None))["count"] == 0
    assert work_web.work_history(authorized(owner, csrf=None))["items"] == []


# ------------------------------------------------- cowork timeline endpoint


def test_the_timeline_endpoint_requires_a_super_administrator(
    config_root: Path, owner: dict[str, str]
) -> None:
    item = create(owner)
    with pytest.raises(HTTPException) as anonymous:
        work_web.work_timeline(item["id"], FakeRequest())
    assert anonymous.value.status_code == 401

    token, _ = admin_web.store().create_session(
        subject="staff", email="staff@rlwrld.ai", role="company_user", auth_method="google"
    )
    with pytest.raises(HTTPException) as company:
        work_web.work_timeline(item["id"], FakeRequest(cookies={SESSION_COOKIE: token}))
    assert company.value.status_code == 403


def test_the_timeline_endpoint_returns_resolved_actors(
    config_root: Path, owner: dict[str, str]
) -> None:
    item = create(owner, assigned_to="moa", requested_by="hk")
    payload = work_web.work_timeline(item["id"], authorized(owner))
    assert payload["item_id"] == item["id"]
    assert payload["entries"]
    entry = payload["entries"][-1]
    assert "party" in entry["actor"] and "resolution" in entry["actor"]
    assert entry["assigned_to"] == "moa"


@pytest.mark.parametrize(
    "item_id", ["../../etc/passwd", "..", "wi_does_not_exist", "/etc/passwd", ""],
)
def test_an_item_id_that_is_not_a_real_item_cannot_reach_the_filesystem(
    config_root: Path, owner: dict[str, str], item_id: str
) -> None:
    """The id is looked up in the document; it is never joined onto a path."""
    with pytest.raises(HTTPException) as error:
        work_web.work_timeline(item_id, authorized(owner))
    assert error.value.status_code == 404


def test_the_meta_endpoint_publishes_the_cowork_authority_rules(
    config_root: Path, owner: dict[str, str]
) -> None:
    payload = work_web.work_meta(authorized(owner))
    # The original shape is preserved for existing clients.
    assert "statuses" in payload and "priorities" in payload
    cowork = payload["cowork"]
    assert cowork["authority_order"] == ["hk", "ari", "mori"]
    assert cowork["types_carrying_authority"] == ["ASSIGN"]
    assert set(cowork["autonomous_baseline"]) == {"read", "investigate", "report", "test"}
    assert "phases" in payload


def test_the_timeline_limit_is_bounded(config_root: Path, owner: dict[str, str]) -> None:
    item = create(owner)
    payload = work_web.work_timeline(item["id"], authorized(owner), limit=1)
    assert len(payload["entries"]) <= 1
