"""Notion daily incremental capture.

A scripted fake API stands in for Notion: no network call is made anywhere in
this file. Every identifier and every piece of text is invented.
"""

from __future__ import annotations

import gzip
import io
import json
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from rlwrld_worklog.archive import RawArchive
from rlwrld_worklog.link_queue import NotionLinkQueue
from rlwrld_worklog.notion_client import NotionApiError, NotionClient
from rlwrld_worklog.notion_collector import (
    BOUNDED_COMMENTS_NOTE,
    DRY_RUN_LINK_QUEUE_NOTE,
    OBJECT_ISOLATION_NOTE,
    SEARCH_INCOMPLETE_NOTE,
    UNRESOLVED_FAILURE_NOTE,
    WATERMARK_HELD_NOTE,
    NotionCollector,
    _advance_watermark,
)

PAGE_ID = "01234567-89ab-cdef-0123-456789abcdef"
SECOND_PAGE_ID = "11111111-2222-3333-4444-555555555555"
DATA_SOURCE_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
SINCE = datetime(2026, 8, 25, tzinfo=timezone.utc)


def page_body(page_id: str, *, edited: str = "2026-08-26T01:00:00Z", **overrides: Any) -> dict[str, Any]:
    body = {
        "object": "page",
        "id": page_id,
        "url": f"https://www.notion.so/{page_id.replace('-', '')}",
        "created_time": "2026-08-20T00:00:00Z",
        "last_edited_time": edited,
        "last_edited_by": {"id": "user-1"},
        "parent": {"type": "workspace", "workspace": True},
        "properties": {},
    }
    body.update(overrides)
    return body


def exhausted(
    *,
    retry_class: str = "timeout",
    attempts: int = 6,
    status: int | None = None,
    code: str | None = None,
) -> NotionApiError:
    """What NotionClient raises once its transient retries are used up.

    The shape matters more than the text: `transient=True` is what tells the
    collector the API never gave a final answer for this object.
    """
    return NotionApiError(
        f"Notion request did not complete after {attempts} attempts",
        status=status,
        code=code,
        transient=True,
        retry_class=retry_class,
        attempts=attempts,
    )


def permanent(status: int, code: str) -> NotionApiError:
    return NotionApiError(f"HTTP {status}", status=status, code=code, transient=False, attempts=1)


def _maybe_raise(entry: Any) -> None:
    """An exception planted in a response list fails that request."""
    if isinstance(entry, BaseException):
        raise entry


class FakeNotionClient:
    def __init__(
        self,
        *,
        search_results: list[dict[str, Any]] | None = None,
        search_error: BaseException | None = None,
        pages: dict[str, dict[str, Any]] | None = None,
        page_errors: dict[str, NotionApiError] | None = None,
        data_sources: dict[str, dict[str, Any]] | None = None,
        blocks: dict[str, list[Any]] | None = None,
        comments: dict[str, list[dict[str, Any]]] | None = None,
        comment_errors: dict[str, Any] | None = None,
        property_pages: dict[str, list[Any]] | None = None,
        users: list[dict[str, Any]] | None = None,
        rate_limit_hits: int = 0,
        transient_retries: int = 0,
        retry_counts: dict[str, int] | None = None,
        exhausted_requests: int = 0,
    ) -> None:
        self.search_results = (
            search_results
            if search_results is not None
            else [{"object": "page", "id": PAGE_ID, "last_edited_time": "2026-08-26T01:00:00Z"}]
        )
        self.pages = pages if pages is not None else {PAGE_ID: page_body(PAGE_ID)}
        self.page_errors = page_errors or {}
        self.data_sources = data_sources or {}
        self.blocks = blocks if blocks is not None else {
            PAGE_ID: [
                [
                    {
                        "id": "block-1",
                        "type": "paragraph",
                        "has_children": False,
                        "paragraph": {"rich_text": [{"plain_text": "weekly meeting decision"}]},
                    },
                    {
                        "id": "meeting-1",
                        "type": "meeting_notes",
                        "has_children": False,
                        "meeting_notes": {"children": {"summary_block_id": "summary-1"}},
                    },
                ]
            ],
            "summary-1": [
                [
                    {
                        "id": "summary-text",
                        "type": "paragraph",
                        "has_children": False,
                        "paragraph": {"rich_text": [{"plain_text": "meeting summary"}]},
                    }
                ]
            ],
        }
        self.comments = comments or {}
        self.comment_errors = comment_errors or {}
        self.property_pages = property_pages or {}
        self.search_error = search_error
        self.users = users if users is not None else [{"object": "user", "id": "user-1", "name": "synthetic"}]
        self.calls: list[tuple[str, str]] = []
        # The accounting a real client exposes, so the manifest wiring is
        # exercised rather than assumed.
        self.rate_limit_hits = rate_limit_hits
        self.transient_retries = transient_retries
        self.retry_counts = dict(retry_counts or {})
        self.exhausted_requests = exhausted_requests
        self.call_counts: dict[str, int] = {}

    def iter_search(self, *, object_filter: str | None = None):
        self.calls.append(("search", str(object_filter)))
        yield {"results": self.search_results, "has_more": False}
        if self.search_error is not None:
            raise self.search_error

    def retrieve_page(self, page_id):
        self.calls.append(("retrieve_page", page_id))
        if page_id in self.page_errors:
            _maybe_raise(self.page_errors[page_id])
        if page_id not in self.pages:
            raise NotionApiError("not found", status=404, code="object_not_found")
        return self.pages[page_id]

    def retrieve_data_source(self, data_source_id):
        self.calls.append(("retrieve_data_source", data_source_id))
        if data_source_id not in self.data_sources:
            raise NotionApiError("not found", status=404, code="object_not_found")
        return self.data_sources[data_source_id]

    def retrieve_database(self, database_id):
        self.calls.append(("retrieve_database", database_id))
        raise NotionApiError("not found", status=404, code="object_not_found")

    def iter_block_children(self, block_id):
        self.calls.append(("blocks", block_id))
        for page in self.blocks.get(block_id, [[]]):
            # An exception in the page list fails that page of the walk, which
            # is how a deep pagination dies in production.
            _maybe_raise(page)
            yield {"results": page, "has_more": False}

    def iter_comments(self, block_id):
        self.calls.append(("comments", block_id))
        if block_id in self.comment_errors:
            _maybe_raise(self.comment_errors[block_id])
        yield {"results": self.comments.get(block_id, []), "has_more": False}

    def iter_property_items(self, page_id, property_id):
        self.calls.append(("properties", page_id))
        for page in self.property_pages.get(page_id, [[]]):
            _maybe_raise(page)
            yield {"results": page, "has_more": False}

    def iter_users(self):
        self.calls.append(("users", ""))
        yield {"results": self.users, "has_more": False}


