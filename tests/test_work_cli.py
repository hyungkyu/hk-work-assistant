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


def test_the_queue_writes_only_the_two_fields_it_is_allowed(
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
        "status": "done",
        "blocker": "막혔다고 대신 말하려는 것",
    })

    _, result = run(capsys, config, "apply-outbox", "--outbox", str(outbox), "--actor", "mori")
    entry = result["results"][0]
    assert entry["applied"] == ["detail", "next_action"]
    assert entry["ignored"] == ["blocker", "progress_summary", "status"]

    _, shown = run(capsys, config, "show", item_id)
    assert shown["item"]["status"] != "done"
    assert shown["item"]["progress_summary"] == ""


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
