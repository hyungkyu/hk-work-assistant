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


def _emergency(config_root: Path, monkeypatch: pytest.MonkeyPatch, **body: Any) -> dict[str, Any]:
    """Go through the break-glass door and hand back the session it minted."""
    monkeypatch.setenv("EMERGENCY_LOGIN_ENABLED", "true")
    admin_web.store().set_admin_password("a-long-enough-password")
    response = FakeResponse()
    asyncio.run(
        admin_web.emergency_login(
            FakeRequest(body={"password": "a-long-enough-password", **body}), response
        )
    )
    session = admin_web.store().read_session(response.cookie)
    assert session is not None
    return session


class FakeResponse:
    """Only what the login routes touch: one cookie."""

    def __init__(self) -> None:
        self.cookie: str | None = None

    def set_cookie(self, *args: Any, **kwargs: Any) -> None:
        self.cookie = kwargs.get("value", args[1] if len(args) > 1 else None)


def test_a_break_glass_session_without_a_name_records_what_it_did_before(
    config_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Naming yourself is optional; declining leaves the trail as it was."""
    session = _emergency(config_root, monkeypatch)
    assert admin_web.session_actor(session) == "local-emergency"


def test_a_break_glass_session_can_name_itself_and_the_board_records_that_name(
    config_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point of step A: an edit made at 8081 stops reading as nobody."""
    session = _emergency(config_root, monkeypatch, actor="hk")
    assert session["sub"] == "hk"
    assert admin_web.session_actor(session) == "hk"

    token, csrf = admin_web.store().create_session(subject="hk")
    request = FakeRequest(
        cookies={SESSION_COOKIE: token},
        headers={"x-csrf-token": csrf},
        body={"fields": {"title": "비상문으로 만든 항목", "assigned_to": "noa"}},
    )
    item = asyncio.run(work_web.create_item(request))["item"]

    history = work_web.work_history(FakeRequest(cookies={SESSION_COOKIE: token}), item_id=item["id"])
    actors = {entry["actor"] for entry in history["items"]}
    assert actors == {"hk"}, actors


def test_a_declared_name_cannot_be_shaped_like_an_authenticated_one(
    config_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A password proves no identity, so it must not mint a Google-looking actor."""
    with pytest.raises(HTTPException) as error:
        _emergency(config_root, monkeypatch, actor="hyungkyu.ryu@rlwrld.ai")
    assert error.value.status_code == 400


@pytest.mark.parametrize("declared", ["../../etc/passwd", "a b", "x" * 65, "*", 7])
def test_a_declared_name_that_is_not_a_name_is_refused(
    config_root: Path, monkeypatch: pytest.MonkeyPatch, declared: Any
) -> None:
    with pytest.raises(HTTPException) as error:
        _emergency(config_root, monkeypatch, actor=declared)
    assert error.value.status_code == 400


def test_a_google_session_still_records_the_email_not_the_subject(
    config_root: Path, owner: dict[str, str]
) -> None:
    """The email is the better name, so step A must not have displaced it."""
    session = admin_web.store().read_session(owner["token"])
    assert session is not None
    assert session["sub"] == "owner"
    assert admin_web.session_actor(session) == "hyungkyu.ryu@rlwrld.ai"


# ------------------------------------------------- what an agent may and may not do


def _agent(name: str) -> dict[str, str]:
    token, csrf = admin_web.store().issue_agent_session(name)
    return {"token": token, "csrf": csrf}


def test_an_agent_can_see_every_screen(config_root: Path, owner: dict[str, str]) -> None:
    """Reads are open. Not seeing a screen is how several wrong calls got made."""
    item = create(owner)
    noa = _agent("noa")

    assert work_web.list_items(authorized(noa))["items"]
    assert work_web.get_item(item["id"], authorized(noa))["item"]["id"] == item["id"]
    assert work_web.work_meta(authorized(noa))["statuses"]
    assert "entries" in work_web.work_timeline(item["id"], authorized(noa))
    assert "items" in work_web.work_history(authorized(noa))


def test_an_agent_can_write_its_own_item(config_root: Path, owner: dict[str, str]) -> None:
    item = create(owner, assigned_to="noa")
    noa = _agent("noa")

    updated = asyncio.run(
        work_web.update_item(
            item["id"], authorized(noa, body={"fields": {"progress_summary": "진행했다"}})
        )
    )["item"]
    assert updated["progress_summary"] == "진행했다"
    # Recorded as the board already knows this party, not as a second spelling.
    assert work_web.work_history(authorized(noa), item_id=item["id"])["items"][0]["actor"] == "noa"


def test_an_agent_cannot_write_another_agents_item(
    config_root: Path, owner: dict[str, str]
) -> None:
    """Handing work to another executor is directing them, which is a role."""
    item = create(owner, assigned_to="boa")
    noa = _agent("noa")

    with pytest.raises(HTTPException) as error:
        asyncio.run(
            work_web.update_item(
                item["id"], authorized(noa, body={"fields": {"progress_summary": "남의 것"}})
            )
        )
    assert error.value.status_code == 403


def test_an_agent_may_open_an_item(config_root: Path) -> None:
    """Recording work that exists is never the dangerous direction."""
    noa = _agent("noa")
    created = asyncio.run(
        work_web.create_item(
            authorized(noa, body={"fields": {"title": "내가 연 항목", "assigned_to": "noa"}})
        )
    )["item"]
    assert created["requested_by"] == "noa"


def test_an_agent_cannot_archive_anything(config_root: Path, owner: dict[str, str]) -> None:
    """Archiving is the one board action that cannot be undone by its author."""
    item = create(owner, assigned_to="noa")
    noa = _agent("noa")

    with pytest.raises(HTTPException) as error:
        asyncio.run(work_web.archive_item(item["id"], authorized(noa, body={})))
    assert error.value.status_code == 403


def test_an_agent_cannot_reach_settings_or_credentials(config_root: Path) -> None:
    """If an agent could change super_admin_google_email the boundary means nothing."""
    noa = _agent("noa")
    for call in (
        lambda: admin_web.get_settings(authorized(noa)),
        lambda: asyncio.run(admin_web.put_settings(authorized(noa, body={"a": 1}))),
        lambda: asyncio.run(
            admin_web.put_secret("slack_token", authorized(noa, body={"value": "xoxp-x"}))
        ),
    ):
        with pytest.raises(HTTPException) as error:
            call()
        assert error.value.status_code == 403


def test_a_revoked_agent_loses_the_board_immediately(
    config_root: Path, owner: dict[str, str]
) -> None:
    noa = _agent("noa")
    assert work_web.list_items(authorized(noa))["items"] is not None

    admin_web.store().revoke_agent("agent:noa")
    with pytest.raises(HTTPException) as error:
        work_web.list_items(authorized(noa))
    assert error.value.status_code == 401


def test_the_password_door_cannot_claim_an_agents_name(
    config_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One spelling, one authority. Otherwise history cannot tell them apart."""
    with pytest.raises(HTTPException) as error:
        _emergency(config_root, monkeypatch, actor="noa")
    assert error.value.status_code == 400


def test_the_detail_names_who_directed_carried_and_checks_it(
    config_root: Path, owner: dict[str, str]
) -> None:
    """Condition 4. All four read off what is already there, so none can drift."""
    item = create(owner, assigned_to="noa", requested_by="hk", status="in_progress")
    payload = work_web.get_item(item["id"], authorized(owner))
    roles = payload["roles"]
    assert roles["directed_by"] == "hk"
    assert roles["performed_by"] == "noa"
    assert roles["stage"] == {
        "status": "in_progress", "label": "진행 중", "kind": "queue", "terminal": False,
    }


def test_a_reviewer_is_read_off_the_review_item_not_stored_twice(
    config_root: Path, owner: dict[str, str]
) -> None:
    item = create(owner, assigned_to="noa")
    assert work_web.get_item(item["id"], authorized(owner))["roles"]["reviewed_by"] == []

    create(owner, assigned_to="roa", parent_id=item["id"], source_ref="review:permissions")
    payload = work_web.get_item(item["id"], authorized(owner))
    assert payload["roles"]["reviewed_by"] == ["roa"]
    assert "names this one as its parent" in payload["roles"]["reviewed_by_basis"]


def test_no_reviewer_says_why_rather_than_reading_as_nobody_checks_it(
    config_root: Path, owner: dict[str, str]
) -> None:
    """An empty list and "no review item points here" are different claims."""
    item = create(owner, assigned_to="noa")
    # A child that is not a review does not make its assignee the reviewer.
    create(owner, assigned_to="boa", parent_id=item["id"], source_ref="defect:something")
    roles = work_web.get_item(item["id"], authorized(owner))["roles"]
    assert roles["reviewed_by"] == []
    assert roles["reviewed_by_basis"] == "no open review item names this one as its parent"


def test_a_condition_status_is_marked_apart_from_a_queue_stage(
    config_root: Path, owner: dict[str, str]
) -> None:
    """`blocked` says why the work is not moving, not where it is."""
    item = create(owner, assigned_to="noa", status="blocked")
    stage = work_web.get_item(item["id"], authorized(owner))["roles"]["stage"]
    assert stage["kind"] == "condition"
    assert stage["terminal"] is False