def collect(
    tmp_path: Path,
    client: FakeNotionClient,
    *,
    run_id: str = "notion-run",
    dry_run: bool = False,
    **kwargs: Any,
):
    queue = NotionLinkQueue(tmp_path, "test")
    archive = RawArchive(tmp_path, "notion", run_id, "test", dry_run=dry_run)
    result = NotionCollector(client, archive, queue).collect(
        since=kwargs.pop("since", SINCE), **kwargs
    )
    return archive, queue, result


def archived_kinds(archive: RawArchive) -> list[str]:
    return [item["kind"] for item in archive.files]


def test_collects_recent_and_forced_linked_pages(tmp_path: Path) -> None:
    queue = NotionLinkQueue(tmp_path, "test")
    url = f"https://www.notion.so/{PAGE_ID.replace('-', '')}"
    queue.add_urls([url], source="slack", run_id="slack-run")
    archive = RawArchive(tmp_path, "notion", "notion-run", "test")
    result = NotionCollector(FakeNotionClient(), archive, queue).collect(since=SINCE)

    assert result.pages_collected == 1
    assert "meeting summary" in result.events[0].payload["content_text"]
    assert queue.load()[url]["status"] == "fetched"
    manifest = json.loads(result.manifest_path.read_text())
    assert manifest["status"] == "success"


def test_descendant_blocks_and_comments_are_fetched_recursively(tmp_path: Path) -> None:
    client = FakeNotionClient(
        comments={PAGE_ID: [{"id": "comment-1", "discussion_id": "d1", "rich_text": []}]}
    )
    archive, _, result = collect(tmp_path, client)

    assert result.blocks_collected == 3, "the meeting-notes child block is followed"
    assert result.comments_collected == 1
    kinds = archived_kinds(archive)
    assert f"blocks-{PAGE_ID}" in kinds
    assert "blocks-summary-1" in kinds
    assert f"comments-{PAGE_ID}" in kinds
    assert "comments-block-1" in kinds, "comments are requested per block, not only per page"


def test_users_and_data_sources_are_captured(tmp_path: Path) -> None:
    client = FakeNotionClient(
        search_results=[
            {"object": "page", "id": PAGE_ID, "last_edited_time": "2026-08-26T01:00:00Z"},
            {"object": "data_source", "id": DATA_SOURCE_ID, "last_edited_time": "2026-08-26T02:00:00Z"},
        ],
        data_sources={
            DATA_SOURCE_ID: {
                "object": "data_source",
                "id": DATA_SOURCE_ID,
                "last_edited_time": "2026-08-26T02:00:00Z",
                "properties": {"Name": {"id": "title", "type": "title"}},
            }
        },
    )
    archive, _, result = collect(tmp_path, client)

    assert result.databases_collected == 1
    assert result.users_seen == 1
    kinds = archived_kinds(archive)
    assert f"data_source-{DATA_SOURCE_ID}" in kinds
    assert "users" in kinds


def test_an_archived_page_is_preserved_with_its_flag(tmp_path: Path) -> None:
    client = FakeNotionClient(pages={PAGE_ID: page_body(PAGE_ID, archived=True, in_trash=True)})
    archive, _, result = collect(tmp_path, client)

    assert result.archived_observed == 1
    stored = json.loads(
        gzip.decompress(
            (tmp_path / next(item["path"] for item in archive.files if item["kind"] == f"page-{PAGE_ID}")).read_bytes()
        )
    )
    assert stored["archived"] is True


