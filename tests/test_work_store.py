# hook-allow: synthetic-credentials
from __future__ import annotations

import hashlib
import json
import os
import stat
import threading
from pathlib import Path

import pytest

from rlwrld_worklog.work_store import (
    DOCUMENT_VERSION,
    WorkConflictError,
    WorkCorruptionError,
    WorkNotFoundError,
    WorkStore,
    WorkValidationError,
)


def make_store(tmp_path: Path) -> WorkStore:
    return WorkStore(tmp_path / "config")


def seed(store: WorkStore, **overrides: object) -> dict:
    fields = {"title": "위임 작업", "requested_by": "hk", "assigned_to": "codex"}
    fields.update(overrides)
    return store.create_item(fields, actor="hk")


def test_create_sets_server_owned_fields_and_defaults(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    item = seed(store, next_action="스키마 확정")

    assert item["id"].startswith("wi_")
    assert item["revision"] == 1
    assert item["status"] == "backlog"
    assert item["priority"] == "normal"
    assert item["archived_at"] is None
    assert item["created_at"] == item["updated_at"]
    assert item["started_at"] is None
    assert store.get_item(item["id"]) == item


def test_document_is_versioned_atomic_and_private(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    seed(store)

    document = json.loads(store.items_path.read_text(encoding="utf-8"))
    assert document["version"] == DOCUMENT_VERSION
    assert document["revision"] == 1
    assert len(document["items"]) == 1
    assert stat.S_IMODE(store.items_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.directory.stat().st_mode) == 0o700
    # An atomic replace leaves no partial temporary file behind.
    assert sorted(path.name for path in store.directory.iterdir()) == [
        "history.jsonl",
        "items.json",
        "items.lock",
    ]


def test_store_lives_only_under_the_configured_config_root(tmp_path: Path) -> None:
    store = WorkStore(tmp_path / "config")
    seed(store)
    assert store.items_path == tmp_path / "config" / "work" / "items.json"

    os.environ["APP_CONFIG_ROOT"] = str(tmp_path / "environment")
    try:
        assert WorkStore.from_environment().root == tmp_path / "environment"
    finally:
        del os.environ["APP_CONFIG_ROOT"]


def test_status_transitions_maintain_timestamps(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    item = seed(store)

    started = store.update_item(item["id"], {"status": "in_progress"}, actor="codex")
    assert started["started_at"] is not None
    assert started["completed_at"] is None

    finished = store.update_item(item["id"], {"status": "done"}, actor="codex")
    assert finished["completed_at"] is not None

    reopened = store.update_item(finished["id"], {"status": "in_progress"}, actor="codex")
    assert reopened["completed_at"] is None
    assert reopened["started_at"] == started["started_at"]


def test_validation_rejects_bad_values(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    with pytest.raises(WorkValidationError, match="missing required fields"):
        store.create_item({"title": "제목만"}, actor="hk")
    with pytest.raises(WorkValidationError, match="status must be one of"):
        seed(store, status="in-progress")
    with pytest.raises(WorkValidationError, match="priority must be one of"):
        seed(store, priority="p0")
    with pytest.raises(WorkValidationError, match="title must be at most"):
        seed(store, title="가" * 201)
    with pytest.raises(WorkValidationError, match="assigned_to must be"):
        seed(store, assigned_to="agent with spaces")
    with pytest.raises(WorkValidationError, match="due_at must be an ISO"):
        seed(store, due_at="next tuesday")
    with pytest.raises(WorkValidationError, match="title is required"):
        seed(store, title="   ")


def test_unknown_and_server_owned_fields_are_rejected(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    item = seed(store)
    with pytest.raises(WorkValidationError, match="unknown fields: secret_token"):
        store.update_item(item["id"], {"secret_token": "value"}, actor="hk")
    with pytest.raises(WorkValidationError, match="read-only fields"):
        store.update_item(item["id"], {"revision": 99}, actor="hk")
    with pytest.raises(WorkValidationError, match="read-only fields"):
        store.create_item(
            {"id": "wi_" + "0" * 16, "title": "t", "requested_by": "hk", "assigned_to": "codex"},
            actor="hk",
        )
    assert store.get_item(item["id"])["revision"] == 1


def test_parent_references_are_validated_and_cycles_are_rejected(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    parent = seed(store, title="상위")
    child = seed(store, title="하위", parent_id=parent["id"])
    grandchild = seed(store, title="손자", parent_id=child["id"])

    with pytest.raises(WorkValidationError, match="parent work item not found"):
        seed(store, parent_id="wi_" + "0" * 16)
    with pytest.raises(WorkValidationError, match="cannot be its own parent"):
        store.update_item(child["id"], {"parent_id": child["id"]}, actor="hk")
    with pytest.raises(WorkValidationError, match="cycle"):
        store.update_item(parent["id"], {"parent_id": grandchild["id"]}, actor="hk")
    assert store.get_item(parent["id"])["parent_id"] is None


def test_archive_is_a_soft_delete_that_keeps_history(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    item = seed(store)
    archived = store.archive_item(item["id"], actor="hk")

    assert archived["archived_at"] is not None
    assert store.list_items()["items"] == []
    assert [entry["id"] for entry in store.list_items(include_archived=True)["items"]] == [item["id"]]
    assert store.get_item(item["id"])["archived_at"] is not None
    with pytest.raises(WorkConflictError, match="already archived"):
        store.archive_item(item["id"], actor="hk")
    with pytest.raises(WorkConflictError, match="is archived"):
        store.update_item(item["id"], {"status": "done"}, actor="hk")

    actions = [entry["action"] for entry in store.read_history()]
    assert actions == ["work.archived", "work.created"]


def test_archiving_a_parent_requires_archiving_children_first(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    parent = seed(store, title="상위")
    child = seed(store, title="하위", parent_id=parent["id"])

    with pytest.raises(WorkConflictError, match="archive the child items first"):
        store.archive_item(parent["id"], actor="hk")
    store.archive_item(child["id"], actor="hk")
    assert store.archive_item(parent["id"], actor="hk")["archived_at"] is not None


def test_optimistic_concurrency_rejects_a_stale_writer(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    item = seed(store)
    first = store.update_item(
        item["id"], {"progress_summary": "코덱스 진행"}, actor="codex", expected_revision=1
    )

    with pytest.raises(WorkConflictError, match="revision 2, not 1"):
        store.update_item(item["id"], {"progress_summary": "덮어쓰기"}, actor="claude-code", expected_revision=1)
    with pytest.raises(WorkConflictError, match="was updated at"):
        store.update_item(
            item["id"],
            {"next_action": "덮어쓰기"},
            actor="claude-code",
            expected_updated_at=item["updated_at"],
        )
    assert store.get_item(item["id"])["progress_summary"] == "코덱스 진행"
    assert store.update_item(
        item["id"],
        {"next_action": "다음"},
        actor="claude-code",
        expected_updated_at=first["updated_at"],
    )["revision"] == 3


def test_concurrent_writers_do_not_lose_updates(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    barrier = threading.Barrier(8)
    failures: list[BaseException] = []

    def create(index: int) -> None:
        try:
            barrier.wait(timeout=10)
            WorkStore(store.root).create_item(
                {"title": f"동시 작업 {index}", "requested_by": "hk", "assigned_to": "codex"},
                actor="hk",
            )
        except BaseException as error:  # noqa: BLE001 - reported to the test
            failures.append(error)

    threads = [threading.Thread(target=create, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    assert failures == []
    document = store.read_document()
    assert len(document["items"]) == 8
    assert document["revision"] == 8
    assert len({item["id"] for item in document["items"]}) == 8


def test_concurrent_updates_with_the_same_expectation_conflict(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    item = seed(store)
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def update(value: str) -> None:
        barrier.wait(timeout=10)
        try:
            WorkStore(store.root).update_item(
                item["id"], {"progress_summary": value}, actor="codex", expected_revision=1
            )
            outcomes.append("ok")
        except WorkConflictError:
            outcomes.append("conflict")

    threads = [threading.Thread(target=update, args=(value,)) for value in ("첫째", "둘째")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    assert sorted(outcomes) == ["conflict", "ok"]
    assert store.get_item(item["id"])["revision"] == 2


def test_corruption_is_surfaced_and_the_file_is_never_overwritten(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    seed(store)
    store.items_path.write_text("{ this is not json", encoding="utf-8")

    with pytest.raises(WorkCorruptionError, match="not valid JSON"):
        store.read_document()
    with pytest.raises(WorkCorruptionError):
        store.list_items()
    with pytest.raises(WorkCorruptionError):
        seed(store)
    assert store.items_path.read_text(encoding="utf-8") == "{ this is not json"


def test_an_unsupported_document_version_is_not_downgraded(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    seed(store)
    future = json.dumps({"version": 99, "revision": 1, "items": []})
    store.items_path.write_text(future, encoding="utf-8")

    with pytest.raises(WorkCorruptionError, match="unsupported work store version"):
        store.read_document()
    with pytest.raises(WorkCorruptionError):
        seed(store)
    assert store.items_path.read_text(encoding="utf-8") == future


def test_history_is_append_only_and_keeps_what_a_field_became(tmp_path: Path) -> None:
    """Values are kept from 2026-09-04 (HK P0 condition 5). What that costs.

    Until now this test asserted the opposite: that a token-shaped string in
    `detail` never reached the history. It does now, and the trade is worth
    stating rather than quietly dropping. The confidentiality boundary is
    unchanged -- both files are 0600 in the same directory, so anyone who can
    read one can read the other. What is new is permanence: a secret pasted
    into an item could previously be edited out, and now it stays in an
    append-only stream. That is a reason to keep secrets off the board, not a
    reason to keep the board's history blind.
    """
    store = make_store(tmp_path)
    item = seed(store, detail="xoxp-not-a-real-token-but-sensitive-looking")
    store.update_item(item["id"], {"status": "in_progress", "progress_summary": "비밀 요약"}, actor="codex")
    store.update_item(item["id"], {"status": "done"}, actor="codex")

    raw = store.history_path.read_text(encoding="utf-8")
    assert "비밀 요약" in raw
    assert stat.S_IMODE(store.history_path.stat().st_mode) == 0o600

    entries = store.read_history()
    assert [entry["action"] for entry in entries] == ["work.updated", "work.updated", "work.created"]
    assert entries[0]["status"] == "done"
    assert entries[0]["status_from"] == "in_progress"
    assert entries[1]["fields"] == ["progress_summary", "status"]
    assert store.read_history(item_id="wi_" + "0" * 16) == []


def test_upsert_creates_then_updates_by_source_ref(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    created = store.upsert_item(
        {"title": "연동 작업", "requested_by": "hk", "assigned_to": "codex"},
        actor="hk",
        source_ref="github:RLWRLD/worklog#12",
    )
    assert created["created"] is True
    assert created["item"]["source_ref"] == "github:RLWRLD/worklog#12"

    updated = store.upsert_item(
        {"progress_summary": "리뷰 대기", "status": "waiting"},
        actor="codex",
        source_ref="github:RLWRLD/worklog#12",
        expected_revision=1,
    )
    assert updated["created"] is False
    assert updated["item"]["id"] == created["item"]["id"]
    assert updated["item"]["revision"] == 2
    assert len(store.list_items()["items"]) == 1

    with pytest.raises(WorkValidationError, match="requires an id or a source_ref"):
        store.upsert_item({"title": "x"}, actor="hk")
    with pytest.raises(WorkNotFoundError):
        store.upsert_item({"title": "x"}, actor="hk", item_id="wi_" + "0" * 16)


def test_upsert_will_not_overwrite_an_existing_item_without_an_expectation(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    created = store.upsert_item(
        {"title": "연동 작업", "requested_by": "hk", "assigned_to": "codex"},
        actor="hk",
        source_ref="github:RLWRLD/worklog#12",
    )["item"]
    # A human edits the item between the agent's read and its write.
    store.update_item(created["id"], {"progress_summary": "사람이 남긴 메모"}, actor="hk")

    with pytest.raises(WorkValidationError, match="pass expected_revision"):
        store.upsert_item(
            {"progress_summary": "에이전트가 덮어쓴 값"},
            actor="codex",
            source_ref="github:RLWRLD/worklog#12",
        )
    with pytest.raises(WorkConflictError, match="revision 2, not 1"):
        store.upsert_item(
            {"progress_summary": "에이전트가 덮어쓴 값"},
            actor="codex",
            source_ref="github:RLWRLD/worklog#12",
            expected_revision=1,
        )
    assert store.get_item(created["id"])["progress_summary"] == "사람이 남긴 메모"
    assert store.get_item(created["id"])["revision"] == 2


def test_upsert_accepts_a_current_expectation_by_revision_or_timestamp(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    created = store.upsert_item(
        {"title": "연동 작업", "requested_by": "hk", "assigned_to": "codex"},
        actor="hk",
        source_ref="ref/1",
    )["item"]

    by_revision = store.upsert_item(
        {"status": "in_progress"}, actor="codex", source_ref="ref/1", expected_revision=1
    )
    assert by_revision["created"] is False
    assert by_revision["item"]["revision"] == 2

    by_timestamp = store.upsert_item(
        {"progress_summary": "진행"},
        actor="codex",
        source_ref="ref/1",
        expected_updated_at=by_revision["item"]["updated_at"],
    )
    assert by_timestamp["item"]["revision"] == 3
    assert by_timestamp["item"]["id"] == created["id"]


def test_upsert_force_overwrite_is_deliberate_last_write_wins(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    created = store.upsert_item(
        {"title": "연동 작업", "requested_by": "hk", "assigned_to": "codex"},
        actor="hk",
        source_ref="ref/1",
    )["item"]
    store.update_item(created["id"], {"progress_summary": "사람이 남긴 메모"}, actor="hk")

    forced = store.upsert_item(
        {"progress_summary": "기계가 덮어쓴 값"},
        actor="codex",
        source_ref="ref/1",
        force_overwrite=True,
    )
    assert forced["created"] is False
    assert forced["item"]["progress_summary"] == "기계가 덮어쓴 값"
    assert forced["item"]["revision"] == 3

    # force only waives the requirement to supply an expectation; a stale one
    # that is supplied anyway is still honoured.
    with pytest.raises(WorkConflictError):
        store.upsert_item(
            {"progress_summary": "더 오래된 값"},
            actor="codex",
            source_ref="ref/1",
            expected_revision=1,
            force_overwrite=True,
        )


def test_upsert_create_path_needs_no_expectation_and_rejects_a_pointless_one(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    created = store.upsert_item(
        {"title": "새 작업", "requested_by": "hk", "assigned_to": "codex"},
        actor="hk",
        source_ref="ref/new",
    )
    assert created["created"] is True

    with pytest.raises(WorkValidationError, match="nothing to expect a revision of"):
        store.upsert_item(
            {"title": "다른 작업", "requested_by": "hk", "assigned_to": "codex"},
            actor="hk",
            source_ref="ref/absent",
            expected_revision=1,
        )
    assert len(store.list_items()["items"]) == 1


def test_concurrent_upserts_with_the_same_expectation_conflict(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.upsert_item(
        {"title": "연동 작업", "requested_by": "hk", "assigned_to": "codex"},
        actor="hk",
        source_ref="ref/race",
    )
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def race(value: str) -> None:
        barrier.wait(timeout=10)
        try:
            WorkStore(store.root).upsert_item(
                {"progress_summary": value},
                actor="codex",
                source_ref="ref/race",
                expected_revision=1,
            )
            outcomes.append("ok")
        except WorkConflictError:
            outcomes.append("conflict")

    threads = [threading.Thread(target=race, args=(value,)) for value in ("첫째", "둘째")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    assert sorted(outcomes) == ["conflict", "ok"]
    assert store.list_items()["items"][0]["revision"] == 2


def test_list_filters_and_orders_by_priority_then_recency(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    low = seed(store, title="낮음", priority="low")
    urgent = seed(store, title="긴급", priority="urgent", assigned_to="claude-code")
    normal = seed(store, title="보통")

    ordered = [item["id"] for item in store.list_items()["items"]]
    assert ordered[0] == urgent["id"]
    assert ordered[-1] == low["id"]
    assert normal["id"] in ordered

    assert [item["id"] for item in store.list_items(assigned_to="claude-code")["items"]] == [urgent["id"]]
    assert store.list_items(statuses=["done"])["items"] == []
    with pytest.raises(WorkValidationError, match="status must be one of"):
        store.list_items(statuses=["nonsense"])


# --------------------------------------------------------------------------
# Stored-document validation.
#
# Every read validates the complete document.  Anything that does not match the
# exact v1 schema is corruption: the file must be left byte-for-byte alone and a
# mutation attempted against it must not append to the history either.
# --------------------------------------------------------------------------

BASE_TIME = "2026-09-01T00:00:00+00:00"
LATER_TIME = "2026-09-02T00:00:00+00:00"


def item_id(number: int) -> str:
    return f"wi_{number:016x}"


def stored_item(identifier: str, **overrides: object) -> dict:
    item = {
        "id": identifier,
        "title": "제목",
        "detail": None,
        "status": "backlog",
        "priority": "normal",
        "requested_by": "hk",
        "assigned_to": "codex",
        "parent_id": None,
        "progress_summary": "",
        "next_action": "",
        "blocker": None,
        "created_at": BASE_TIME,
        "updated_at": BASE_TIME,
        "started_at": None,
        "completed_at": None,
        "due_at": None,
        "source_ref": None,
        "archived_at": None,
        "revision": 1,
    }
    item.update(overrides)
    return item


def stored_document(items: list, **overrides: object) -> dict:
    document = {"version": 1, "revision": len(items), "updated_at": BASE_TIME, "items": items}
    document.update(overrides)
    return document


def write_document(store: WorkStore, document: dict) -> bytes:
    store.items_path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return store.items_path.read_bytes()


def history_bytes(store: WorkStore) -> bytes:
    return store.history_path.read_bytes() if store.history_path.exists() else b""


def chain(length: int) -> list:
    return [
        stored_item(item_id(index), parent_id=item_id(index - 1) if index > 1 else None)
        for index in range(1, length + 1)
    ]


def test_a_hand_written_valid_document_round_trips(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    write_document(
        store,
        stored_document(
            [
                stored_item(item_id(1)),
                stored_item(item_id(2), parent_id=item_id(1), status="in_progress", revision=4),
                stored_item(item_id(3), archived_at=BASE_TIME),
                stored_item(item_id(4), parent_id=item_id(3), archived_at=BASE_TIME),
            ]
        ),
    )

    document = store.read_document()
    assert [item["id"] for item in document["items"]] == [item_id(number) for number in (1, 2, 3, 4)]
    assert document["items"][1]["revision"] == 4
    assert store.create_item(
        {"title": "새 작업", "requested_by": "hk", "assigned_to": "codex"}, actor="hk"
    )["revision"] == 1
    assert len(store.read_document()["items"]) == 5


def _drop(field: str):
    def mutate(document: dict) -> None:
        document["items"][0].pop(field)

    return mutate


def _set(field: str, value: object):
    def mutate(document: dict) -> None:
        document["items"][0][field] = value

    return mutate


def _top(field: str, value: object):
    def mutate(document: dict) -> None:
        document[field] = value

    return mutate


def _pop_top(field: str):
    def mutate(document: dict) -> None:
        document.pop(field)

    return mutate


CORRUPTIONS = [
    ("item-missing-field", _drop("next_action"), "missing fields: next_action"),
    ("item-missing-archived-at", _drop("archived_at"), "missing fields: archived_at"),
    ("item-unknown-field", _set("owner", "codex"), "unknown fields: owner"),
    ("item-not-an-object", _top("items", ["nope"]), "items\\[0\\] must be an object"),
    ("id-malformed", _set("id", "work-1"), "id is not a work item identifier"),
    ("status-invalid", _set("status", "in-progress"), "status is not one of"),
    ("status-null", _set("status", None), "status is not one of"),
    ("priority-invalid", _set("priority", "p0"), "priority is not one of"),
    ("assigned-to-invalid", _set("assigned_to", "Codex Agent"), "assigned_to is not an identifier"),
    ("assigned-to-not-a-string", _set("assigned_to", 7), "assigned_to is not an identifier"),
    ("title-empty", _set("title", "   "), "title must not be empty"),
    ("title-not-a-string", _set("title", 12), "title must be a string"),
    ("title-too-long", _set("title", "가" * 201), "title is longer than 200"),
    ("required-text-null", _set("progress_summary", None), "progress_summary must be a string"),
    ("optional-text-wrong-type", _set("blocker", 5), "blocker must be a string"),
    ("created-at-not-a-timestamp", _set("created_at", "yesterday"), "created_at is not an ISO"),
    ("created-at-null", _set("created_at", None), "created_at must be a timestamp"),
    ("timestamp-without-offset", _set("updated_at", "2026-09-01T00:00:00"), "must carry a UTC offset"),
    ("due-at-not-a-timestamp", _set("due_at", "someday"), "due_at is not an ISO"),
    ("updated-at-precedes-created-at", _set("created_at", LATER_TIME), "updated_at precedes created_at"),
    ("archived-at-after-updated-at", _set("archived_at", LATER_TIME), "archived_at is outside"),
    ("revision-zero", _set("revision", 0), "revision must be an integer of at least 1"),
    ("revision-not-an-integer", _set("revision", "3"), "revision must be an integer of at least 1"),
    ("revision-boolean", _set("revision", True), "revision must be an integer of at least 1"),
    ("parent-id-malformed", _set("parent_id", "wi_zzz"), "parent_id is not a work item identifier"),
    ("document-missing-field", _pop_top("updated_at"), "document is missing fields: updated_at"),
    ("document-missing-items", _pop_top("items"), "document is missing fields: items"),
    ("document-updated-at-null-after-writes", _top("updated_at", None), "updated_at must be a timestamp"),
    ("document-unknown-field", _top("note", "hand edited"), "unknown fields: note"),
    ("document-revision-negative", _top("revision", -1), "revision must be a non-negative integer"),
    ("document-revision-not-an-integer", _top("revision", 1.5), "revision must be a non-negative integer"),
    ("document-updated-at-invalid", _top("updated_at", "soon"), "document updated_at is not an ISO"),
    ("document-items-not-a-list", _top("items", {}), "items must be a list"),
]


@pytest.mark.parametrize(
    ("mutate", "message"),
    [pytest.param(mutate, message, id=name) for name, mutate, message in CORRUPTIONS],
)
def test_invalid_stored_documents_are_corruption(tmp_path: Path, mutate, message: str) -> None:
    store = make_store(tmp_path)
    document = stored_document([stored_item(item_id(1)), stored_item(item_id(2))])
    mutate(document)
    raw = write_document(store, document)
    history_before = history_bytes(store)

    with pytest.raises(WorkCorruptionError, match=message):
        store.read_document()
    with pytest.raises(WorkCorruptionError):
        store.list_items()
    with pytest.raises(WorkCorruptionError):
        store.get_item(item_id(1))

    # A mutation must stop at the read, before committing or writing history.
    with pytest.raises(WorkCorruptionError):
        store.create_item({"title": "t", "requested_by": "hk", "assigned_to": "codex"}, actor="hk")
    with pytest.raises(WorkCorruptionError):
        store.update_item(item_id(1), {"status": "done"}, actor="hk")
    with pytest.raises(WorkCorruptionError):
        store.archive_item(item_id(1), actor="hk")
    with pytest.raises(WorkCorruptionError):
        store.upsert_item({"title": "t"}, actor="hk", source_ref="ref/1")

    assert store.items_path.read_bytes() == raw
    assert history_bytes(store) == history_before


GRAPH_CORRUPTIONS = [
    (
        "duplicate-ids",
        [stored_item(item_id(1)), stored_item(item_id(1), title="복제")],
        "duplicate work item id",
    ),
    (
        "missing-parent",
        [stored_item(item_id(1), parent_id=item_id(9))],
        "references a missing parent",
    ),
    (
        "self-parent",
        [stored_item(item_id(1), parent_id=item_id(1))],
        "is its own parent",
    ),
    (
        "archived-parent-of-a-live-child",
        [stored_item(item_id(1), archived_at=BASE_TIME), stored_item(item_id(2), parent_id=item_id(1))],
        "has an archived parent",
    ),
    (
        "two-item-cycle",
        [
            stored_item(item_id(1), parent_id=item_id(2)),
            stored_item(item_id(2), parent_id=item_id(1)),
        ],
        "parent cycle",
    ),
    ("chain-too-deep", chain(10), "deeper than 8 levels"),
]


@pytest.mark.parametrize(
    ("items", "message"),
    [pytest.param(items, message, id=name) for name, items, message in GRAPH_CORRUPTIONS],
)
def test_invalid_stored_parent_graphs_are_corruption(
    tmp_path: Path, items: list, message: str
) -> None:
    store = make_store(tmp_path)
    raw = write_document(store, stored_document(items))
    history_before = history_bytes(store)

    with pytest.raises(WorkCorruptionError, match=message):
        store.read_document()
    with pytest.raises(WorkCorruptionError):
        store.create_item({"title": "t", "requested_by": "hk", "assigned_to": "codex"}, actor="hk")

    assert store.items_path.read_bytes() == raw
    assert history_bytes(store) == history_before


def test_a_parent_chain_at_the_limit_is_still_valid(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    write_document(store, stored_document(chain(9)))
    assert len(store.read_document()["items"]) == 9


def test_corruption_never_costs_a_previously_written_history(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    item = seed(store)
    store.update_item(item["id"], {"status": "in_progress"}, actor="codex")
    history_before = history_bytes(store)
    raw = store.items_path.read_bytes()

    document = json.loads(raw)
    document["items"][0]["status"] = "정지"
    write_document(store, document)
    corrupt_raw = store.items_path.read_bytes()

    with pytest.raises(WorkCorruptionError):
        store.update_item(item["id"], {"next_action": "이어서"}, actor="codex")

    assert store.items_path.read_bytes() == corrupt_raw
    assert history_bytes(store) == history_before


def test_an_unknown_version_is_reported_before_any_field_validation(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    # A v3 document is refused because no loader or migration is registered for
    # it, not because its extra fields look unfamiliar to the current schema.
    # It is preserved intact so a future release can migrate it.
    raw = write_document(
        store, {"version": 3, "revision": 1, "updated_at": BASE_TIME, "items": [], "owners": []}
    )
    history_before = history_bytes(store)
    with pytest.raises(WorkCorruptionError, match="unsupported work store version 3"):
        store.read_document()
    with pytest.raises(WorkCorruptionError, match="unsupported work store version 3"):
        store.create_item({"title": "t", "requested_by": "hk", "assigned_to": "codex"}, actor="hk")
    assert store.items_path.read_bytes() == raw
    assert history_bytes(store) == history_before

    for version in ("1", 1.0, None, True):
        write_document(store, {"version": version, "revision": 0, "updated_at": None, "items": []})
        with pytest.raises(WorkCorruptionError, match="unsupported work store version"):
            store.read_document()


def test_history_keeps_both_sides_of_a_change(tmp_path: Path) -> None:
    """A record that says what changed but not what it became is a notification."""
    store = make_store(tmp_path)
    item = seed(store, detail="처음")
    store.update_item(item["id"], {"detail": "다음"}, actor="codex")

    entry = store.read_history(item_id=item["id"])[0]
    assert entry["values"]["detail"]["before"] == {"present": True, "value": "처음"}
    assert entry["values"]["detail"]["after"] == {"present": True, "value": "다음"}


def test_a_field_that_was_absent_is_not_reported_as_empty(tmp_path: Path) -> None:
    """"There was no field" and "the field held nothing" are different facts."""
    store = make_store(tmp_path)
    item = seed(store)
    store.update_item(item["id"], {"blocker": "막혔다"}, actor="codex")

    entry = store.read_history(item_id=item["id"])[0]
    # The item carried `blocker` from creation, holding null. That is a value.
    assert entry["values"]["blocker"]["before"] == {"present": True, "value": None}

    # `title` did not exist before the item did. That is not the same thing.
    created = store.read_history(item_id=item["id"])[-1]
    assert created["values"]["title"]["before"] == {"present": False}


def test_a_long_value_is_clipped_and_says_so(tmp_path: Path) -> None:
    """A cap that hides its own effect would be a lie."""
    store = make_store(tmp_path)
    item = seed(store)
    long_detail = "가" * 3_000
    store.update_item(item["id"], {"detail": long_detail}, actor="codex")

    after = store.read_history(item_id=item["id"])[0]["values"]["detail"]["after"]
    assert after["truncated"] is True
    assert after["length"] == 3_000
    assert len(after["value"]) == 2_000
    assert after["sha256"] == hashlib.sha256(long_detail.encode("utf-8")).hexdigest()


def test_entries_written_before_values_existed_are_reported_empty(tmp_path: Path) -> None:
    """Not reconstructed from the item as it stands: that would be a guess."""
    store = make_store(tmp_path)
    item = seed(store)
    line = json.loads(store.history_path.read_text(encoding="utf-8").splitlines()[0])
    line.pop("values")
    store.history_path.write_text(json.dumps(line, ensure_ascii=False) + "\n", encoding="utf-8")

    assert store.read_history(item_id=item["id"])[0]["values"] == {}


def test_the_board_says_what_it_is_not_showing(tmp_path: Path) -> None:
    """An archived item leaves every view at once, whatever state it was in."""
    store = make_store(tmp_path)
    finished = seed(store)
    store.update_item(finished["id"], {"status": "done"}, actor="codex")
    store.archive_item(finished["id"], actor="codex")

    unfinished = seed(store)
    store.update_item(unfinished["id"], {"status": "in_progress"}, actor="codex")
    store.archive_item(unfinished["id"], actor="codex")

    withheld = store.list_items()["withheld"]
    assert withheld == {"archived": 2, "archived_unfinished": 1, "included": False}
    # Asking for them says so, so the number cannot be read as a live count.
    assert store.list_items(include_archived=True)["withheld"]["included"] is True
