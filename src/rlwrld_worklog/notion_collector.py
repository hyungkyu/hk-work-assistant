"""Daily incremental Notion capture over the official read-only API.

Coverage design, and the honest limits of it:

  * `/search` with no object filter enumerates every object the integration can
    see: pages, databases and data sources. It is ordered by
    `last_edited_time` descending, so the walk stops once a result page falls
    entirely below the requested window; that early stop is recorded in the
    manifest rather than assumed to be free.
  * `/search` is **not** a perfect change feed. It omits archived and trashed
    objects, objects never shared with the integration, and it can lag an
    edit. So three further paths run: the Notion link queue (every Notion URL
    seen in Slack or Calendar), a bounded re-check of objects this collector
    has seen before (which is the only way an archive, a trash or a lost share
    becomes observable), and `last_edited_time` checkpointing that only
    advances over objects that were actually fetched.
  * Every descendant block is fetched recursively, including the child blocks
    that meeting-notes blocks point at, plus paginated title/rich-text/relation
    properties and comments under one of two named strategies -- see
    `COMMENT_STRATEGIES`, which records which sweep a run used and what the
    cheaper one gives up.
  * The checkpoint watermark advances only across the contiguous prefix of
    successfully fetched objects: it is never moved past an object whose fetch
    failed, so a permission error cannot silently skip a window.
  * **One candidate object is the unit of completeness.** If the retrieval, the
    recursive block walk, the per-block comment sweep, the paginated properties
    or the normalization of an object cannot be finished -- because the client
    exhausted its transient retries, or hit a status the inner handlers do not
    absorb -- that object alone is recorded as a failure. It emits no event, is
    not marked fetched in the link queue, and is not remembered as successfully
    checked. Every other candidate still runs. The raw pages already written
    for it stay exactly where they are: the archive is append-only, and a
    partial capture is still evidence.

Attachments are preserved as the property/block JSON that names them. No file
body is ever downloaded.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Protocol, Sequence

from .archive import RawArchive
from .link_queue import NotionLinkQueue
from .links import canonical_notion_page_id
from .models import TimelineEvent
from .normalizers import normalize_notion
from .notion_client import NotionApiError, NotionClient

NOTION_CAPTURE_PROFILE = "live-notion-api/v1"
CHECKPOINT_SCHEMA_VERSION = 2
DEFAULT_RECHECK_LIMIT = 100
# No arbitrary comment cap for a full-density run. A finite default would make
# every large workspace mark itself truncated, withhold the checkpoint, and
# then redo the same window forever. The bound is the API client's rate-limit
# handling; an operator can still set a finite budget explicitly.
DEFAULT_COMMENT_REQUEST_BUDGET: int | None = None
MAX_TRACKED_OBJECTS = 20_000

# How the comment sweep decides which blocks to ask about.
#
#   page_first  -- ask the object for its own comments, and sweep its blocks
#                  only if that answered with at least one comment.
#   every_block -- ask every block, always. Exhaustive, and expensive.
#
# The default is `page_first` because of what one completed day measured:
# 6,053 block requests and 1,500 comment requests over 176 pages, and the
# whole 1,500 returned a single comment. It is a trade, not a free win -- what
# it gives up is named in PAGE_FIRST_COMMENTS_NOTE and written into every
# manifest -- so the exhaustive sweep stays selectable for a run that wants it.
COMMENT_STRATEGIES = ("page_first", "every_block")
DEFAULT_COMMENT_STRATEGY = "page_first"

# Block types that are another object rather than content of this one. The
# block walk records them and stops; see `_collect_blocks`.
PAGE_BOUNDARY_BLOCK_TYPES = frozenset({"child_page", "child_database"})

COVERAGE_NOTES = (
    "notion.search_is_not_a_change_feed: /search omits archived and trashed objects and "
    "anything not shared with the integration, and can lag an edit. Deletion and "
    "un-sharing are observable only through the bounded re-check of known objects.",
    "notion.attachments_are_metadata_only: file blocks and file properties are preserved as "
    "JSON with their (expiring) URLs; no file body is downloaded.",
)

# What a day's set of documents is made of, and where each part can fail. This
# replaces a note that claimed data-source rows arrive through /search alone,
# which was an assumption nobody had checked.
DOCUMENT_SET_NOTE = (
    "notion.document_set_is_search_plus_seeds: the documents attributed to a day are what "
    "/search listed inside the window, plus every row a data source seen in that window "
    "reports as edited in it, plus the operator's explicit seed pages checked one by one. "
    "The block walk stops at child_page and child_database blocks, so no object is pulled "
    "in merely because its parent was edited. counters.child_object_refs_unlisted says how "
    "many child objects a run saw that /search had not listed: it is the run's own measure "
    "of whether search is still finding what it should, and a jump in it is the signal to "
    "look again."
)

SEED_PAGES_NOTE = (
    "notion.seed_pages_are_checked_not_walked: each configured seed page is retrieved once "
    "per run and collected only if its own last_edited_time falls in the window. A seed is "
    "insurance against /search missing a page that matters, not a standing instruction to "
    "re-capture it daily."
)

DATA_SOURCE_QUERY_NOTE = (
    "notion.data_source_rows_are_queried: every data source listed in the window is queried "
    "for rows edited in it, rather than trusting /search to have listed them. The query "
    "reaches only data sources the run saw; a database edited in the window whose own object "
    "/search did not list is not queried, and its rows are covered by /search alone."
)

# One of these two is always recorded, so a manifest names the sweep that
# produced its comments instead of leaving a reader to infer it from counters.
EXHAUSTIVE_COMMENTS_NOTE = (
    "notion.comments_are_per_block: an inline comment lives on its own block, so /comments "
    "was queried for every block of every fetched object. This sweep is exhaustive; the "
    "API client's rate-limit handling is the bound."
)

PAGE_FIRST_COMMENTS_NOTE = (
    "notion.comments_page_first: /comments was asked for each object itself, and the "
    "per-block sweep ran only for objects whose own query returned at least one comment. "
    "What this gives up is real: an inline comment left on a block of a page that carries "
    "no page-level comment is not fetched by this run, and its absence here is not evidence "
    "it does not exist. Measured on one completed day, the exhaustive sweep spent 1,500 "
    "requests to return one comment. counters.comment_blocks_unswept says how many blocks "
    "were skipped this way."
)

BOUNDED_COMMENTS_NOTE = (
    "notion.comment_requests_capped: an explicit comment-request budget was configured for "
    "this run, so the per-block comment sweep is not exhaustive. Reaching the cap marks the "
    "run truncated and withholds the checkpoint."
)

DRY_RUN_LINK_QUEUE_NOTE = (
    "notion.link_queue_not_persisted_in_dry_run: a dry or smoke run reads the link queue but "
    "writes nothing to it, so no URL is marked fetched, failed or unresolved and no discovery "
    "from this run is remembered. The next real run re-reads every still-pending URL."
)

OBJECT_ISOLATION_NOTE = (
    "notion.object_is_the_unit_of_completeness: a candidate object whose retrieval, block "
    "walk, comment sweep, properties or normalization could not be finished is recorded as a "
    "failed object and skipped on its own. It emits no event, is not marked fetched and is not "
    "remembered as checked, the run continues to every other candidate, and the raw pages "
    "already written for it are kept immutably rather than deleted or rewritten."
)

UNRESOLVED_FAILURE_NOTE = (
    "notion.unresolved_objects_present: at least one object failed without the API ever giving "
    "a final answer -- the client exhausted its transient retries. The run is degraded, not "
    "complete: those objects are still unread and the checkpoint is held below them."
)

WATERMARK_HELD_NOTE = (
    "notion.watermark_held_for_unknown_position: an object failed unresolved and carried no "
    "last_edited_time, so its position in the edit ordering is unknown. The watermark was left "
    "where it was rather than advanced over a position that cannot be bounded."
)

PERMANENT_SKIP_WATERMARK_NOTE = (
    "notion.permanent_skips_do_not_hold_the_watermark: an object the API answered definitively "
    "for (400/401/403/404) and that carries no last_edited_time -- a deleted or un-shared "
    "object found by re-check -- does not hold the watermark. The answer is final, so there is "
    "no unknown window hiding behind it, and holding would freeze the watermark forever."
)

DATE_SLICE_NOTE = (
    "notion.date_slice_capture: this run captured one bounded last_edited_time window "
    "instead of the live head. The link queue and the known-object re-check were not "
    "consulted and the checkpoint was not advanced: a historical slice proves nothing "
    "about everything edited before its end."
)

SEARCH_INCOMPLETE_NOTE = (
    "notion.search_walk_incomplete: the /search walk stopped on an error before it had read "
    "every result page, so discovery for this run is partial. The run is marked truncated and "
    "the checkpoint is withheld rather than advanced over objects that were never listed."
)

# The phase of per-object work that a failure interrupted. Recorded per failure
# so an operator can tell a dead object from a page whose 74k-block subtree
# timed out three quarters of the way through.
OBJECT_PHASES = ("retrieve", "archive", "blocks", "comments", "properties", "normalize")


class NotionApi(Protocol):
    def iter_search(self, *, object_filter: str | None = None) -> Iterator[dict[str, Any]]: ...
    def retrieve_page(self, page_id: str) -> dict[str, Any]: ...
    def iter_block_children(self, block_id: str) -> Iterator[dict[str, Any]]: ...
    def iter_comments(self, block_id: str) -> Iterator[dict[str, Any]]: ...
    def iter_property_items(self, page_id: str, property_id: str) -> Iterator[dict[str, Any]]: ...
    # Optional: a client without it makes the run record the gap in coverage
    # rather than fail, because a scripted fake in a test predates the method.
    # def iter_data_source_rows(
    #     self, data_source_id: str, *, since: str, until: str | None = None
    # ) -> Iterator[dict[str, Any]]: ...


@dataclass(frozen=True)
class NotionCollectionResult:
    run_id: str
    pages_discovered: int
    pages_collected: int
    pages_skipped: int
    events: tuple[TimelineEvent, ...]
    manifest_path: Path
    checkpoint_advanced: bool = False
    databases_collected: int = 0
    users_seen: int = 0
    comments_collected: int = 0
    blocks_collected: int = 0
    link_queue_fetched: int = 0
    link_queue_unresolved: int = 0
    archived_observed: int = 0
    counters: dict[str, Any] = field(default_factory=dict)
    status: str = "success"
    objects_completed: int = 0
    objects_failed_unresolved: int = 0
    coverage_complete: bool = True


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


@dataclass
class _Candidate:
    object_id: str
    object_type: str  # page | data_source | database
    source: str  # search | link_queue | recheck | data_source_row | seed
    last_edited_time: str | None = None


class NotionCollector:
    def __init__(
        self,
        client: NotionApi,
        archive: RawArchive,
        link_queue: NotionLinkQueue,
    ) -> None:
        self.client = client
        self.archive = archive
        self.link_queue = link_queue
        self._comment_budget: int | None = DEFAULT_COMMENT_REQUEST_BUDGET
        self._comment_requests_made = 0
        self._comment_budget_exhausted = False
        self._comment_block_sweeps = 0
        self._comment_blocks_unswept = 0

    # ------------------------------------------------------------- helpers

    def _search(
        self,
        candidates: dict[str, _Candidate],
        *,
        since: datetime,
        until: datetime | None = None,
    ) -> dict[str, Any]:
        """Walk /search, keeping whatever it managed to list.

        A failure part-way through the walk is not fatal: the candidates
        already listed (and the link queue and re-check candidates gathered
        elsewhere) are still worth collecting. It *is* a truncation, though --
        objects in the window may never have been listed -- so it marks the run
        truncated, which withholds the checkpoint.

        ``until`` bounds the window from above. `/search` is ordered by
        `last_edited_time` descending, so a bounded walk skips the newer
        objects at the front, collects the slice, and still stops as soon as
        results fall below ``since``. That is what makes one day's capture
        finite: without an upper bound a first sync has to walk the whole
        workspace before it can stop.
        """
        stats: dict[str, Any] = {
            "pages_walked": 0,
            "results": 0,
            "stopped_early": False,
            "complete": True,
            "skipped_above_window": 0,
            "by_object": {},
        }
        cursor_pages = 0
        try:
            for search_page in self.client.iter_search():
                cursor_pages += 1
                results = search_page.get("results") or []
                self.archive.write_page(
                    "search",
                    search_page,
                    endpoint="POST /search",
                    request={"sort": "last_edited_time desc"},
                    item_count=len(results),
                )
                page_had_recent = False
                for result in results:
                    if not isinstance(result, dict) or not result.get("id"):
                        continue
                    stats["results"] += 1
                    object_type = str(result.get("object") or "unknown")
                    stats["by_object"][object_type] = stats["by_object"].get(object_type, 0) + 1
                    edited = _parse_time(result.get("last_edited_time"))
                    if until is not None and edited is not None and edited > until:
                        # Newer than this slice. The walk is descending, so
                        # these sit in front of the window and the walk must
                        # keep going rather than stop.
                        stats["skipped_above_window"] += 1
                        page_had_recent = True
                        continue
                    if edited is None or edited >= since:
                        page_had_recent = True
                        object_id = str(result["id"])
                        candidates[object_id] = _Candidate(
                            object_id=object_id,
                            object_type=object_type,
                            source="search",
                            last_edited_time=result.get("last_edited_time"),
                        )
                if not page_had_recent:
                    stats["stopped_early"] = True
                    break
        except NotionApiError as error:
            stats["complete"] = False
            stats["error"] = {
                "status": error.status,
                "code": error.code,
                "resolution": error.resolution,
                "retry_class": error.retry_class,
                "attempts": error.attempts,
                "error": str(error),
            }
            self.archive.note_error("search_incomplete", **stats["error"])
            self.archive.note_truncation(
                "search_incomplete", pages_walked=cursor_pages, resolution=error.resolution
            )
            self.archive.note_coverage(SEARCH_INCOMPLETE_NOTE)
        stats["pages_walked"] = cursor_pages
        return stats

    def _query_data_sources(
        self,
        candidates: dict[str, _Candidate],
        *,
        since: datetime,
        until: datetime | None,
    ) -> dict[str, Any]:
        """Ask each data source in the window which of its rows changed.

        Runs after `_search`, over the data sources search itself listed, and
        adds any row it reports that search did not already name. A row is a
        page, so it joins the same queue as everything else.

        A client without the method -- an older scripted fake, or a build
        predating it -- makes the run record the gap in coverage and carry on.
        A failed query is the same: it narrows this pass, not the run.
        """
        stats: dict[str, Any] = {
            "data_sources_queried": 0,
            "requests": 0,
            "rows_seen": 0,
            "rows_new": 0,
            "failed": 0,
            "complete": True,
        }
        query = getattr(self.client, "iter_data_source_rows", None)
        sources = sorted(
            identifier
            for identifier, candidate in candidates.items()
            if candidate.object_type in {"data_source", "database"}
        )
        if query is None:
            if sources:
                stats["complete"] = False
                stats["reason"] = "client has no iter_data_source_rows"
                self.archive.note_coverage(
                    "notion.data_source_query_unavailable: this run's client could not query "
                    "data sources, so rows are covered by /search alone."
                )
            return stats
        since_iso = since.isoformat()
        until_iso = until.isoformat() if until is not None else None
        for data_source_id in sources:
            stats["data_sources_queried"] += 1
            try:
                for page in query(data_source_id, since=since_iso, until=until_iso):
                    stats["requests"] += 1
                    results = page.get("results") or []
                    self.archive.write_page(
                        f"rows-{data_source_id}",
                        page,
                        endpoint="POST /data_sources/{id}/query",
                        request={"data_source_id": data_source_id, "since": since_iso},
                        item_count=len(results),
                    )
                    for row in results:
                        if not isinstance(row, dict) or not row.get("id"):
                            continue
                        stats["rows_seen"] += 1
                        row_id = str(row["id"])
                        if row_id in candidates:
                            continue
                        stats["rows_new"] += 1
                        candidates[row_id] = _Candidate(
                            object_id=row_id,
                            object_type=str(row.get("object") or "page"),
                            source="data_source_row",
                            last_edited_time=row.get("last_edited_time"),
                        )
            except NotionApiError as error:
                # One inaccessible data source is a hole in this pass, named
                # and survived. `/search` still covered whatever it listed.
                stats["failed"] += 1
                stats["complete"] = False
                self.archive.note_skip(
                    "data_source_query_failed",
                    data_source_id=data_source_id,
                    status=error.status,
                    code=error.code,
                )
        return stats

    def _check_seeds(
        self,
        candidates: dict[str, _Candidate],
        seeds: Sequence[str],
        *,
        since: datetime,
        until: datetime | None,
    ) -> dict[str, Any]:
        """Retrieve each configured seed page and keep the ones edited in the window.

        A seed is the operator's answer to "what must never be missed". It
        costs one retrieval per run whether or not it changed, and it earns a
        full capture only when its own `last_edited_time` lands in the window
        -- so a long seed list stays cheap and a seed never inflates a quiet
        day into a busy one.
        """
        stats: dict[str, Any] = {
            "configured": len(seeds),
            "checked": 0,
            "in_window": 0,
            "added": 0,
            "failed": 0,
        }
        if not seeds:
            return stats
        self.archive.note_coverage(SEED_PAGES_NOTE)
        for seed in seeds:
            seed_id = str(seed).strip()
            if not seed_id:
                continue
            stats["checked"] += 1
            try:
                obj, resolved = self._retrieve(
                    _Candidate(object_id=seed_id, object_type="page", source="seed")
                )
            except NotionApiError as error:
                stats["failed"] += 1
                self.archive.note_skip(
                    "seed_inaccessible", object_id=seed_id, status=error.status, code=error.code
                )
                continue
            edited = _parse_time(obj.get("last_edited_time"))
            if edited is None:
                continue
            if edited < since or (until is not None and edited >= until):
                continue
            stats["in_window"] += 1
            if seed_id in candidates:
                continue
            stats["added"] += 1
            candidates[seed_id] = _Candidate(
                object_id=seed_id,
                object_type=resolved,
                source="seed",
                last_edited_time=obj.get("last_edited_time"),
            )
        return stats

    def _retrieve(self, candidate: _Candidate) -> tuple[dict[str, Any], str]:
        """Retrieve one object, resolving an unknown id across object types.

        A link-queue URL says nothing reliable about whether it points at a
        page, a data source or a database, so each supported type is tried in
        turn and only 400/404 moves on to the next.
        """
        order = {
            "page": ("page", "data_source", "database"),
            "data_source": ("data_source", "database", "page"),
            "database": ("database", "data_source", "page"),
        }.get(candidate.object_type, ("page", "data_source", "database"))
        retrievers = {
            "page": getattr(self.client, "retrieve_page", None),
            "data_source": getattr(self.client, "retrieve_data_source", None),
            "database": getattr(self.client, "retrieve_database", None),
        }

        last_error: NotionApiError | None = None
        for kind in order:
            call = retrievers.get(kind)
            if call is None:
                continue
            try:
                return call(candidate.object_id), kind
            except NotionApiError as error:
                last_error = error
                if error.status not in {400, 404}:
                    raise
        raise last_error or NotionApiError("no retrieval path for object", status=404)

    def _collect_blocks(
        self, root_id: str
    ) -> tuple[list[dict[str, Any]], list[str], list[str]]:
        """This object's own blocks, their ids, and the child objects not entered.

        The ids matter beyond traversal: a Notion comment hangs off the block
        it was left on, so a leaf paragraph can carry a discussion that a
        page-level /comments query never returns.

        **The walk stops at a page boundary.** A `child_page` or
        `child_database` block declares `has_children`, so a walk that only
        consults that flag descends into the whole tree beneath an object and
        files it under the day the *parent* was edited. Measured on two
        collected days, that descent was two thirds of the block requests on
        the heavier one and it bought almost nothing: of the blocks below a
        child page that `/search` had not itself returned for that day, none
        on 2026-08-25 and 2.2% on 2026-09-02 had been edited in the window.
        Nearly every in-window block it did find sat under a child page
        `/search` *had* returned -- which this collector walks as its own root
        anyway. So the descent was mostly duplicate work, and where it was not
        duplicate it was attributing untouched pages to a day nobody touched
        them.

        The child object is still recorded: its block sits in the parent's
        children response like any other, so "this document linked to that one"
        survives. Only the descent stops. The ids are returned so the caller
        can count how many of them `/search` did not list, which is the run's
        own measure of whether search is still finding what it should.
        """
        blocks: list[dict[str, Any]] = []
        block_ids: list[str] = []
        child_objects: list[str] = []
        seen: set[str] = set()
        pending = [root_id]
        while pending:
            parent_id = pending.pop()
            if parent_id in seen:
                continue
            seen.add(parent_id)
            try:
                # Each page is archived as it arrives rather than after the
                # whole subtree paginates: a failure on page 40 of a 74,000
                # block walk must not throw away the 39 raw responses already
                # in hand. They are evidence, and the archive is append-only.
                for child_page in self.client.iter_block_children(parent_id):
                    results = child_page.get("results") or []
                    self.archive.write_page(
                        f"blocks-{parent_id}",
                        child_page,
                        endpoint="GET /blocks/{id}/children",
                        request={"block_id": parent_id},
                        item_count=len(results),
                    )
                    for block in results:
                        if not isinstance(block, dict):
                            continue
                        blocks.append(block)
                        block_id = block.get("id")
                        if block_id:
                            block_ids.append(str(block_id))
                        if block_id and block.get("type") in PAGE_BOUNDARY_BLOCK_TYPES:
                            # A page boundary. Recorded, not entered -- see the
                            # docstring. `has_children` is deliberately not
                            # consulted: it is true here, and consulting it is
                            # exactly what made the walk unbounded.
                            child_objects.append(str(block_id))
                            continue
                        if block_id and block.get("has_children"):
                            pending.append(str(block_id))
                        meeting = block.get("meeting_notes") or block.get("transcription") or {}
                        pointers = meeting.get("children") if isinstance(meeting, dict) else None
                        for key in ("summary_block_id", "notes_block_id", "transcript_block_id"):
                            if isinstance(pointers, dict) and pointers.get(key):
                                pending.append(str(pointers[key]))
            except NotionApiError as error:
                # A definitive 400/403/404 is a documented hole in one subtree
                # and the object survives it. Anything else -- including a
                # transient failure the client retried to exhaustion -- means
                # the subtree is unknown, so it is left to the per-object
                # handler to fail this object as a whole.
                if error.status in {400, 403, 404}:
                    self.archive.note_skip(
                        "block_children_inaccessible", block_id=parent_id, status=error.status, code=error.code
                    )
                    continue
                raise
        return (
            blocks,
            sorted(set(block_ids) | (seen - {root_id})),
            sorted(set(child_objects)),
        )

    def _comments_for(self, block_id: str) -> list[dict[str, Any]]:
        """One /comments query, or none at all once the budget is spent."""
        if self._comment_budget is not None and self._comment_requests_made >= self._comment_budget:
            self._comment_budget_exhausted = True
            return []
        self._comment_requests_made += 1
        results: list[dict[str, Any]] = []
        try:
            for page in self.client.iter_comments(block_id):
                page_results = page.get("results") or []
                self.archive.write_page(
                    f"comments-{block_id}",
                    page,
                    endpoint="GET /comments",
                    request={"block_id": block_id},
                    item_count=len(page_results),
                )
                results.extend(page_results)
        except NotionApiError as error:
            if error.status in {400, 403, 404}:
                self.archive.note_skip(
                    "comments_inaccessible", block_id=block_id, status=error.status, code=error.code
                )
                return results
            raise
        return results

    def _collect_comments(
        self, object_id: str, block_ids: list[str], *, strategy: str
    ) -> list[dict[str, Any]]:
        """Comments for one object, asking the object itself before its blocks.

        Under `page_first` a page that answered with nothing has its blocks
        left alone. That is a deliberate loss of coverage rather than a free
        saving: a discussion that exists only as an inline comment on a block,
        with nothing at page level, is not fetched, and PAGE_FIRST_COMMENTS_NOTE
        says so in the manifest so the hole is a recorded one. The price of
        closing it is one request per block -- 6,053 blocks in a measured day,
        of which the 1,500 comment requests returned one comment.

        `every_block` keeps the exhaustive sweep for a run that would rather
        pay that.
        """
        comments = self._comments_for(object_id)
        if self._comment_budget_exhausted:
            # The budget stopped this, not the strategy. Nothing below would
            # run anyway, and counting these blocks as strategy-skipped would
            # blame the wrong bound.
            return comments
        if strategy == "page_first" and not comments:
            self._comment_blocks_unswept += len(block_ids)
            return comments
        self._comment_block_sweeps += 1
        for block_id in block_ids:
            comments.extend(self._comments_for(block_id))
            if self._comment_budget_exhausted:
                break
        return comments

    def _collect_paginated_properties(self, page: dict[str, Any]) -> int:
        page_id = str(page["id"])
        collected = 0
        for property_value in (page.get("properties") or {}).values():
            if not isinstance(property_value, dict):
                continue
            if property_value.get("type") not in {"title", "rich_text", "relation", "people", "rollup"}:
                continue
            property_id = property_value.get("id")
            if not property_id:
                continue
            try:
                # Archived per page, for the same reason as the block walk.
                for result in self.client.iter_property_items(page_id, str(property_id)):
                    self.archive.write_page(
                        f"property-{page_id}-{property_id}",
                        result,
                        endpoint="GET /pages/{id}/properties/{property_id}",
                        request={"page_id": page_id, "property_id": str(property_id)},
                        item_count=len(result.get("results") or []) if isinstance(result, dict) else None,
                    )
                    collected += 1
            except NotionApiError as error:
                if error.status in {400, 403, 404}:
                    self.archive.note_skip(
                        "property_inaccessible",
                        page_id=page_id,
                        property_id=str(property_id),
                        status=error.status,
                    )
                    continue
                raise
        return collected

    def _collect_users(self) -> int:
        iter_users = getattr(self.client, "iter_users", None)
        if iter_users is None:
            self.archive.note_coverage("notion.users_not_collected: client exposes no /users iterator.")
            return 0
        seen = 0
        try:
            for page in iter_users():
                results = page.get("results") or []
                self.archive.write_page(
                    "users",
                    page,
                    endpoint="GET /users",
                    item_count=len(results),
                )
                seen += len(results)
        except NotionApiError as error:
            self.archive.note_skip("users_inaccessible", status=error.status, code=error.code)
        return seen

    # -------------------------------------------------------------- collect

    def collect(
        self,
        *,
        since: datetime,
        max_objects: int | None = None,
        recheck_limit: int = DEFAULT_RECHECK_LIMIT,
        comment_request_budget: int | None = DEFAULT_COMMENT_REQUEST_BUDGET,
        comment_strategy: str = DEFAULT_COMMENT_STRATEGY,
        advance_checkpoint: bool = True,
        until: datetime | None = None,
        seed_pages: Sequence[str] = (),
    ) -> NotionCollectionResult:
        if comment_strategy not in COMMENT_STRATEGIES:
            # Refused before the first API call: a misspelled strategy that
            # silently fell back to a default would produce a run whose
            # manifest names a sweep it did not perform.
            raise ValueError(
                f"unknown comment strategy {comment_strategy!r}; expected one of "
                + ", ".join(COMMENT_STRATEGIES)
            )
        archive = self.archive
        # A dry or smoke run must not touch persistent production state. The
        # queue is swapped for a read-only view rather than guarded at each
        # call site, so every mark below is inert by construction.
        link_queue = self.link_queue.read_only_view() if archive.dry_run else self.link_queue
        if link_queue.read_only:
            archive.note_coverage(DRY_RUN_LINK_QUEUE_NOTE)
        self._comment_budget = comment_request_budget
        self._comment_requests_made = 0
        self._comment_budget_exhausted = False
        self._comment_block_sweeps = 0
        self._comment_blocks_unswept = 0
        archive.note_coverage(
            PAGE_FIRST_COMMENTS_NOTE
            if comment_strategy == "page_first"
            else EXHAUSTIVE_COMMENTS_NOTE
        )
        if comment_request_budget is not None:
            archive.note_coverage(BOUNDED_COMMENTS_NOTE)
        checkpoint = archive.read_checkpoint()
        archive.set_checkpoint_in(
            {
                "last_edited_watermark": checkpoint.get("last_edited_watermark"),
                "known_objects": len(checkpoint.get("known_objects") or {}),
                "run_id": checkpoint.get("run_id"),
            }
        )
        watermark = _parse_time(checkpoint.get("last_edited_watermark"))
        if until is not None:
            # A bounded slice captures one historical window, out of order with
            # the live head. Widening it by the checkpoint would pull in objects
            # outside the slice, and advancing the watermark from it would claim
            # everything up to that date had been seen. Neither is true, so the
            # window is taken literally and the checkpoint is left alone.
            effective_since = since
            advance_checkpoint = False
            archive.note_coverage(DATE_SLICE_NOTE)
        else:
            # Overlap is harmless because the ledger is idempotent, and a
            # stalled run must not leave a hole, so the earlier bound wins.
            effective_since = min(since, watermark) if watermark else since
        for note in COVERAGE_NOTES:
            archive.note_coverage(note)
        archive.set_requested_window(
            {
                "since_requested": since.isoformat(),
                "since_effective": effective_since.isoformat(),
                "until": until.isoformat() if until else None,
                "mode": "date_slice" if until else "incremental",
                "checkpoint_watermark": checkpoint.get("last_edited_watermark"),
                "max_objects": max_objects,
                "recheck_limit": recheck_limit,
                "comment_request_budget": comment_request_budget,
                "comment_strategy": comment_strategy,
            }
        )

        candidates: dict[str, _Candidate] = {}
        linked_by_id: dict[str, list[str]] = {}
        unresolved_urls: list[str] = []
        # The link queue and the re-check target the live head. A date slice is
        # a historical window, so pulling them in would make the slice
        # unbounded again and would consume queue entries a real run needs.
        slice_only = until is not None
        for item in ([] if slice_only else link_queue.pending()):
            page_id = item.get("page_id") or canonical_notion_page_id(item["url"])
            if page_id:
                candidates[page_id] = _Candidate(object_id=page_id, object_type="unknown", source="link_queue")
                linked_by_id.setdefault(page_id, []).append(item["url"])
            else:
                unresolved_urls.append(item["url"])
                link_queue.mark(item["url"], "unresolved", error="Notion page ID not present in URL")
                archive.note_skip("link_queue_unresolved", url_hash=_url_fingerprint(item["url"]))

        search_stats = self._search(candidates, since=effective_since, until=until)
        archive.note_coverage(DOCUMENT_SET_NOTE)
        # Order matters: the data-source pass reads what search listed, and the
        # seed pass is checked against the same window as everything else.
        archive.note_coverage(DATA_SOURCE_QUERY_NOTE)
        data_source_stats = self._query_data_sources(
            candidates, since=effective_since, until=until
        )
        seed_stats = self._check_seeds(
            candidates, seed_pages, since=effective_since, until=until
        )
        # Every id the run knows about before the walk starts. The walk's
        # `child_object_refs_unlisted` counter is measured against this, so a
        # child page that search *did* list is not counted as a miss.
        listed_object_ids = set(candidates)

        known_objects: dict[str, dict[str, Any]] = {
            str(key): dict(value)
            for key, value in (checkpoint.get("known_objects") or {}).items()
            if isinstance(value, dict)
        }
        rechecked = 0
        # The re-check exists to notice archives, trashes and lost shares at the
        # live head. A historical slice is not the head, so it stays out.
        if recheck_limit > 0 and not slice_only:
            stale = sorted(
                (identifier for identifier in known_objects if identifier not in candidates),
                key=lambda identifier: str(known_objects[identifier].get("last_checked") or ""),
            )
            for identifier in stale[:recheck_limit]:
                candidates[identifier] = _Candidate(
                    object_id=identifier,
                    object_type=str(known_objects[identifier].get("type") or "unknown"),
                    source="recheck",
                )
                rechecked += 1

        ordered = sorted(candidates.values(), key=lambda item: item.object_id)
        if max_objects is not None and len(ordered) > max_objects:
            ordered = ordered[:max_objects]
            archive.note_truncation("max_objects", limit=max_objects, discovered=len(candidates))

        events: list[TimelineEvent] = []
        skipped: list[dict[str, Any]] = []
        collected_edits: list[datetime] = []
        failed_edits: list[datetime] = []
        failures_by_phase: dict[str, int] = {}
        unresolved_failures = 0
        # Set when an object failed unresolved and its place in the edit
        # ordering is unknown, so no failure time can bound the watermark.
        hold_watermark = False
        databases_collected = 0
        child_object_refs = 0
        child_object_refs_unlisted = 0
        blocks_collected = 0
        comments_collected = 0
        properties_collected = 0
        archived_observed = 0
        link_fetched = 0
        objects_completed = 0
        now_iso = datetime.now(timezone.utc).isoformat()
        archive.note_coverage(OBJECT_ISOLATION_NOTE)

        for candidate in ordered:
            object_id = candidate.object_id
            # Everything below is provisional until the object is finished. It
            # is committed to the run's counters, events, link-queue marks and
            # known_objects in one step at the bottom, so a failure part-way
            # through leaves no half-counted object behind.
            phase = "retrieve"
            obj: dict[str, Any] | None = None
            resolved_type: str | None = None
            blocks: list[dict[str, Any]] = []
            comments: list[dict[str, Any]] = []
            property_pages = 0
            event: TimelineEvent | None = None
            failure: NotionApiError | Exception | None = None

            try:
                obj, resolved_type = self._retrieve(candidate)
                phase = "archive"
                archive.write_page(
                    f"{resolved_type}-{object_id}",
                    obj,
                    endpoint=f"GET /{resolved_type}s/{{id}}",
                    request={"object_id": object_id, "discovered_by": candidate.source},
                    item_count=1,
                )
                phase = "blocks"
                blocks, block_ids, child_refs = self._collect_blocks(object_id)
                child_object_refs += len(child_refs)
                child_object_refs_unlisted += sum(
                    1 for ref in child_refs if ref not in listed_object_ids
                )
                phase = "comments"
                comments = self._collect_comments(
                    object_id, block_ids, strategy=comment_strategy
                )
                if resolved_type == "page":
                    phase = "properties"
                    property_pages = self._collect_paginated_properties(obj)
                    phase = "normalize"
                    event = normalize_notion(obj, blocks=blocks, comments=comments)
            except NotionApiError as error:
                failure = error
            except (KeyError, TypeError, ValueError) as error:
                # Normalization refused the object. The object is as incomplete
                # as an unfetched one: it produces no event, so it must not be
                # recorded as successfully checked either.
                failure = error

            if failure is not None:
                detail = _failure_detail(
                    candidate, phase=phase, resolved_type=resolved_type, error=failure
                )
                skipped.append(detail)
                archive.note_skip(
                    "object_inaccessible" if phase == "retrieve" else "object_incomplete", **detail
                )
                failures_by_phase[phase] = failures_by_phase.get(phase, 0) + 1
                if detail["resolution"] == "unresolved":
                    unresolved_failures += 1
                    archive.note_error(
                        "object_unresolved",
                        object_id=object_id,
                        phase=phase,
                        retry_class=detail["retry_class"],
                        attempts=detail["attempts"],
                    )
                # The watermark must not step over an object whose state is
                # still unknown. A known edit time bounds it precisely; without
                # one, the only safe bound is not to move at all.
                edited = _parse_time((obj or {}).get("last_edited_time")) or _parse_time(
                    candidate.last_edited_time
                )
                if edited:
                    failed_edits.append(edited)
                elif detail["resolution"] == "unresolved":
                    hold_watermark = True
                for url in linked_by_id.get(object_id, []):
                    link_queue.mark(url, "failed", error=_link_failure_reason(failure, phase))
                continue

            assert obj is not None and resolved_type is not None  # the try above set both
            if bool(obj.get("archived") or obj.get("in_trash")):
                archived_observed += 1
            blocks_collected += len(blocks)
            comments_collected += len(comments)
            properties_collected += property_pages
            if event is not None:
                events.append(event)
            if resolved_type != "page":
                databases_collected += 1
            objects_completed += 1

            edited = _parse_time(obj.get("last_edited_time")) or _parse_time(candidate.last_edited_time)
            if edited:
                collected_edits.append(edited)
            known_objects[object_id] = {"type": resolved_type, "last_checked": now_iso}
            for url in linked_by_id.get(object_id, []):
                link_queue.mark(url, "fetched")
                link_fetched += 1

        if unresolved_failures:
            archive.note_coverage(UNRESOLVED_FAILURE_NOTE)
        if hold_watermark:
            archive.note_coverage(WATERMARK_HELD_NOTE)
        if any(item["resolution"] == "permanent" for item in skipped):
            archive.note_coverage(PERMANENT_SKIP_WATERMARK_NOTE)

        users_seen = self._collect_users()
        if self._comment_budget_exhausted:
            archive.note_truncation("comment_request_budget", limit=comment_request_budget)
        archive.note_rate_limit(getattr(self.client, "rate_limit_hits", 0))

        events.sort(key=lambda item: (item.occurred_at, item.event_id))
        known_objects = _cap_known_objects(known_objects)
        new_watermark = _advance_watermark(
            previous=checkpoint.get("last_edited_watermark"),
            collected=collected_edits,
            failed=failed_edits,
            hold=hold_watermark,
        )

        # "Complete" means every candidate this run knew about was read to the
        # end and nothing was bounded away. It is deliberately strict: a single
        # inaccessible child block is enough to make it false.
        coverage_complete = not (
            skipped
            or unresolved_urls
            or archive.skips
            or archive.errors
            or archive.truncated
            or not search_stats.get("complete", True)
        )

        counters = {
            "search": search_stats,
            "objects_discovered": len(candidates),
            "objects_attempted": len(ordered),
            "objects_rechecked": rechecked,
            "pages_collected": len(events),
            "databases_collected": databases_collected,
            "blocks_collected": blocks_collected,
            "comments_collected": comments_collected,
            "comment_requests_made": self._comment_requests_made,
            "comment_strategy": comment_strategy,
            # What the strategy bought and what it cost, as two counts rather
            # than one ratio: sweeps that ran, and blocks never asked about.
            "comment_block_sweeps": self._comment_block_sweeps,
            "comment_blocks_unswept": self._comment_blocks_unswept,
            "comment_request_budget": comment_request_budget,
            "comment_requests_remaining": (
                None
                if comment_request_budget is None
                else max(0, comment_request_budget - self._comment_requests_made)
            ),
            "comment_budget_exhausted": self._comment_budget_exhausted,
            "property_pages_collected": properties_collected,
            # The walk stops at page boundaries, so these two say what it chose
            # not to enter. `unlisted` is the one to watch: it counts child
            # objects no discovery pass had already named, which is what a
            # leaking /search would look like from inside a run.
            "child_object_refs": child_object_refs,
            "child_object_refs_unlisted": child_object_refs_unlisted,
            "data_source_query": data_source_stats,
            "seed_pages": seed_stats,
            "archived_observed": archived_observed,
            "users_seen": users_seen,
            "link_queue_fetched": link_fetched,
            "link_queue_unresolved": len(unresolved_urls),
            "link_queue_persisted": not link_queue.read_only,
            "api_call_counts": dict(getattr(self.client, "call_counts", {}) or {}),
            # Coverage, stated positively and negatively so neither reading
            # needs the other to be trusted.
            "objects_completed": objects_completed,
            "objects_failed": len(skipped),
            "objects_failed_unresolved": unresolved_failures,
            "objects_failed_permanent": len(skipped) - unresolved_failures,
            "objects_failed_by_phase": dict(sorted(failures_by_phase.items())),
            "watermark_held": hold_watermark,
            "coverage_complete": coverage_complete,
            # Transient-retry accounting from the client, alongside the
            # rate-limit accounting it already kept.
            "rate_limit_hits": int(getattr(self.client, "rate_limit_hits", 0) or 0),
            "transient_retries": int(getattr(self.client, "transient_retries", 0) or 0),
            "transient_retries_by_class": dict(getattr(self.client, "retry_counts", {}) or {}),
            "requests_exhausted": int(getattr(self.client, "exhausted_requests", 0) or 0),
        }

        # An unresolved object is worse than a skipped one: the API never said
        # what became of it, so the run cannot claim it looked everywhere.
        if unresolved_failures or not search_stats.get("complete", True):
            status = "degraded"
        elif skipped or unresolved_urls or archive.skips:
            status = "success_with_skips"
        else:
            status = "success"
        checkpoint_advanced = False
        if advance_checkpoint and not archive.truncated and not archive.dry_run:
            archive.write_checkpoint(
                {
                    "schema_version": CHECKPOINT_SCHEMA_VERSION,
                    "source": "notion",
                    "run_id": archive.run_id,
                    "updated_at": now_iso,
                    "since_effective": effective_since.isoformat(),
                    "last_edited_watermark": new_watermark,
                    "known_objects": known_objects,
                }
            )
            checkpoint_advanced = True
        elif archive.truncated:
            archive.note_coverage(
                "notion.checkpoint_withheld_truncated: the capture was bounded, so the "
                "checkpoint was left at its previous position."
            )

        manifest_path = archive.finish(
            {
                "status": status,
                "since": since.isoformat(),
                "since_effective": effective_since.isoformat(),
                "last_edited_watermark": new_watermark,
                "pages_discovered": len(candidates),
                "pages_collected": len(events),
                "pages_skipped": len(skipped),
                "skipped_pages": skipped,
                "objects_completed": objects_completed,
                "objects_failed_unresolved": unresolved_failures,
                "coverage_complete": coverage_complete,
                "watermark_held": hold_watermark,
                "unresolved_link_urls": [_url_fingerprint(url) for url in unresolved_urls],
                "events": len(events),
                "counters": counters,
            }
        )
        return NotionCollectionResult(
            run_id=archive.run_id,
            pages_discovered=len(candidates),
            pages_collected=len(events),
            pages_skipped=len(skipped),
            events=tuple(events),
            manifest_path=manifest_path,
            checkpoint_advanced=checkpoint_advanced,
            databases_collected=databases_collected,
            users_seen=users_seen,
            comments_collected=comments_collected,
            blocks_collected=blocks_collected,
            link_queue_fetched=link_fetched,
            link_queue_unresolved=len(unresolved_urls),
            archived_observed=archived_observed,
            counters=counters,
            status=status,
            objects_completed=objects_completed,
            objects_failed_unresolved=unresolved_failures,
            coverage_complete=coverage_complete,
        )


def _failure_detail(
    candidate: _Candidate,
    *,
    phase: str,
    resolved_type: str | None,
    error: BaseException,
) -> dict[str, Any]:
    """One object's failure, in the shape the manifest records it.

    `resolution` is the field that matters operationally: `permanent` means the
    API gave a final answer (gone, forbidden, malformed), `unresolved` means it
    never did and the object's real state is still unknown.

    Only a NotionApiError's message is recorded verbatim, because the client
    guarantees those carry no credential and no query string. Anything else --
    a normalizer's ValueError, say -- contributes its type name only, since its
    message could quote the page content this file must never write to disk.
    """
    api_error = error if isinstance(error, NotionApiError) else None
    return {
        "object_id": candidate.object_id,
        "object_type": resolved_type or candidate.object_type,
        "discovered_by": candidate.source,
        "phase": phase,
        "status": api_error.status if api_error else None,
        "code": api_error.code if api_error else None,
        "resolution": api_error.resolution if api_error else "permanent",
        "retry_class": api_error.retry_class if api_error else None,
        "attempts": api_error.attempts if api_error else None,
        "error_type": type(error).__name__,
        "error": str(error) if api_error else type(error).__name__,
    }


def _link_failure_reason(error: BaseException, phase: str) -> str:
    """Why a queued URL was marked failed, without leaking a request URL."""
    if isinstance(error, NotionApiError):
        if error.status is not None:
            return f"HTTP {error.status}: {error.code or 'unknown'} during {phase}"
        return (
            f"{error.resolution} {error.retry_class or 'error'} during {phase} "
            f"after {error.attempts or 0} attempts"
        )
    return f"{type(error).__name__} during {phase}"


def _url_fingerprint(url: str) -> str:
    """A stable, non-reversible reference to a URL for manifests.

    A Notion URL contains the page title, which is company content. The
    manifest records the page id (already an opaque identifier) and a hash so
    two runs can be compared without writing content into an operational file.
    """
    import hashlib

    page_id = canonical_notion_page_id(url) or "unresolved"
    return f"{page_id}:sha256:{hashlib.sha256(url.encode('utf-8')).hexdigest()[:16]}"


def _cap_known_objects(known: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    if len(known) <= MAX_TRACKED_OBJECTS:
        return dict(sorted(known.items()))
    newest = sorted(
        known.items(), key=lambda item: str(item[1].get("last_checked") or ""), reverse=True
    )[:MAX_TRACKED_OBJECTS]
    return dict(sorted(newest))


def _advance_watermark(
    *,
    previous: Any,
    collected: list[datetime],
    failed: list[datetime],
    hold: bool = False,
) -> str | None:
    """Advance only across the contiguous prefix of successfully fetched objects.

    If an object failed, the watermark stops just below the oldest failure, so
    the next run reads that window again instead of stepping over it.

    `hold` is the case a failure time cannot express: an object failed
    unresolved and carried no `last_edited_time`, so there is no position to
    stop below. The watermark then does not move at all. It applies only to
    unresolved failures -- an object the API answered definitively for (a
    deleted page found by re-check, say) carries no unknown window behind it,
    and holding for those would freeze the watermark permanently.
    """
    previous_time = _parse_time(previous)
    if hold:
        return previous_time.isoformat() if previous_time else None
    usable = list(collected)
    if failed:
        oldest_failure = min(failed)
        usable = [value for value in usable if value < oldest_failure]
        if previous_time and previous_time >= oldest_failure:
            return previous_time.isoformat()
    if not usable:
        return previous_time.isoformat() if previous_time else None
    candidate = max(usable)
    if previous_time and previous_time > candidate:
        return previous_time.isoformat()
    return candidate.isoformat()


def make_notion_collector(
    *,
    token: str,
    archive_root: Path,
    environment: str,
    capture_density: str = "full",
    dry_run: bool = False,
    # Where a run publishes its derived progress snapshot. None falls back to
    # APP_CONFIG_ROOT, and to no snapshot at all when that is unset.
    config_root: Path | None = None,
) -> tuple[RawArchive, NotionCollector]:
    run_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:12]}"
    archive = RawArchive(
        archive_root,
        "notion",
        run_id,
        environment,
        capture_profile=NOTION_CAPTURE_PROFILE,
        capture_density=capture_density,
        dry_run=dry_run,
        config_root=config_root,
    )
    queue = NotionLinkQueue(archive_root, environment, read_only=dry_run)
    return archive, NotionCollector(NotionClient(token), archive, queue)