def test_known_objects_are_rechecked_so_deletion_becomes_observable(tmp_path: Path) -> None:
    seed = RawArchive(tmp_path, "notion", "notion-run-0", "test")
    seed.write_checkpoint(
        {
            "schema_version": 2,
            "source": "notion",
            "run_id": "notion-run-0",
            "last_edited_watermark": "2026-08-26T01:00:00+00:00",
            "known_objects": {SECOND_PAGE_ID: {"type": "page", "last_checked": "2026-08-20T00:00:00+00:00"}},
        }
    )
    # The previously known page is gone from search and now 404s everywhere.
    client = FakeNotionClient()
    _, _, result = collect(tmp_path, client, run_id="notion-run-1", recheck_limit=10)

    assert result.counters["objects_rechecked"] == 1
    assert result.pages_skipped == 1
    manifest = json.loads(result.manifest_path.read_text())
    gone = next(item for item in manifest["skipped_pages"] if item["object_id"] == SECOND_PAGE_ID)
    assert gone["discovered_by"] == "recheck"
    assert gone["status"] == 404
    assert manifest["status"] == "success_with_skips"


def test_an_unresolvable_link_queue_url_is_marked_and_reported(tmp_path: Path) -> None:
    queue = NotionLinkQueue(tmp_path, "test")
    queue.add_urls(["https://www.notion.so/team/no-identifier-here"], source="slack", run_id="slack-run")
    archive = RawArchive(tmp_path, "notion", "notion-run", "test")
    result = NotionCollector(FakeNotionClient(), archive, queue).collect(since=SINCE)

    assert result.link_queue_unresolved == 1
    stored = queue.load()["https://www.notion.so/team/no-identifier-here"]
    assert stored["status"] == "unresolved"
    manifest = json.loads(result.manifest_path.read_text())
    assert manifest["unresolved_link_urls"] == ["unresolved:sha256:" + manifest["unresolved_link_urls"][0].split(":")[-1]]
    assert "no-identifier-here" not in json.dumps(manifest), "a manifest never stores page titles"


def test_an_inaccessible_linked_page_is_marked_failed_not_fetched(tmp_path: Path) -> None:
    queue = NotionLinkQueue(tmp_path, "test")
    url = f"https://www.notion.so/{SECOND_PAGE_ID.replace('-', '')}"
    queue.add_urls([url], source="slack", run_id="slack-run")
    archive = RawArchive(tmp_path, "notion", "notion-run", "test")
    client = FakeNotionClient(
        page_errors={SECOND_PAGE_ID: NotionApiError("forbidden", status=403, code="restricted_resource")}
    )
    result = NotionCollector(client, archive, queue).collect(since=SINCE)

    assert queue.load()[url]["status"] == "failed"
    assert result.pages_skipped == 1


def test_checkpoint_advances_only_after_a_clean_window(tmp_path: Path) -> None:
    client = FakeNotionClient()
    _, _, result = collect(tmp_path, client)

    checkpoint = json.loads((tmp_path / "manifests/notion/test/checkpoint.json").read_text())
    assert result.checkpoint_advanced is True
    assert checkpoint["last_edited_watermark"] == "2026-08-26T01:00:00+00:00"
    assert PAGE_ID in checkpoint["known_objects"]


def test_the_watermark_never_steps_over_a_failed_object() -> None:
    previous = "2026-08-20T00:00:00+00:00"
    collected = [datetime(2026, 8, 26, tzinfo=timezone.utc), datetime(2026, 8, 28, tzinfo=timezone.utc)]
    failed = [datetime(2026, 8, 27, tzinfo=timezone.utc)]

    assert _advance_watermark(previous=previous, collected=collected, failed=failed) == (
        datetime(2026, 8, 26, tzinfo=timezone.utc).isoformat()
    )
    assert _advance_watermark(previous=previous, collected=collected, failed=[]) == (
        datetime(2026, 8, 28, tzinfo=timezone.utc).isoformat()
    )
    assert _advance_watermark(previous=previous, collected=[], failed=[]) == previous
    # A failure older than the stored position must not rewind it either.
    assert _advance_watermark(
        previous="2026-08-29T00:00:00+00:00", collected=collected, failed=failed
    ) == "2026-08-29T00:00:00+00:00"


def test_the_checkpoint_widens_the_window_after_a_missed_day(tmp_path: Path) -> None:
    seed = RawArchive(tmp_path, "notion", "notion-run-0", "test")
    seed.write_checkpoint(
        {
            "schema_version": 2,
            "source": "notion",
            "run_id": "notion-run-0",
            "last_edited_watermark": "2026-08-01T00:00:00+00:00",
            "known_objects": {},
        }
    )
    client = FakeNotionClient()
    _, _, result = collect(tmp_path, client, run_id="notion-run-1")

    manifest = json.loads(result.manifest_path.read_text())
    assert manifest["since_effective"].startswith("2026-08-01"), (
        "a stalled checkpoint must widen the window, not leave a hole"
    )


