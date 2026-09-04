"""The work-item activity timeline.

What these hold down: the named fields are actually recorded, a record written
before those fields existed is reported as `legacy` rather than back-filled,
receipts are local identifiers, and the item's own free text never reaches the
history stream.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

import pytest

from rlwrld_worklog.work_store import PHASES, WorkStore, WorkValidationError


@pytest.fixture()
def store(tmp_path: Path) -> Iterator[WorkStore]:
    yield WorkStore(tmp_path)


def seed(store: WorkStore, **fields: Any) -> dict[str, Any]:
    payload = {"title": "위임 작업", "assigned_to": "moa", "requested_by": "hk"}
    payload.update(fields)
    return store.create_item(payload, actor="moa", context={"phase": "assigned"})


def entries(store: WorkStore, item_id: str) -> list[dict[str, Any]]:
    return store.read_timeline(item_id)["entries"]


# ------------------------------------------------------------- recording


def test_the_timeline_records_every_field_the_board_promises(store: WorkStore) -> None:
    item = seed(store)
    store.update_item(
        item["id"],
        {"status": "in_progress"},
        actor="moa",
        expected_revision=1,
        context={
            "directed_by": "ari",
            "phase": "started",
            "session_id": "sess-abc",
            "summary": "구현 착수",
            "receipt": "cowork/handoffs/x.json",
        },
    )
    latest = entries(store, item["id"])[-1]
    assert latest["actor"]["name"] == "moa"
    assert latest["directed_by"]["name"] == "ari"
    assert latest["requested_by"] == "hk"
    assert latest["assigned_to"] == "moa"
    assert latest["phase"] == "started"
    assert latest["session_id"] == "sess-abc"
    assert latest["summary"] == "구현 착수"
    assert latest["receipt"] == "cowork/handoffs/x.json"
    assert latest["at"] and latest["revision"] == 2
    assert latest["record_schema"] == "current"


def test_a_change_without_context_still_records_the_item_level_actors(store: WorkStore) -> None:
    item = store.create_item(
        {"title": "t", "assigned_to": "moa", "requested_by": "hk"}, actor="moa"
    )
    entry = entries(store, item["id"])[-1]
    assert entry["requested_by"] == "hk" and entry["assigned_to"] == "moa"
    assert entry["record_schema"] == "current"
    assert entry["phase"] is None


@pytest.mark.parametrize("phase", PHASES)
def test_every_declared_phase_is_accepted(store: WorkStore, phase: str) -> None:
    item = seed(store)
    store.update_item(
        item["id"], {"status": "in_progress"}, actor="moa",
        expected_revision=1, context={"phase": phase},
    )
    assert entries(store, item["id"])[-1]["phase"] == phase


def test_an_unknown_phase_is_refused(store: WorkStore) -> None:
    item = seed(store)
    with pytest.raises(WorkValidationError, match="phase must be one of"):
        store.update_item(
            item["id"], {"status": "in_progress"}, actor="moa",
            expected_revision=1, context={"phase": "shipping"},
        )


def test_an_unknown_context_field_is_refused(store: WorkStore) -> None:
    item = seed(store)
    with pytest.raises(WorkValidationError, match="unknown context fields"):
        store.update_item(
            item["id"], {"status": "in_progress"}, actor="moa",
            expected_revision=1, context={"whatever": "x"},
        )


@pytest.mark.parametrize(
    "receipt", ["/etc/passwd", "~/secret", "../../escape", "cowork/../../etc/x"],
)
def test_a_receipt_that_leaves_the_cowork_tree_is_refused(
    store: WorkStore, receipt: str
) -> None:
    item = seed(store)
    with pytest.raises(WorkValidationError, match="receipt"):
        store.update_item(
            item["id"], {"status": "in_progress"}, actor="moa",
            expected_revision=1, context={"receipt": receipt},
        )


def test_a_summary_is_bounded(store: WorkStore) -> None:
    item = seed(store)
    with pytest.raises(WorkValidationError, match="summary"):
        store.update_item(
            item["id"], {"status": "in_progress"}, actor="moa",
            expected_revision=1, context={"summary": "x" * 501},
        )


def test_the_history_stream_keeps_what_each_field_became(
    store: WorkStore, tmp_path: Path
) -> None:
    """Reversed on 2026-09-04 (HK P0 condition 5); it read the other way before.

    Keeping only field names was the right balance while `progress_summary`
    carried the running account. That field is being cut to three lines, so
    names alone would leave no account anywhere. Fields the update did not
    touch still stay out: the entry records the change, not the item.
    """
    item = seed(store, detail="상세 내용", progress_summary="진행")
    store.update_item(
        item["id"],
        {"blocker": "막힘", "next_action": "다음"},
        actor="moa",
        expected_revision=1,
    )
    entry = store.read_history(item_id=item["id"])[0]
    assert entry["values"]["blocker"]["after"] == {"present": True, "value": "막힘"}
    assert entry["values"]["next_action"]["after"] == {"present": True, "value": "다음"}
    assert set(entry["values"]) == {"blocker", "next_action"}


# ---------------------------------------------------- legacy preservation


def test_a_record_written_before_the_timeline_fields_is_marked_legacy(
    store: WorkStore, tmp_path: Path
) -> None:
    """Rule 5: an older entry is reported as unknown, never back-filled."""
    item = seed(store)
    history = tmp_path / "work" / "history.jsonl"
    history.write_text(
        json.dumps(
            {
                "at": "2026-09-01T04:57:06+00:00",
                "action": "work.created",
                "actor": "codex",
                "item_id": item["id"],
                "revision": 1,
                "fields": ["title"],
                "status": "backlog",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    entry = entries(store, item["id"])[0]
    assert entry["record_schema"] == "legacy"
    assert set(entry["unknown_fields"]) == set(WorkStore.TIMELINE_FIELDS)
    assert entry["requested_by"] is None and entry["assigned_to"] is None
    assert entry["actor"]["name"] == "codex"
    assert entry["actor"]["party"] is None
    assert entry["actor"]["resolution"] == "unresolved"


def test_a_pre_rename_party_name_is_not_relabelled_as_todays_party(
    store: WorkStore, tmp_path: Path
) -> None:
    item = seed(store)
    history = tmp_path / "work" / "history.jsonl"
    history.write_text(
        json.dumps({
            "at": "2026-08-20T00:00:00+00:00", "action": "work.updated", "actor": "moa",
            "item_id": item["id"], "revision": 1, "fields": ["status"], "status": "ready",
        }) + "\n",
        encoding="utf-8",
    )
    entry = entries(store, item["id"])[0]
    assert entry["actor"]["name"] == "moa"
    assert entry["actor"]["party"] is None
    assert entry["actor"]["resolution"] == "inferred"


def test_a_malformed_history_line_is_skipped_not_fatal(
    store: WorkStore, tmp_path: Path
) -> None:
    item = seed(store)
    history = tmp_path / "work" / "history.jsonl"
    with history.open("a", encoding="utf-8") as handle:
        handle.write("{not json\n\n")
    assert entries(store, item["id"])


# ------------------------------------------------------------- receipts


def test_cowork_events_and_handoffs_are_merged_in_by_work_id(
    store: WorkStore, tmp_path: Path
) -> None:
    item = seed(store)
    cowork = tmp_path / "cowork"
    (cowork / "handoffs").mkdir(parents=True)
    (cowork / "events.jsonl").write_text(
        json.dumps({"at": "2026-09-02T12:00:00Z", "event": "build", "work_id": item["id"],
                    "revision": 1, "handoff_id": "ho_1", "summary": "built"}) + "\n"
        + json.dumps({"at": "2026-09-02T12:00:00Z", "event": "build", "work_id": "wi_other",
                      "revision": 1, "summary": "other item"}) + "\n",
        encoding="utf-8",
    )
    name = f"20260902T120500Z-{item['id']}.json"
    (cowork / "handoffs" / name).write_text(
        json.dumps({"handoff_id": "ho_1", "created_at": "2026-09-02T12:05:00Z",
                    "actor": "moa", "work_id": item["id"], "outcome": "completed",
                    "next_action": "review", "item": {"revision_after": 2}}),
        encoding="utf-8",
    )
    found = entries(store, item["id"])
    sources = [e["source"] for e in found]
    assert "cowork_event" in sources and "cowork_handoff" in sources
    assert not any(e.get("summary") == "other item" for e in found)
    handoff = next(e for e in found if e["source"] == "cowork_handoff")
    assert handoff["receipt"] == f"cowork/handoffs/{name}"
    assert not handoff["receipt"].startswith("/")


def test_a_missing_or_unreadable_cowork_tree_yields_no_entries(store: WorkStore) -> None:
    item = seed(store)
    found = entries(store, item["id"])
    assert found and all(e["source"] == "work_history" for e in found)


def test_the_timeline_is_ordered_oldest_first_and_bounded(store: WorkStore) -> None:
    item = seed(store)
    for revision in range(1, 6):
        store.update_item(
            item["id"], {"next_action": f"step {revision}"}, actor="moa",
            expected_revision=revision, context={"phase": "progress"},
        )
    payload = store.read_timeline(item["id"], limit=3)
    assert len(payload["entries"]) == 3
    assert payload["truncated"] is True
    assert payload["count"] == 6
    stamps = [e["at"] for e in payload["entries"]]
    assert stamps == sorted(stamps)


def test_the_timeline_header_reports_the_items_current_identity(store: WorkStore) -> None:
    item = seed(store)
    payload = store.read_timeline(item["id"])
    assert payload["item_id"] == item["id"]
    assert payload["requested_by"] == "hk"
    assert payload["assigned_to"] == "moa"
    assert payload["revision"] == item["revision"]


# ------------------------------------------- document compatibility (rule 6)


def test_the_timeline_needed_no_new_document_version(store: WorkStore) -> None:
    """History is an append-only stream, so the fields were added additively.

    A new `DOCUMENT_VERSION` would have forced a migration and a compatibility
    window for every reader. Because nothing in `items.json` changed shape,
    none of that was necessary -- and this test fails if that ever stops being
    true, which is the signal to add an explicit migration and its tests.
    """
    from rlwrld_worklog.work_store import DOCUMENT_VERSION, _MIGRATIONS

    assert DOCUMENT_VERSION == 2
    assert sorted(_MIGRATIONS) == [1]
    seed(store)
    document = store.read_document()
    assert document["version"] == 2


def test_an_existing_v2_document_is_read_without_loss(store: WorkStore, tmp_path: Path) -> None:
    item = seed(store, detail="상세", source_ref="incident:x")
    store.update_item(
        item["id"], {"status": "in_progress"}, actor="moa",
        expected_revision=1, context={"phase": "started"},
    )
    before = json.loads((tmp_path / "work" / "items.json").read_text(encoding="utf-8"))
    reread = WorkStore(tmp_path).get_item(item["id"])
    stored = next(row for row in before["items"] if row["id"] == item["id"])
    assert reread == stored
    assert before["version"] == 2
    assert reread["detail"] == "상세" and reread["source_ref"] == "incident:x"


def test_a_v1_document_still_migrates_and_then_carries_a_timeline(
    tmp_path: Path,
) -> None:
    """The v1 -> v2 path keeps working, and migrated items get timelines too."""
    directory = tmp_path / "work"
    directory.mkdir(parents=True)
    (directory / "items.json").write_text(
        json.dumps({
            "version": 1, "revision": 3, "updated_at": "2026-09-01T00:00:00+00:00",
            "items": [{
                "id": "wi_00000000000000aa", "title": "legacy", "detail": None,
                "status": "backlog", "priority": "normal", "requested_by": "hk",
                "assigned_to": "codex", "parent_id": None, "progress_summary": "",
                "next_action": "", "blocker": None, "source_ref": None,
                "created_at": "2026-09-01T00:00:00+00:00",
                "updated_at": "2026-09-01T00:00:00+00:00",
                "started_at": None, "completed_at": None, "due_at": None,
                "archived_at": None, "revision": 1,
            }],
        }),
        encoding="utf-8",
    )
    store = WorkStore(tmp_path)
    document = store.read_document()
    assert document["version"] == 2
    payload = store.read_timeline("wi_00000000000000aa")
    assert payload["item_id"] == "wi_00000000000000aa"
    # A migrated item has no history of its own; the timeline says so plainly
    # rather than inventing entries for it.
    assert payload["entries"] == []
    assert payload["count"] == 0
