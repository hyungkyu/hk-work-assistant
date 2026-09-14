"""The programme phase on a work item: P0, P1, P2 …

HK, 2026-09-14: 업무 목록을 p0, P1, P2 등을 붙여줘 구분되게.

Phase is not priority. Priority says how soon; phase says which piece of the
programme an item is part of, and the two disagree constantly -- a P0 item
goes `normal` once P0 is nearly closed, and a P2 item goes `urgent` because
it blocks a demo. The tests below hold that separation, the open-endedness of
the vocabulary, and the one thing that matters most: that an item nobody has
placed in a phase says so instead of being filed under the current one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rlwrld_worklog.work_store import (
    DOCUMENT_VERSION,
    WorkStore,
    WorkValidationError,
    phase_rank,
)


def make_store(tmp_path: Path) -> WorkStore:
    return WorkStore(tmp_path / "config")


def create(store: WorkStore, **fields: object) -> dict:
    payload = {"title": "t", "requested_by": "hk", "assigned_to": "codex"}
    payload.update(fields)
    return store.create_item(payload, actor="hk")


def test_an_item_starts_with_no_phase(tmp_path: Path) -> None:
    """The default is "nobody has said", not "whatever phase we are in"."""
    assert create(make_store(tmp_path))["phase"] is None


def test_a_phase_is_stored_as_written_and_uppercased(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    assert create(store, phase="p1")["phase"] == "P1"
    assert create(store, phase="P12")["phase"] == "P12"


def test_a_phase_that_is_not_a_phase_is_refused(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    for value in ("P", "phase 1", "1", "PA", "P100", "urgent"):
        with pytest.raises(WorkValidationError, match="phase"):
            create(store, phase=value)


def test_a_phase_can_be_cleared(tmp_path: Path) -> None:
    """Taking an item out of a phase is a real decision, not a validation error."""
    store = make_store(tmp_path)
    item = create(store, phase="P2")
    store.update_item(item["id"], {"phase": ""}, actor="hk", expected_revision=1)
    assert store.get_item(item["id"])["phase"] is None


def test_phase_and_priority_are_separate_axes(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    item = create(store, phase="P2", priority="urgent")
    assert (item["phase"], item["priority"]) == ("P2", "urgent")


def test_the_list_reads_p0_first_and_the_unplaced_last(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    for phase in ("P2", None, "P0", "P10", "P1"):
        create(store, phase=phase, title=f"item {phase}")
    listed = store.list_items()["items"]
    assert [item["phase"] for item in listed] == ["P0", "P1", "P2", "P10", None]


def test_phase_rank_orders_numerically_not_alphabetically() -> None:
    """P10 after P2. Sorting these as strings is the obvious wrong answer."""
    assert sorted(["P10", "P2", "P0", None], key=phase_rank) == ["P0", "P2", "P10", None]


def test_priority_still_orders_within_a_phase(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    create(store, phase="P1", priority="low", title="낮음")
    create(store, phase="P1", priority="urgent", title="급함")
    create(store, phase="P0", priority="low", title="P0 낮음")
    listed = store.list_items()["items"]
    assert [item["title"] for item in listed] == ["P0 낮음", "급함", "낮음"]


def test_the_store_version_moved_for_this(tmp_path: Path) -> None:
    """A field added to `items.json` is a migration, stated rather than assumed."""
    store = make_store(tmp_path)
    create(store, phase="P1")
    assert store.read_document()["version"] == DOCUMENT_VERSION == 3
