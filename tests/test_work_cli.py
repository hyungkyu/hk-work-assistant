from __future__ import annotations

import json
from pathlib import Path

import pytest

from rlwrld_worklog.cli import main
from rlwrld_worklog.work_store import WorkStore


def run(capsys: pytest.CaptureFixture[str], root: Path, *argv: str) -> tuple[int, dict]:
    code = main(["work", *argv, "--config-root", str(root)])
    captured = capsys.readouterr()
    return code, json.loads(captured.out)


def test_cli_creates_lists_and_shows_machine_readable_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "config"
    code, created = run(
        capsys,
        root,
        "create",
        "--title",
        "위임 추적 1차",
        "--requested-by",
        "hk",
        "--assigned-to",
        "codex",
        "--priority",
        "high",
        "--next-action",
        "스키마 확정",
    )
    assert code == 0
    assert created["ok"] is True and created["created"] is True
    item_id = created["item"]["id"]

    code, listing = run(capsys, root, "list")
    assert code == 0
    assert listing["count"] == 1
    assert listing["items"][0]["next_action"] == "스키마 확정"

    code, shown = run(capsys, root, "show", item_id)
    assert code == 0 and shown["item"]["priority"] == "high"

    # The store lives only under the given config root.
    assert (root / "work" / "items.json").is_file()
    assert WorkStore(root).get_item(item_id)["title"] == "위임 추적 1차"


