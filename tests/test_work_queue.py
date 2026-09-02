"""The four-stage queue: schema, migration, and the board's total partition.

The bug these guard against is the one that hid an in_progress item: a board
that renders an allow-list of statuses drops anything the list does not name,
silently, and reports the filtered count as if it were the whole picture.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from rlwrld_worklog.work_store import (
    BOARD_COLUMNS,
    DOCUMENT_VERSION,
    QUEUE_STAGES,
    RESIDUE_COLUMN_KEY,
    STATUSES,
    SUPPORTING_STATUSES,
    V1_STATUSES,
    WorkCorruptionError,
    WorkStore,
    _validate_board_columns,
    group_into_columns,
    status_metadata,
)

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)


def make_store(tmp_path: Path) -> WorkStore:
    return WorkStore(tmp_path / "config")


def create(store: WorkStore, **fields: object) -> dict:
    payload = {"title": "t", "requested_by": "hk", "assigned_to": "codex"}
    payload.update(fields)
    return store.create_item(payload, actor="hk")


# ------------------------------------------------------------- schema


def test_the_queue_has_four_stages_and_the_rest_are_supporting() -> None:
    assert QUEUE_STAGES == ("in_progress", "ready", "todo", "backlog")
    assert SUPPORTING_STATUSES == ("waiting", "blocked", "done", "cancelled")
    assert STATUSES == QUEUE_STAGES + SUPPORTING_STATUSES
    # Every status v1 could write is still a status, so no historical item can
    # become unreadable or need relabelling.
    assert set(V1_STATUSES) < set(STATUSES)
    assert set(STATUSES) - set(V1_STATUSES) == {"todo"}


def test_the_board_columns_are_a_total_partition_of_the_status_set() -> None:
    claimed = [status for column in BOARD_COLUMNS for status in column["statuses"]]
    assert sorted(claimed) == sorted(STATUSES)
    assert len(claimed) == len(set(claimed))
    _validate_board_columns()


def test_a_status_without_a_column_is_refused_at_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Adding a status without a column must fail loudly, not hide an item."""
    from rlwrld_worklog import work_store

    monkeypatch.setattr(work_store, "STATUSES", STATUSES + ("triage",))
    with pytest.raises(RuntimeError, match="do not claim: triage"):
        work_store._validate_board_columns()


def test_metadata_gives_a_client_everything_it_needs_to_lay_out_the_board() -> None:
    meta = status_metadata()
    assert meta["document_version"] == DOCUMENT_VERSION == 2
    assert meta["queue_stages"] == list(QUEUE_STAGES)
    assert meta["status_labels"]["todo"] == "해야 할 일"
    assert meta["status_labels"]["ready"] == "다음 할 일"
    assert meta["status_labels"]["backlog"] == "백로그"
    assert meta["status_groups"]["todo"] == "queue"
    assert meta["status_groups"]["waiting"] == "supporting"
    assert meta["residue_column"]["key"] == RESIDUE_COLUMN_KEY
    assert [column["key"] for column in meta["columns"]] == [
        "in_progress", "ready", "todo", "backlog", "held", "closed"
    ]


# --------------------------------------------------------- partition


def _item(identifier: str, status: str, *, updated: datetime = NOW) -> dict:
    return {
        "id": identifier,
        "status": status,
        "updated_at": updated.isoformat(),
        "completed_at": None,
    }


def test_every_item_lands_in_exactly_one_column() -> None:
    items = [_item(f"wi_{index:016x}", status) for index, status in enumerate(STATUSES)]
    columns = group_into_columns(items, now=NOW)
    placed = [item["id"] for column in columns for item in column["items"]]
    assert sorted(placed) == sorted(item["id"] for item in items)
    assert len(placed) == len(set(placed))
    assert columns[-1]["key"] == RESIDUE_COLUMN_KEY and columns[-1]["count"] == 0


def test_a_status_no_column_claims_surfaces_instead_of_vanishing() -> None:
    """The exact failure mode that hid wi_10873fa6f98c05ee's class of item."""
    items = [_item("wi_" + "a" * 16, "in_progress"), _item("wi_" + "b" * 16, "unheard_of")]
    columns = group_into_columns(items, now=NOW)
    residue = columns[-1]
    assert residue["key"] == RESIDUE_COLUMN_KEY
    assert [item["id"] for item in residue["items"]] == ["wi_" + "b" * 16]
    assert residue["statuses"] == ["unheard_of"]
    total_placed = sum(column["count"] for column in columns)
    assert total_placed == len(items), "no item may be dropped by the board"