def test_dry_run_bounds_the_work_and_leaves_the_checkpoint_alone(tmp_path: Path) -> None:
    client = FakeNotionClient(
        search_results=[
            {"object": "page", "id": PAGE_ID, "last_edited_time": "2026-08-26T01:00:00Z"},
            {"object": "page", "id": SECOND_PAGE_ID, "last_edited_time": "2026-08-26T03:00:00Z"},
        ],
        pages={PAGE_ID: page_body(PAGE_ID), SECOND_PAGE_ID: page_body(SECOND_PAGE_ID)},
    )
    _, _, result = collect(tmp_path, client, dry_run=True, max_objects=1, recheck_limit=0)

    assert result.counters["objects_attempted"] == 1
    assert result.checkpoint_advanced is False
    assert not (tmp_path / "manifests/notion/test/checkpoint.json").exists()
    manifest = json.loads(result.manifest_path.read_text())
    assert manifest["dry_run"] is True
    assert manifest["truncation"][0]["reason"] == "max_objects"


def test_coverage_notes_declare_the_search_limits(tmp_path: Path) -> None:
    _, _, result = collect(tmp_path, FakeNotionClient())
    notes = " ".join(json.loads(result.manifest_path.read_text())["coverage_notes"])
    assert "notion.search_is_not_a_change_feed" in notes
    assert "notion.attachments_are_metadata_only" in notes


def test_an_explicitly_configured_comment_budget_bounds_and_reports_itself(tmp_path: Path) -> None:
    client = FakeNotionClient()
    _, _, result = collect(tmp_path, client, comment_request_budget=1)

    assert result.counters["comment_budget_exhausted"] is True
    assert result.counters["comment_request_budget"] == 1
    assert result.counters["comment_requests_made"] == 1
    assert result.counters["comment_requests_remaining"] == 0
    manifest = json.loads(result.manifest_path.read_text())
    assert manifest["truncated"] is True
    assert manifest["truncation"][0]["reason"] == "comment_request_budget"
    assert BOUNDED_COMMENTS_NOTE in manifest["coverage_notes"]
    assert result.checkpoint_advanced is False, (
        "an incomplete comment sweep must not move the watermark"
    )


def test_a_full_density_run_sweeps_comments_exhaustively(tmp_path: Path) -> None:
    """A finite default cap would make a large workspace truncate itself
    forever: withhold the checkpoint, then redo the identical window."""
    client = FakeNotionClient()
    _, _, result = collect(tmp_path, client)

    assert result.counters["comment_request_budget"] is None
    assert result.counters["comment_requests_remaining"] is None
    assert result.counters["comment_budget_exhausted"] is False
    # The page, its three blocks, and the meeting-notes container the
    # traversal followed -- any of which can carry an inline comment.
    assert result.counters["comment_requests_made"] == 5
    assert [block for kind, block in client.calls if kind == "comments"] == [
        PAGE_ID,
        "block-1",
        "meeting-1",
        "summary-1",
        "summary-text",
    ]
    manifest = json.loads(result.manifest_path.read_text())
    assert manifest["truncated"] is False
    assert BOUNDED_COMMENTS_NOTE not in manifest["coverage_notes"]
    assert result.checkpoint_advanced is True


def test_a_large_object_count_still_never_caps_itself(tmp_path: Path) -> None:
    """The failure mode Codex found: enough blocks to exceed any old default."""
    wide = [
        [
            {"id": f"block-{index}", "type": "paragraph", "has_children": False}
            for index in range(2_500)
        ]
    ]
    client = FakeNotionClient(blocks={PAGE_ID: wide})
    _, _, result = collect(tmp_path, client)

    assert result.blocks_collected == 2_500
    assert result.counters["comment_requests_made"] == 2_501  # every block, plus the page
    assert result.counters["comment_budget_exhausted"] is False
    assert json.loads(result.manifest_path.read_text())["truncated"] is False
    assert result.checkpoint_advanced is True


# ------------------------------------------------- the link queue in a dry run


def seed_link_queue(tmp_path: Path, url: str) -> bytes:
    queue = NotionLinkQueue(tmp_path, "test")
    queue.add_urls([url], source="slack", run_id="slack-run")
    return queue.path.read_bytes()


def test_a_dry_run_never_writes_to_the_link_queue(tmp_path: Path) -> None:
    url = f"https://www.notion.so/{PAGE_ID.replace('-', '')}"
    before = seed_link_queue(tmp_path, url)

    _, queue, result = collect(tmp_path, FakeNotionClient(), dry_run=True)

    assert queue.path.read_bytes() == before, (
        "a dry run must not mark a real pending URL fetched"
    )
    assert queue.load()[url]["status"] == "pending"
    assert result.link_queue_fetched == 1, "the run still reports what it would have marked"
    assert result.counters["link_queue_persisted"] is False
    assert DRY_RUN_LINK_QUEUE_NOTE in json.loads(result.manifest_path.read_text())["coverage_notes"]


def test_a_dry_run_creates_no_link_queue_where_there_was_none(tmp_path: Path) -> None:
    _, queue, _ = collect(tmp_path, FakeNotionClient(), dry_run=True)
    assert not queue.path.exists()


def test_a_dry_run_does_not_mark_an_unresolvable_url(tmp_path: Path) -> None:
    url = "https://www.notion.so/team/no-identifier-here"
    before = seed_link_queue(tmp_path, url)

    _, queue, result = collect(tmp_path, FakeNotionClient(), dry_run=True)

    assert queue.path.read_bytes() == before
    assert queue.load()[url]["status"] == "pending"
    assert result.link_queue_unresolved == 1, "it is still reported, just not written"