def test_cli_updates_clear_fields_and_record_the_actor(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "config"
    _, created = run(
        capsys, root, "create", "--title", "T", "--requested-by", "hk", "--assigned-to", "codex",
        "--blocker", "리뷰 대기",
    )
    item_id = created["item"]["id"]

    code, updated = run(
        capsys, root, "update", item_id, "--status", "in_progress", "--progress", "설계 완료",
        "--clear", "blocker", "--actor", "codex", "--expected-revision", "1",
    )
    assert code == 0
    assert updated["item"]["status"] == "in_progress"
    assert updated["item"]["blocker"] is None
    assert updated["item"]["started_at"] is not None
    assert updated["item"]["revision"] == 2

    _, history = run(capsys, root, "history", "--limit", "1")
    assert history["items"][0]["actor"] == "codex"
    assert history["items"][0]["fields"] == ["blocker", "progress_summary", "status"]


def test_cli_upsert_is_idempotent_by_source_ref(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "config"
    code, first = run(
        capsys, root, "upsert", "--match-source-ref", "notion:page-1",
        "--title", "노션 정리", "--requested-by", "hk", "--assigned-to", "claude-code",
    )
    assert code == 0 and first["created"] is True

    code, second = run(
        capsys, root, "upsert", "--match-source-ref", "notion:page-1", "--status", "waiting",
        "--expected-revision", "1",
    )
    assert code == 0
    assert second["created"] is False
    assert second["item"]["id"] == first["item"]["id"]
    assert second["item"]["status"] == "waiting"

    _, listing = run(capsys, root, "list")
    assert listing["count"] == 1


def test_cli_upsert_will_not_overwrite_an_existing_item_by_default(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "config"
    _, first = run(
        capsys, root, "upsert", "--match-source-ref", "notion:page-1",
        "--title", "노션 정리", "--requested-by", "hk", "--assigned-to", "claude-code",
    )
    item_id = first["item"]["id"]
    run(capsys, root, "update", item_id, "--progress", "사람이 남긴 메모", "--actor", "hk")

    code, refused = run(
        capsys, root, "upsert", "--match-source-ref", "notion:page-1", "--progress", "덮어쓰기",
    )
    assert code == 2
    assert refused["ok"] is False and refused["error"]["kind"] == "validation"
    assert "expected_revision" in refused["error"]["message"]

    code, stale = run(
        capsys, root, "upsert", "--match-source-ref", "notion:page-1", "--progress", "덮어쓰기",
        "--expected-revision", "1",
    )
    assert code == 4 and stale["error"]["kind"] == "conflict"

    _, shown = run(capsys, root, "show", item_id)
    assert shown["item"]["progress_summary"] == "사람이 남긴 메모"

    code, forced = run(
        capsys, root, "upsert", "--match-source-ref", "notion:page-1", "--progress", "기계가 덮어씀",
        "--force-overwrite",
    )
    assert code == 0
    assert forced["created"] is False
    assert forced["item"]["progress_summary"] == "기계가 덮어씀"

    code, current = run(
        capsys, root, "upsert", "--match-source-ref", "notion:page-1", "--status", "waiting",
        "--expected-updated-at", forced["item"]["updated_at"],
    )
    assert code == 0 and current["item"]["status"] == "waiting"


def test_cli_upsert_by_id_and_missing_match_keep_the_exit_contract(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "config"
    _, created = run(
        capsys, root, "create", "--title", "T", "--requested-by", "hk", "--assigned-to", "codex"
    )
    item_id = created["item"]["id"]

    code, updated = run(
        capsys, root, "upsert", "--id", item_id, "--status", "ready", "--expected-revision", "1"
    )
    assert code == 0 and updated["created"] is False and updated["item"]["status"] == "ready"

    code, pointless = run(
        capsys, root, "upsert", "--match-source-ref", "absent", "--title", "새 작업",
        "--requested-by", "hk", "--assigned-to", "codex", "--expected-revision", "1",
    )
    assert code == 2 and "nothing to expect" in pointless["error"]["message"]

    code, missing = run(capsys, root, "upsert", "--id", "wi_0000000000000000", "--status", "done")
    assert code == 3 and missing["error"]["kind"] == "not_found"


def test_cli_archive_is_a_soft_delete(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / "config"
    _, created = run(
        capsys, root, "create", "--title", "T", "--requested-by", "hk", "--assigned-to", "codex"
    )
    item_id = created["item"]["id"]

    code, archived = run(capsys, root, "archive", item_id)
    assert code == 0 and archived["item"]["archived_at"] is not None

    _, listing = run(capsys, root, "list")
    assert listing["count"] == 0
    _, all_items = run(capsys, root, "list", "--include-archived")
    assert all_items["count"] == 1


def test_cli_reports_errors_as_json_with_distinct_exit_codes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "config"
    _, created = run(
        capsys, root, "create", "--title", "T", "--requested-by", "hk", "--assigned-to", "codex"
    )
    item_id = created["item"]["id"]

    code, missing = run(capsys, root, "show", "wi_0000000000000000")
    assert code == 3 and missing["ok"] is False and missing["error"]["kind"] == "not_found"

    run(capsys, root, "update", item_id, "--status", "ready")
    code, conflict = run(capsys, root, "update", item_id, "--status", "done", "--expected-revision", "1")
    assert code == 4 and conflict["error"]["kind"] == "conflict"

    code, invalid = run(capsys, root, "create", "--title", "T", "--requested-by", "hk", "--assigned-to", "not valid")
    assert code == 2 and invalid["error"]["kind"] == "validation"

    (root / "work" / "items.json").write_text("nope", encoding="utf-8")
    code, corrupt = run(capsys, root, "list")
    assert code == 5 and corrupt["error"]["kind"] == "corruption"
    assert (root / "work" / "items.json").read_text(encoding="utf-8") == "nope"


def test_cli_rejects_unsupported_values_before_touching_the_store(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "config"
    with pytest.raises(SystemExit):
        main(["work", "create", "--title", "T", "--status", "invented", "--config-root", str(root)])
    with pytest.raises(SystemExit):
        main(["work", "update", "x", "--clear", "title", "--config-root", str(root)])


def test_cli_uses_the_environment_config_root_when_no_override(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APP_CONFIG_ROOT", str(tmp_path / "environment"))
    monkeypatch.setenv("WORKLOG_ACTOR", "claude-code")
    assert main(["work", "create", "--title", "T", "--requested-by", "hk", "--assigned-to", "codex"]) == 0
    payload = json.loads(capsys.readouterr().out)

    store = WorkStore(tmp_path / "environment")
    assert store.get_item(payload["item"]["id"])["title"] == "T"
    assert store.read_history()[0]["actor"] == "claude-code"


def test_a_token_can_be_issued_and_that_agent_alone_cut_off(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """B's whole point: a ninety-day session is only safe if one can be ended."""
    from rlwrld_worklog.admin_store import AdminStore

    config = tmp_path / "config"
    _, issued = run(capsys, config, "agent-token", "noa")
    _, other = run(capsys, config, "agent-token", "boa")
    assert issued["subject"] == "agent:noa"
    assert issued["expires_in_seconds"] == AdminStore.AGENT_SESSION_SECONDS

    store = AdminStore(config)
    assert store.read_session(issued["token"]) is not None

    _, revoked = run(capsys, config, "agent-revoke", "noa")
    assert revoked["generation"] == 1
    assert store.read_session(issued["token"]) is None
    assert store.read_session(other["token"]) is not None


def test_the_roster_is_listed_with_how_often_each_was_cut_off(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "config"
    run(capsys, config, "agent-revoke", "roa")
    _, listed = run(capsys, config, "agent-list")
    counts = {entry["name"]: entry["revocations"] for entry in listed["agents"]}
    assert counts == {"noa": 0, "boa": 0, "doa": 0, "roa": 1, "soa": 0}


def _queue(outbox: Path, name: str, payload: dict) -> Path:
    outbox.mkdir(parents=True, exist_ok=True)
    path = outbox / name
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_a_queued_edit_reaches_the_board_and_is_filed_as_applied(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config, outbox = tmp_path / "config", tmp_path / "outbox"
    _, created = run(capsys, config, "create", "--title", "티켓", "--assigned-to", "soa",
                     "--requested-by", "mori")
    item_id = created["item"]["id"]
    _queue(outbox, "a.json", {"work_id": item_id, "next_action": "이걸 해라"})

    _, result = run(capsys, config, "apply-outbox", "--outbox", str(outbox), "--actor", "mori")
    assert (result["applied"], result["rejected"]) == (1, 0)

    _, shown = run(capsys, config, "show", item_id)
    assert shown["item"]["next_action"] == "이걸 해라"
    assert (outbox / "applied" / "a.json").exists()
    assert (outbox / "applied" / "a.json.reason.json").exists()


def test_the_queue_never_writes_the_executors_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The requester writes the request; the executor writes the report."""
    config, outbox = tmp_path / "config", tmp_path / "outbox"
    _, created = run(capsys, config, "create", "--title", "티켓", "--assigned-to", "soa",
                     "--requested-by", "mori")
    item_id = created["item"]["id"]
    _queue(outbox, "b.json", {
        "work_id": item_id,
        "next_action": "허용",
        "detail": "허용",
        "progress_summary": "남의 보고를 대신 쓰려는 것",
        "blocker": "막혔다고 대신 말하려는 것",
    })

    _, result = run(capsys, config, "apply-outbox", "--outbox", str(outbox), "--actor", "mori")
    entry = result["results"][0]
    assert entry["applied"] == ["detail", "next_action"]
    assert entry["ignored"] == ["blocker", "progress_summary"]

    _, shown = run(capsys, config, "show", item_id)
    assert shown["item"]["progress_summary"] == ""
    assert shown["item"]["blocker"] is None


def test_the_queue_refuses_a_status_that_claims_work_happened(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """done, in_progress, waiting and blocked are all claims about what happened.

    Refused rather than dropped: a queue that silently ignored the field would
    leave the sender believing the board says something it does not.
    """
    config, outbox = tmp_path / "config", tmp_path / "outbox"
    _, created = run(capsys, config, "create", "--title", "티켓", "--assigned-to", "soa",
                     "--requested-by", "mori")
    item_id = created["item"]["id"]
    for name, status in (
        ("d.json", "done"),
        ("p.json", "in_progress"),
        ("w.json", "waiting"),
        ("b.json", "blocked"),
    ):
        _queue(outbox, name, {"work_id": item_id, "status": status})

    _, result = run(capsys, config, "apply-outbox", "--outbox", str(outbox), "--actor", "mori")
    assert (result["applied"], result["rejected"]) == (0, 4)
    for entry in result["results"]:
        assert "may not be set from the queue" in entry["reason"]

    _, shown = run(capsys, config, "show", item_id)
    assert shown["item"]["status"] == "backlog"


def test_the_queue_can_take_back_work_nobody_is_doing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """P0 condition 3: an in_progress item nobody is working must be correctable.

    Moving out of in_progress disclaims progress rather than asserting it, so
    it belongs to the requester. Until the queue could do this, the requester
    could see the defect and had no way to fix it.
    """
    config, outbox = tmp_path / "config", tmp_path / "outbox"
    _, created = run(capsys, config, "create", "--title", "티켓", "--assigned-to", "soa",
                     "--requested-by", "mori", "--status", "in_progress")
    item_id = created["item"]["id"]
    _queue(outbox, "r.json", {"work_id": item_id, "status": "ready", "assigned_to": "local"})

    _, result = run(capsys, config, "apply-outbox", "--outbox", str(outbox), "--actor", "mori")
    assert (result["applied"], result["rejected"]) == (1, 0)

    _, shown = run(capsys, config, "show", item_id)
    assert shown["item"]["status"] == "ready"
    assert shown["item"]["assigned_to"] == "local"


def test_the_queue_can_withdraw_its_own_request(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Taking back a request is not a report about how the work went."""
    config, outbox = tmp_path / "config", tmp_path / "outbox"
    _, created = run(capsys, config, "create", "--title", "티켓", "--assigned-to", "soa",
                     "--requested-by", "mori")
    item_id = created["item"]["id"]
    _queue(outbox, "c.json", {"work_id": item_id, "status": "cancelled",
                              "next_action": "구멍이 메워져 용도가 끝났다"})

    _, result = run(capsys, config, "apply-outbox", "--outbox", str(outbox), "--actor", "mori")
    assert (result["applied"], result["rejected"]) == (1, 0)

    _, shown = run(capsys, config, "show", item_id)
    assert shown["item"]["status"] == "cancelled"


def test_a_stale_revision_is_refused_and_never_merged(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Merging would overwrite a change the requester never saw."""
    config, outbox = tmp_path / "config", tmp_path / "outbox"
    _, created = run(capsys, config, "create", "--title", "티켓", "--assigned-to", "soa",
                     "--requested-by", "mori")
    item_id = created["item"]["id"]
    run(capsys, config, "update", item_id, "--progress", "그 사이 소아가 적었다")
    _queue(outbox, "c.json", {"work_id": item_id, "next_action": "낡은 개정판", "expected_revision": 1})

    _, result = run(capsys, config, "apply-outbox", "--outbox", str(outbox), "--actor", "mori")
    assert result["rejected"] == 1
    assert "not merged" in result["results"][0]["reason"]

    _, shown = run(capsys, config, "show", item_id)
    assert shown["item"]["progress_summary"] == "그 사이 소아가 적었다"
    assert (outbox / "rejected" / "c.json").exists()


@pytest.mark.parametrize(
    "name,payload,fragment",
    [
        ("no-id.json", {"next_action": "x"}, "work_id is required"),
        ("nothing.json", {"work_id": "wi_x"}, "nothing to apply"),
        ("bad-type.json", {"work_id": "wi_x", "next_action": 7}, "must be a string"),
        ("missing.json", {"work_id": "wi_nope", "next_action": "x"}, "WorkNotFoundError"),
    ],
)
def test_every_refusal_is_filed_with_a_reason(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], name: str, payload: dict, fragment: str
) -> None:
    """The worst outcome is a file dropped and nothing happening anywhere."""
    config, outbox = tmp_path / "config", tmp_path / "outbox"
    _queue(outbox, name, payload)

    _, result = run(capsys, config, "apply-outbox", "--outbox", str(outbox), "--actor", "mori")
    assert result["rejected"] == 1
    assert fragment in result["results"][0]["reason"]
    reason = json.loads((outbox / "rejected" / f"{name}.reason.json").read_text(encoding="utf-8"))
    assert reason["ok"] is False


def test_a_file_that_is_not_json_is_refused_not_interpreted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config, outbox = tmp_path / "config", tmp_path / "outbox"
    outbox.mkdir(parents=True, exist_ok=True)
    (outbox / "d.json").write_text("rm -rf /", encoding="utf-8")

    _, result = run(capsys, config, "apply-outbox", "--outbox", str(outbox), "--actor", "mori")
    assert result["rejected"] == 1
    assert "not JSON" in result["results"][0]["reason"]


def test_the_edit_is_recorded_under_the_name_the_board_knows(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config, outbox = tmp_path / "config", tmp_path / "outbox"
    _, created = run(capsys, config, "create", "--title", "티켓", "--assigned-to", "soa",
                     "--requested-by", "mori")
    item_id = created["item"]["id"]
    _queue(outbox, "e.json", {"work_id": item_id, "next_action": "x"})
    run(capsys, config, "apply-outbox", "--outbox", str(outbox), "--actor", "mori")

    _, history = run(capsys, config, "history", "--item-id", item_id)
    assert history["items"][0]["actor"] == "mori"


def test_a_queued_file_can_create_an_item(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Without this the queue can only edit work someone else already filed."""
    config, outbox = tmp_path / "config", tmp_path / "outbox"
    _queue(outbox, "n.json", {
        "op": "create",
        "title": "수집 판정이 실행 순서를 무시한다",
        "assigned_to": "local",
        "status": "ready",
        "next_action": "최신 정착 실행으로 판정하라",
    })

    _, result = run(capsys, config, "apply-outbox", "--outbox", str(outbox), "--actor", "mori")
    assert (result["applied"], result["rejected"]) == (1, 0)
    entry = result["results"][0]
    assert entry["reason"] == "created"

    _, shown = run(capsys, config, "show", entry["work_id"])
    item = shown["item"]
    assert item["assigned_to"] == "local"
    assert item["status"] == "ready"
    # The queue's owner is the requester by construction, never a claim in
    # the file.
    assert item["requested_by"] == "mori"


def test_a_queued_create_may_not_file_work_as_already_underway(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Filing straight to done would let a requester close work nobody did."""
    config, outbox = tmp_path / "config", tmp_path / "outbox"
    for name, status in (("d.json", "done"), ("p.json", "in_progress")):
        _queue(outbox, name, {
            "op": "create", "title": "t", "assigned_to": "local", "status": status,
        })

    _, result = run(capsys, config, "apply-outbox", "--outbox", str(outbox), "--actor", "mori")
    assert (result["applied"], result["rejected"]) == (0, 2)
    for entry in result["results"]:
        assert "may not be set on create" in entry["reason"]

    _, listed = run(capsys, config, "list")
    assert listed["count"] == 0


def test_a_queued_create_may_not_name_another_requester(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A board that misattributes who asked is worse than a rejected file."""
    config, outbox = tmp_path / "config", tmp_path / "outbox"
    _queue(outbox, "r.json", {
        "op": "create", "title": "t", "assigned_to": "local", "requested_by": "hk",
    })

    _, result = run(capsys, config, "apply-outbox", "--outbox", str(outbox), "--actor", "mori")
    assert (result["applied"], result["rejected"]) == (0, 1)
    assert "requested_by must be" in result["results"][0]["reason"]


def test_a_queued_create_naming_its_own_owner_is_accepted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Stating the truth explicitly is not an error."""
    config, outbox = tmp_path / "config", tmp_path / "outbox"
    _queue(outbox, "s.json", {
        "op": "create", "title": "t", "assigned_to": "local", "requested_by": "mori",
    })

    _, result = run(capsys, config, "apply-outbox", "--outbox", str(outbox), "--actor", "mori")
    assert (result["applied"], result["rejected"]) == (1, 0)


def test_a_queued_create_without_an_assignee_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The store's own create rules still apply through the queue."""
    config, outbox = tmp_path / "config", tmp_path / "outbox"
    _queue(outbox, "m.json", {"op": "create", "title": "t"})

    _, result = run(capsys, config, "apply-outbox", "--outbox", str(outbox), "--actor", "mori")
    assert (result["applied"], result["rejected"]) == (0, 1)
    assert "missing required fields" in result["results"][0]["reason"]


def test_an_unknown_op_is_refused_rather_than_guessed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config, outbox = tmp_path / "config", tmp_path / "outbox"
    _queue(outbox, "u.json", {"op": "archive", "work_id": "wi_0000000000000000"})

    _, result = run(capsys, config, "apply-outbox", "--outbox", str(outbox), "--actor", "mori")
    assert (result["applied"], result["rejected"]) == (0, 1)
    assert "unknown op" in result["results"][0]["reason"]


def test_a_file_without_an_op_is_still_an_edit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every queued file written before create existed omits op."""
    config, outbox = tmp_path / "config", tmp_path / "outbox"
    _, created = run(capsys, config, "create", "--title", "티켓", "--assigned-to", "soa",
                     "--requested-by", "mori")
    item_id = created["item"]["id"]
    _queue(outbox, "o.json", {"work_id": item_id, "next_action": "옛 형식"})

    _, result = run(capsys, config, "apply-outbox", "--outbox", str(outbox), "--actor", "mori")
    assert (result["applied"], result["rejected"]) == (1, 0)
    assert result["results"][0]["reason"] == "applied"