def test_an_item_aged_out_of_a_dated_column_is_counted_not_lost() -> None:
    old = _item("wi_" + "c" * 16, "done", updated=NOW - timedelta(days=40))
    columns = group_into_columns([old], now=NOW)
    assert all(column["count"] == 0 for column in columns)
    # Ageing out of 최근 완료 is deliberate, so it is reported separately from
    # the residue rather than counted as a layout defect.
    assert columns[-1]["aged_out"] == 1
    assert columns[-1]["count"] == 0


def test_an_unparseable_timestamp_keeps_the_item_on_the_board() -> None:
    broken = {"id": "wi_" + "d" * 16, "status": "done", "updated_at": "??", "completed_at": None}
    columns = group_into_columns([broken], now=NOW)
    assert sum(column["count"] for column in columns) == 1


# --------------------------------------------------------- migration


def test_an_existing_v1_document_is_read_and_migrated_without_loss(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    for status in V1_STATUSES:
        create(store, status=status, title=f"item {status}")
    before = json.loads(store.items_path.read_text(encoding="utf-8"))
    before["version"] = 1
    store.items_path.write_text(json.dumps(before, ensure_ascii=False, indent=2), encoding="utf-8")

    document = store.read_document()
    assert document["version"] == 2
    assert document["migrated_from"] == 1
    assert [item["status"] for item in document["items"]] == [
        item["status"] for item in before["items"]
    ]
    assert len(document["items"]) == len(V1_STATUSES)
    # Reading never rewrites: the file is still v1 until the next real write.
    assert json.loads(store.items_path.read_text(encoding="utf-8"))["version"] == 1

    create(store, status="todo", title="새 todo")
    stored = json.loads(store.items_path.read_text(encoding="utf-8"))
    assert stored["version"] == 2
    assert len(stored["items"]) == len(V1_STATUSES) + 1
    assert store.read_document()["migrated_from"] is None


def test_a_v1_document_containing_todo_is_corruption_not_a_silent_upgrade(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    create(store, status="todo")
    stored = json.loads(store.items_path.read_text(encoding="utf-8"))
    raw = json.dumps({**stored, "version": 1}, ensure_ascii=False)
    store.items_path.write_text(raw, encoding="utf-8")
    with pytest.raises(WorkCorruptionError, match="status is not one of"):
        store.read_document()
    assert store.items_path.read_text(encoding="utf-8") == raw


def test_todo_is_accepted_end_to_end_and_behaves_as_a_queue_stage(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    item = create(store, status="todo")
    assert item["status"] == "todo"
    assert item["started_at"] is None and item["completed_at"] is None
    moved = store.update_item(item["id"], {"status": "in_progress"}, actor="hk")
    assert moved["started_at"] is not None
    back = store.update_item(moved["id"], {"status": "todo"}, actor="hk")
    assert back["completed_at"] is None
    finished = store.update_item(back["id"], {"status": "done"}, actor="hk")
    assert finished["completed_at"] is not None
    reopened = store.update_item(finished["id"], {"status": "todo"}, actor="hk")
    assert reopened["completed_at"] is None


# ------------------------------------------------------------ counts


def test_listing_reports_the_whole_live_set_beside_the_filtered_slice(
    tmp_path: Path,
) -> None:
    """A filtered view must never be able to pass for the complete picture."""
    store = make_store(tmp_path)
    create(store, status="in_progress", assigned_to="codex")
    create(store, status="in_progress", assigned_to="claude-code")
    create(store, status="todo", assigned_to="claude-code")

    everything = store.list_items()
    assert everything["count"] == everything["total"] == 3
    assert everything["status_counts"] == {"in_progress": 2, "todo": 1}

    filtered = store.list_items(assigned_to="codex")
    assert filtered["count"] == 1
    assert filtered["total"] == 3, "total stays the size of the live set"
    assert filtered["status_counts"] == {"in_progress": 2, "todo": 1}

    by_status = store.list_items(statuses=["todo"])
    assert by_status["count"] == 1 and by_status["total"] == 3


def test_archived_items_stay_out_of_the_totals(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    keep = create(store, status="todo")
    gone = create(store, status="todo")
    store.archive_item(gone["id"], actor="hk")
    live = store.list_items()
    assert live["total"] == 1 and live["status_counts"] == {"todo": 1}
    assert [item["id"] for item in live["items"]] == [keep["id"]]
    assert store.list_items(include_archived=True)["total"] == 2