def test_a_dry_run_does_not_mark_an_inaccessible_url_failed(tmp_path: Path) -> None:
    url = f"https://www.notion.so/{SECOND_PAGE_ID.replace('-', '')}"
    before = seed_link_queue(tmp_path, url)
    client = FakeNotionClient(
        page_errors={SECOND_PAGE_ID: NotionApiError("forbidden", status=403, code="restricted_resource")}
    )

    _, queue, result = collect(tmp_path, client, dry_run=True)

    assert queue.path.read_bytes() == before
    assert result.pages_skipped == 1


def test_a_real_run_does_persist_its_link_queue_marks(tmp_path: Path) -> None:
    url = f"https://www.notion.so/{PAGE_ID.replace('-', '')}"
    before = seed_link_queue(tmp_path, url)

    _, queue, result = collect(tmp_path, FakeNotionClient())

    assert queue.path.read_bytes() != before
    assert queue.load()[url]["status"] == "fetched"
    assert result.counters["link_queue_persisted"] is True
    assert DRY_RUN_LINK_QUEUE_NOTE not in json.loads(result.manifest_path.read_text())["coverage_notes"]


def test_a_read_only_queue_view_shares_the_path_it_refuses_to_write(tmp_path: Path) -> None:
    live = NotionLinkQueue(tmp_path, "test")
    view = live.read_only_view()

    assert view.path == live.path
    assert view.read_only is True
    assert view.read_only_view() is view
    assert live.read_only is False


# ------------------------------------- per-object isolation of a transient failure
#
# The production failure this section exists for: a bare read timeout escaped
# the client during a 62-minute run, after 9,005 raw pages and 73,999 blocks
# had been archived, and killed the entire Notion source. One object must never
# be able to do that again.


THIRD_PAGE_ID = "22222222-3333-4444-5555-666666666666"


def three_page_client(**overrides: Any) -> FakeNotionClient:
    """PAGE, SECOND, THIRD -- collected in that (id-sorted) order."""
    defaults: dict[str, Any] = {
        "search_results": [
            {"object": "page", "id": PAGE_ID, "last_edited_time": "2026-08-26T01:00:00Z"},
            {"object": "page", "id": SECOND_PAGE_ID, "last_edited_time": "2026-08-26T02:00:00Z"},
            {"object": "page", "id": THIRD_PAGE_ID, "last_edited_time": "2026-08-26T03:00:00Z"},
        ],
        "pages": {
            PAGE_ID: page_body(PAGE_ID, edited="2026-08-26T01:00:00Z"),
            SECOND_PAGE_ID: page_body(SECOND_PAGE_ID, edited="2026-08-26T02:00:00Z"),
            THIRD_PAGE_ID: page_body(THIRD_PAGE_ID, edited="2026-08-26T03:00:00Z"),
        },
        "blocks": {},
    }
    defaults.update(overrides)
    return FakeNotionClient(**defaults)


def failure_for(result: Any, object_id: str) -> dict[str, Any]:
    manifest = json.loads(result.manifest_path.read_text())
    return next(item for item in manifest["skipped_pages"] if item["object_id"] == object_id)


def assert_only_the_middle_object_failed(result: Any, *, phase: str) -> dict[str, Any]:
    """Every shared consequence of one object failing, in one place."""
    assert result.pages_collected == 2, "the two healthy objects still produced events"
    assert {event.external_id for event in result.events} == {PAGE_ID, THIRD_PAGE_ID}
    assert SECOND_PAGE_ID not in {event.external_id for event in result.events}, (
        "a failed object emits no normalized event"
    )
    assert result.objects_completed == 2
    assert result.pages_skipped == 1
    assert result.objects_failed_unresolved == 1
    assert result.coverage_complete is False
    assert result.status == "degraded", "the run must not claim it looked everywhere"

    detail = failure_for(result, SECOND_PAGE_ID)
    assert detail["phase"] == phase
    assert detail["resolution"] == "unresolved"
    assert detail["retry_class"] == "timeout"
    assert detail["attempts"] == 6

    manifest = json.loads(result.manifest_path.read_text())
    assert manifest["status"] == "degraded"
    assert manifest["coverage_complete"] is False
    assert manifest["objects_completed"] == 2
    assert manifest["objects_failed_unresolved"] == 1
    assert manifest["counters"]["objects_failed_by_phase"] == {phase: 1}
    assert UNRESOLVED_FAILURE_NOTE in manifest["coverage_notes"]
    assert OBJECT_ISOLATION_NOTE in manifest["coverage_notes"]
    return manifest


def test_a_transient_exhaustion_at_retrieve_skips_only_that_object(tmp_path: Path) -> None:
    client = three_page_client(page_errors={SECOND_PAGE_ID: exhausted()})
    _, _, result = collect(tmp_path, client)

    assert_only_the_middle_object_failed(result, phase="retrieve")
    assert ("retrieve_page", THIRD_PAGE_ID) in client.calls, "the run continued past the failure"


def test_a_transient_exhaustion_in_deep_block_pagination_skips_only_that_object(
    tmp_path: Path,
) -> None:
    """Page one of the subtree arrives, page two never does."""
    client = three_page_client(
        blocks={
            SECOND_PAGE_ID: [
                [{"id": "deep-1", "type": "paragraph", "has_children": False}],
                exhausted(),
            ]
        }
    )
    archive, _, result = collect(tmp_path, client)

    assert_only_the_middle_object_failed(result, phase="blocks")
    assert result.blocks_collected == 0, (
        "blocks from an incomplete subtree are not counted as collected coverage"
    )
    assert f"blocks-{SECOND_PAGE_ID}" in archived_kinds(archive), (
        "the raw page that did arrive is still archived"
    )


def test_a_transient_exhaustion_in_comments_skips_only_that_object(tmp_path: Path) -> None:
    client = three_page_client(comment_errors={SECOND_PAGE_ID: exhausted()})
    _, _, result = collect(tmp_path, client)

    assert_only_the_middle_object_failed(result, phase="comments")


def test_a_transient_exhaustion_in_properties_skips_only_that_object(tmp_path: Path) -> None:
    titled = {
        object_id: page_body(
            object_id,
            edited=edited,
            properties={"Name": {"id": "title", "type": "title"}},
        )
        for object_id, edited in (
            (PAGE_ID, "2026-08-26T01:00:00Z"),
            (SECOND_PAGE_ID, "2026-08-26T02:00:00Z"),
            (THIRD_PAGE_ID, "2026-08-26T03:00:00Z"),
        )
    }
    client = three_page_client(
        pages=titled, property_pages={SECOND_PAGE_ID: [[{"id": "title"}], exhausted()]}
    )
    archive, _, result = collect(tmp_path, client)

    assert_only_the_middle_object_failed(result, phase="properties")
    assert result.counters["property_pages_collected"] == 2, "one per healthy page"
    assert f"property-{SECOND_PAGE_ID}-title" in archived_kinds(archive), (
        "the property page that did arrive is still archived"
    )


def test_a_normalization_failure_makes_the_whole_object_incomplete(tmp_path: Path) -> None:
    """No created_time and no last_edited_time: normalize_notion refuses it."""
    broken = {"object": "page", "id": SECOND_PAGE_ID, "properties": {}}
    client = three_page_client(
        pages={
            PAGE_ID: page_body(PAGE_ID, edited="2026-08-26T01:00:00Z"),
            SECOND_PAGE_ID: broken,
            THIRD_PAGE_ID: page_body(THIRD_PAGE_ID, edited="2026-08-26T03:00:00Z"),
        }
    )
    _, _, result = collect(tmp_path, client)

    assert result.pages_collected == 2
    assert result.objects_completed == 2
    detail = failure_for(result, SECOND_PAGE_ID)
    assert detail["phase"] == "normalize"
    assert detail["error_type"] == "ValueError"
    assert detail["resolution"] == "permanent", "a malformed object is a final answer"
    checkpoint = json.loads((tmp_path / "manifests/notion/test/checkpoint.json").read_text())
    assert SECOND_PAGE_ID not in checkpoint["known_objects"], (
        "an object that produced no event was not successfully checked"
    )


def test_a_failed_object_is_never_marked_fetched_in_the_link_queue(tmp_path: Path) -> None:
    queue = NotionLinkQueue(tmp_path, "test")
    failing = f"https://www.notion.so/{SECOND_PAGE_ID.replace('-', '')}"
    healthy = f"https://www.notion.so/{PAGE_ID.replace('-', '')}"
    queue.add_urls([failing, healthy], source="slack", run_id="slack-run")
    archive = RawArchive(tmp_path, "notion", "notion-run", "test")
    client = three_page_client(comment_errors={SECOND_PAGE_ID: exhausted()})

    result = NotionCollector(client, archive, queue).collect(since=SINCE)

    stored = queue.load()
    assert stored[failing]["status"] == "failed"
    assert "unresolved timeout during comments" in stored[failing]["last_error"]
    assert "6 attempts" in stored[failing]["last_error"]
    assert stored[healthy]["status"] == "fetched", "the other queued URL is unaffected"
    assert result.link_queue_fetched == 1


def test_partial_raw_pages_from_a_failed_object_are_kept_untouched(tmp_path: Path) -> None:
    """An incomplete capture is still evidence. Nothing is deleted or rewritten."""
    client = three_page_client(
        blocks={
            SECOND_PAGE_ID: [
                [{"id": "deep-1", "type": "paragraph", "has_children": False}],
                exhausted(),
            ]
        }
    )
    archive, _, result = collect(tmp_path, client)

    partial = [
        item
        for item in archive.files
        if item["kind"] in {f"page-{SECOND_PAGE_ID}", f"blocks-{SECOND_PAGE_ID}"}
    ]
    assert len(partial) == 2, "the retrieved page and the one block page that arrived"
    for entry in partial:
        stored = tmp_path / entry["path"]
        assert stored.exists(), "a partial raw page is never removed"
        body = json.loads(gzip.decompress(stored.read_bytes()))
        assert body, "and it still holds what the API actually returned"
    manifest = json.loads(result.manifest_path.read_text())
    assert {entry["path"] for entry in partial} <= {entry["path"] for entry in manifest["files"]}, (
        "the manifest still lists every raw page the run wrote"
    )


def test_a_failed_object_is_not_remembered_as_successfully_checked(tmp_path: Path) -> None:
    client = three_page_client(page_errors={SECOND_PAGE_ID: exhausted()})
    collect(tmp_path, client)

    checkpoint = json.loads((tmp_path / "manifests/notion/test/checkpoint.json").read_text())
    assert set(checkpoint["known_objects"]) == {PAGE_ID, THIRD_PAGE_ID}


# ------------------------------------------------------- checkpoint safety


def test_the_watermark_cannot_advance_past_an_unresolved_failure(tmp_path: Path) -> None:
    """SECOND failed at 02:00, so THIRD's 03:00 must not become the watermark."""
    client = three_page_client(comment_errors={SECOND_PAGE_ID: exhausted()})
    _, _, result = collect(tmp_path, client)

    checkpoint = json.loads((tmp_path / "manifests/notion/test/checkpoint.json").read_text())
    assert checkpoint["last_edited_watermark"] == "2026-08-26T01:00:00+00:00", (
        "the watermark stops below the failure, so the next run reads it again"
    )
    assert result.counters["watermark_held"] is False, "a known edit time bounded it precisely"


def test_the_watermark_is_held_when_an_unresolved_failure_has_no_edit_time(
    tmp_path: Path,
) -> None:
    """A link-queue candidate carries no last_edited_time. If it fails
    unresolved there is no position to stop below, so nothing may move."""
    seed = RawArchive(tmp_path, "notion", "notion-run-0", "test")
    seed.write_checkpoint(
        {
            "schema_version": 2,
            "source": "notion",
            "run_id": "notion-run-0",
            "last_edited_watermark": "2026-08-20T00:00:00+00:00",
            "known_objects": {},
        }
    )
    queue = NotionLinkQueue(tmp_path, "test")
    url = "https://www.notion.so/33333333444455556666777788889999"
    queue.add_urls([url], source="slack", run_id="slack-run")
    archive = RawArchive(tmp_path, "notion", "notion-run-1", "test")
    client = FakeNotionClient(
        page_errors={"33333333-4444-5555-6666-777788889999": exhausted()},
    )

    result = NotionCollector(client, archive, queue).collect(since=SINCE, recheck_limit=0)

    checkpoint = json.loads((tmp_path / "manifests/notion/test/checkpoint.json").read_text())
    assert checkpoint["last_edited_watermark"] == "2026-08-20T00:00:00+00:00", (
        "the healthy page's 2026-08-26 edit must not become the watermark"
    )
    assert result.counters["watermark_held"] is True
    assert WATERMARK_HELD_NOTE in json.loads(result.manifest_path.read_text())["coverage_notes"]


def test_holding_the_watermark_leaves_no_watermark_when_there_was_none(tmp_path: Path) -> None:
    assert _advance_watermark(previous=None, collected=[], failed=[], hold=True) is None
    assert (
        _advance_watermark(
            previous="2026-08-20T00:00:00+00:00",
            collected=[datetime(2026, 8, 30, tzinfo=timezone.utc)],
            failed=[],
            hold=True,
        )
        == "2026-08-20T00:00:00+00:00"
    )


def test_a_permanent_recheck_skip_does_not_freeze_the_watermark_forever(tmp_path: Path) -> None:
    """A deleted page 404s on every future run. Holding for it would stall the
    watermark permanently, and its answer is final, so it does not hold."""
    seed = RawArchive(tmp_path, "notion", "notion-run-0", "test")
    seed.write_checkpoint(
        {
            "schema_version": 2,
            "source": "notion",
            "run_id": "notion-run-0",
            "last_edited_watermark": "2026-08-20T00:00:00+00:00",
            "known_objects": {SECOND_PAGE_ID: {"type": "page", "last_checked": "2026-08-20T00:00:00+00:00"}},
        }
    )
    _, _, result = collect(tmp_path, FakeNotionClient(), run_id="notion-run-1", recheck_limit=10)

    checkpoint = json.loads((tmp_path / "manifests/notion/test/checkpoint.json").read_text())
    assert checkpoint["last_edited_watermark"] == "2026-08-26T01:00:00+00:00"
    assert result.counters["watermark_held"] is False
    assert result.status == "success_with_skips", "a definitive answer is a skip, not a degradation"
    assert failure_for(result, SECOND_PAGE_ID)["resolution"] == "permanent"


# ------------------------------------------------------------ search failure


def test_search_failing_mid_walk_keeps_what_it_listed_and_withholds_the_checkpoint(
    tmp_path: Path,
) -> None:
    client = FakeNotionClient(search_error=exhausted(retry_class="server_error", status=503))
    _, _, result = collect(tmp_path, client)

    assert result.pages_collected == 1, "what search did list is still collected"
    assert result.counters["search"]["complete"] is False
    assert result.status == "degraded"
    assert result.checkpoint_advanced is False, (
        "discovery was partial, so the window may hide objects that were never listed"
    )
    manifest = json.loads(result.manifest_path.read_text())
    assert manifest["truncated"] is True
    assert manifest["truncation"][0]["reason"] == "search_incomplete"
    assert SEARCH_INCOMPLETE_NOTE in manifest["coverage_notes"]


# --------------------------------------------------------- eventual recovery


class _Body(io.BytesIO):
    """Minimal stand-in for the response object `urlopen` hands back."""

    def __enter__(self) -> "_Body":
        return self

    def __exit__(self, *_: Any) -> bool:
        return False


class ScriptedNotion:
    """A whole Notion conversation as canned bodies, with injected timeouts.

    Used with a *real* NotionClient so the recovery path is exercised through
    both layers: the transport fails, the client retries, and the collector
    never learns anything went wrong.
    """

    def __init__(self, *, fail_path: str, times: int) -> None:
        self.fail_path = fail_path
        self.remaining = times
        self.paths: list[str] = []

    def __call__(self, request: Any, timeout: float | None = None) -> _Body:
        path = urllib.parse.urlsplit(request.full_url).path
        self.paths.append(path)
        if self.fail_path in path and self.remaining > 0:
            self.remaining -= 1
            raise TimeoutError("The read operation timed out")
        return _Body(json.dumps(self._body(path)).encode())

    def _body(self, path: str) -> dict[str, Any]:
        if path == "/v1/search":
            return {
                "results": [
                    {"object": "page", "id": PAGE_ID, "last_edited_time": "2026-08-26T01:00:00Z"}
                ],
                "has_more": False,
            }
        if path.startswith("/v1/blocks/"):
            return {
                "results": [
                    {
                        "id": "block-1",
                        "type": "paragraph",
                        "has_children": False,
                        "paragraph": {"rich_text": [{"plain_text": "recovered content"}]},
                    }
                ],
                "has_more": False,
            }
        if path in {"/v1/comments", "/v1/users"}:
            return {"results": [], "has_more": False}
        if path.startswith("/v1/pages/"):
            return page_body(PAGE_ID)
        raise AssertionError(f"unscripted path {path}")


@pytest.mark.parametrize("fail_path", ["/v1/search", "/v1/pages/", "/v1/blocks/", "/v1/comments"])
def test_a_transient_failure_the_client_recovers_from_completes_normally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail_path: str
) -> None:
    """Eventual recovery is an ordinary run: full coverage, an event, a fetched
    queue mark, an advanced checkpoint, and no skip anywhere in the manifest."""
    transport = ScriptedNotion(fail_path=fail_path, times=2)
    monkeypatch.setattr("urllib.request.urlopen", transport)
    sleeps: list[float] = []
    client = NotionClient(
        "ntn_synthetic_token", sleeper=sleeps.append, jitter=lambda _delay: 0.0
    )
    queue = NotionLinkQueue(tmp_path, "test")
    url = f"https://www.notion.so/{PAGE_ID.replace('-', '')}"
    queue.add_urls([url], source="slack", run_id="slack-run")
    archive = RawArchive(tmp_path, "notion", "notion-run", "test")

    result = NotionCollector(client, archive, queue).collect(since=SINCE)

    assert sleeps == [1.0, 2.0], "the retry really happened, with bounded backoff"
    assert client.transient_retries == 2
    assert client.exhausted_requests == 0
    assert result.status == "success"
    assert result.coverage_complete is True
    assert result.pages_collected == 1
    assert result.objects_completed == 1
    assert result.pages_skipped == 0
    assert "recovered content" in result.events[0].payload["content_text"]
    assert queue.load()[url]["status"] == "fetched"
    assert result.checkpoint_advanced is True
    manifest = json.loads(result.manifest_path.read_text())
    assert manifest["skips"] == []
    assert manifest["errors"] == []
    assert manifest["counters"]["transient_retries"] == 2


# ------------------------------------------------------- retry accounting


def test_client_retry_accounting_reaches_the_manifest(tmp_path: Path) -> None:
    client = three_page_client(
        rate_limit_hits=4,
        transient_retries=11,
        retry_counts={"timeout": 7, "rate_limit": 4},
        exhausted_requests=1,
    )
    _, _, result = collect(tmp_path, client)

    counters = result.counters
    assert counters["rate_limit_hits"] == 4
    assert counters["transient_retries"] == 11
    assert counters["transient_retries_by_class"] == {"timeout": 7, "rate_limit": 4}
    assert counters["requests_exhausted"] == 1
    manifest = json.loads(result.manifest_path.read_text())
    assert manifest["rate_limit_hits"] == 4
    assert "ntn_" not in json.dumps(manifest), "no credential shape reaches a manifest"


def test_a_clean_run_still_reports_complete_coverage(tmp_path: Path) -> None:
    _, _, result = collect(tmp_path, FakeNotionClient())

    assert result.status == "success"
    assert result.coverage_complete is True
    assert result.objects_completed == 1
    assert result.objects_failed_unresolved == 0
    manifest = json.loads(result.manifest_path.read_text())
    assert manifest["counters"]["objects_failed_by_phase"] == {}
    assert UNRESOLVED_FAILURE_NOTE not in manifest["coverage_notes"]
