"""Append-only registry of the rules a collection run was captured under.

Why this exists
---------------
A manifest says *what* a run fetched. It does not say *what the run was
supposed to fetch*, and that changes over time: the legacy dumps and the
current official-API archive have different scopes, different densities and
different blind spots. Without a versioned rule, a coverage dashboard silently
compares two incomparable things.

The two guarantees this module makes:

  1. **Append-only.** A published version is frozen. Changing what ``V1``
     means is not an edit; it is a new version. ``_validate_registry`` refuses
     duplicate versions, and ``PUBLISHED_DIGESTS`` pins the content of every
     already-published version so an accidental edit fails loudly at import
     instead of quietly re-labelling historical runs.
  2. **Stable content digest.** ``CollectionRule.digest`` is a sha256 over the
     rule's canonical JSON. The same rule always hashes the same; any change
     to its meaning produces a different digest, and the digest is written
     into every manifest alongside the version string.

Everything here is a literal statement of fact investigated from the collector
code and the legacy files. Nothing is imported from the collectors at runtime:
a rule must keep describing what a *past* run did even after the collector
changes. ``tests/test_collection_rules.py`` asserts that the current
collectors have not grown a limitation this registry does not name -- that
test failing is the signal to append a new version.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from typing import Any, Mapping

RULE_REGISTRY_SCHEMA_VERSION = 2

# Where a version sits in the registry's lifecycle. Not part of any digest --
# see `CollectionRule.content` for why.
#
# `pending` exists because publishing a rule and running under it are two
# different days, and collapsing them forces a lie in one direction or the
# other. A repair to a collector cannot land while the active rule describes
# the behaviour being repaired -- the rule would keep asserting what the code
# no longer does -- but activating the new rule first makes every run in
# between stamp a rule it does not follow. A pending version is published,
# readable and digest-checkable, and names the coverage notes the repair will
# record, so the repair has something to land against; it stamps nothing, and
# it does not close its predecessor's window. Activating it is a status flip,
# which the digest deliberately does not cover.
#
# A pending version is always the **tip**: it supersedes the version in force
# and never sits beneath one. That ordering is the whole point rather than an
# accident of who committed first. The version describing what the collector
# does is the version being stamped -- a stamp that does not describe the run
# is worth less than no stamp, because it is believed -- and a change that has
# already landed never queues behind an unlanded one for nothing but a number.
# When both kinds are in flight, the landed one takes the next active number
# and the pending one is renumbered above it. That costs nothing recoverable:
# a pending version has stamped no manifest and, by the check in
# `_validate_registry`, has pinned no digest.
RULE_STATUSES = ("pending", "active", "superseded")

# Canonical ledger source names, as used by the ledger and the service DB.
#
# This list and the active rule move together, always in one commit. The
# invariant in `_validate_registry` is two-sided -- a rule may only name a
# source in this list, and the active rule must cover every source in it -- so
# widening one without the other leaves the registry invalid in between. That
# is deliberate: a source this list claims but no live rule describes would be
# a source the dashboard reports with no statement of what was collected.
SOURCES = ("slack", "notion", "google_calendar", "github", "slurm")

# Collector directory names, as used under raw/ and manifests/.
COLLECTOR_SOURCES = ("slack", "notion", "google-calendar", "github", "slurm")

SOURCE_TO_COLLECTOR = {
    "slack": "slack",
    "notion": "notion",
    "google_calendar": "google-calendar",
    "github": "github",
    "slurm": "slurm",
}
COLLECTOR_TO_SOURCE = {value: key for key, value in SOURCE_TO_COLLECTOR.items()}

SOURCE_LABELS = {
    "slack": "Slack",
    "notion": "Notion",
    "google_calendar": "Google Calendar",
    "github": "GitHub",
    "slurm": "Slurm",
}


class RuleRegistryError(RuntimeError):
    """The registry is not append-only, or a published rule was edited."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class SourceRule:
    """What one source was collected under, for one rule version."""

    source: str
    scope: str
    density: str
    # Machine-readable density class, so a dashboard can compare a day-sliced
    # dump against a continuously resumed capture without parsing prose.
    density_kind: str  # day_slice | incremental_continuous | unknown
    includes: tuple[str, ...] = ()
    excludes: tuple[str, ...] = ()
    known_limitations: tuple[str, ...] = ()
    # What proves the statements above: a path, a file, or a code location.
    evidence: tuple[str, ...] = ()
    # Facts this rule cannot assert. Named explicitly rather than implied.
    unknowns: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "label": SOURCE_LABELS.get(self.source, self.source),
            "scope": self.scope,
            "density": self.density,
            "density_kind": self.density_kind,
            "includes": list(self.includes),
            "excludes": list(self.excludes),
            "known_limitations": list(self.known_limitations),
            "evidence": list(self.evidence),
            "unknowns": list(self.unknowns),
        }


@dataclass(frozen=True)
class EffectivePeriod:
    """When a rule applied, and how that is known.

    `end` is always None in the registry, and `_validate_registry` enforces it.
    A version's end is only knowable when a successor appears, so writing it
    into the rule would mean editing published, digest-frozen content later --
    the same trap `status` was in before it left the digest. The end is derived
    from the successor's start instead, by `effective_window`.
    """

    start: str | None
    end: str | None
    basis: str

    def as_dict(self) -> dict[str, Any]:
        return {"start": self.start, "end": self.end, "basis": self.basis}


@dataclass(frozen=True)
class CollectionRule:
    version: str
    title: str
    status: str  # one of RULE_STATUSES: pending | active | superseded
    effective: EffectivePeriod
    summary: str
    # Schema versions a run under this rule writes. Literal, not imported: a
    # historical rule must keep describing the schema its runs actually used.
    manifest_schema_version: int | None
    ledger_schema_version: str | None
    source_schema_version: str | None
    capture_profiles: tuple[str, ...]
    sources: tuple[SourceRule, ...]
    storage_layout: tuple[str, ...] = ()
    unknowns: tuple[str, ...] = ()
    supersedes: str | None = None

    def content(self) -> dict[str, Any]:
        """Everything the digest covers. The digest itself is never inside.

        `status` is deliberately absent. It records where this version sits in
        the registry's lifecycle, not what a run under it collected. Including
        it meant that retiring a version -- which any registry with more than
        one version must do -- changed its digest and tripped the append-only
        check, making a required transition look like tampering. The frozen
        thing is what the rule says about collection; that is what is hashed.
        """
        return {
            "registry_schema_version": RULE_REGISTRY_SCHEMA_VERSION,
            "version": self.version,
            "title": self.title,
            "effective": self.effective.as_dict(),
            "summary": self.summary,
            "manifest_schema_version": self.manifest_schema_version,
            "ledger_schema_version": self.ledger_schema_version,
            "source_schema_version": self.source_schema_version,
            "capture_profiles": list(self.capture_profiles),
            "storage_layout": list(self.storage_layout),
            "unknowns": list(self.unknowns),
            "supersedes": self.supersedes,
            "sources": [rule.as_dict() for rule in self.sources],
        }

    @property
    def digest(self) -> str:
        return "sha256:" + hashlib.sha256(_canonical_json(self.content()).encode("utf-8")).hexdigest()

    def source_rule(self, source: str) -> SourceRule | None:
        for rule in self.sources:
            if rule.source == source:
                return rule
        return None

    def as_dict(self) -> dict[str, Any]:
        value = self.content()
        value["digest"] = self.digest
        return value


# --------------------------------------------------------------------- V0

V0 = CollectionRule(
    version="V0",
    title="레거시 일자별 덤프 (legacy daily_raw dump)",
    status="superseded",
    effective=EffectivePeriod(
        start=None,
        end=None,
        basis=(
            "unknown: the legacy dumps carry no record of when their collection rule "
            "began or ended. The observed date range is computed at query time from the "
            "legacy daily_raw date directories and is reported as an observation, never "
            "as the rule's declared period."
        ),
    ),
    summary=(
        "A per-day dump written by the previous macOS collector into "
        "<legacy>/<root>/daily_raw/<YYYY-MM-DD>/<source>/. One directory per KST date, "
        "no run identity, and a meta.json whose `status` is hardcoded and therefore is "
        "not evidence of completeness."
    ),
    manifest_schema_version=None,
    ledger_schema_version=None,
    source_schema_version="1.0",
    capture_profiles=(
        "legacy-slack-slim12/v1",
        "legacy-slack-thread-store/v1",
        "legacy-notion-page/v1",
        "legacy-notion-block/v1",
        "legacy-notion-comment-from-attribution/v1",
    ),
    storage_layout=(
        "<legacy_root>/{shared,personal}/daily_raw/<YYYY-MM-DD>/<source>/<container>/*.json",
        "<legacy_root>/hk_private/thread_store/ -- Slack threads with no date directory",
        "meta.json and all_users.json sit beside the payload files in each source directory",
        "2025-05-16..2026-03-31: Notion payloads were written straight into "
        "<date>/common/ with no notion/ directory (layout `legacy_no_source_dir`)",
    ),
    unknowns=(
        "A missing date directory does not distinguish 'the collector did not run' from "
        "'the dump was lost or never transferred'. Such a date is reported 미수집, never "
        "inferred as collected.",
        "There is no run id, no start/finish timestamp pair, and no per-run manifest, so "
        "two runs on one date are indistinguishable from one.",
        "meta.json `status` is hardcoded to \"ok\" on every date, including dates with "
        "recorded truncation warnings and rate-limit loss, so it is ignored as a "
        "completeness signal.",
        "The exact API endpoints, scopes and token identity of the legacy collector are "
        "not recorded anywhere in the dump.",
        "Files marked DATALESS or carried as .icloud placeholders in the migration "
        "inventory were never downloaded; their content is absent even where the name is "
        "present.",
        ".rsync-partial paths are abandoned transfers; some of them parse successfully and "
        "are excluded by path rather than by parse error.",
    ),
    sources=(
        SourceRule(
            source="slack",
            scope=(
                "Messages for one KST date, written per container under "
                "<date>/slack/{common,dm,private}/, plus per-person attribution buckets."
            ),
            density="한 날짜 = 한 슬라이스 (day slice)",
            density_kind="day_slice",
            includes=(
                "Raw message stores under common/, dm/ and private/",
                "all_users.json workspace user list per date",
                "meta.json with collection_time, target_date, date_range, api_calls, "
                "rate_limit_hits and truncation_warnings",
                "reactions_supplement.json as a separate file, never merged into messages",
                "search-supplemented messages carrying `_supplemented`, mixed into the same "
                "file as conversations.history records",
                "hk_private/thread_store/ thread dumps, which carry no date",
            ),
            excludes=(
                "attribution/ and personal/ buckets duplicate the same message once per "
                "person and mix observation with inference; they are not raw stores",
                "File bodies: only the message JSON was kept",
            ),
            known_limitations=(
                "legacy_slack.deletions_never_tracked: the dump has no tombstone feed, so "
                "the absence of a message is not evidence it still exists.",
                "legacy_slack.workspace_id_may_be_unknown: some files carry no workspace "
                "identity, so identity falls back to a resolver and can be unknown.",
                "legacy_slack.updated_at_may_be_unknown: edit timestamps were not always "
                "recorded.",
                "legacy_slack.private_content_routed_under_shared: slim_message never "
                "carried is_private at message level, so private containers appear under "
                "the shared/ root on some dates (a recorded routing anomaly).",
                "legacy_slack.rate_limit_loss_recorded_only_as_warnings: truncation is "
                "listed in meta.json truncation_warnings with no count of what was lost.",
            ),
            evidence=(
                "src/rlwrld_worklog/ledger/legacy_slack.py (RAW_CONTAINERS, "
                "EXCLUDED_CONTAINERS, EXCLUDED_FILENAMES, _routing_anomaly, _supplement)",
                "src/rlwrld_worklog/ledger/common.py (classify_path, MetaSignalResolver)",
                "<legacy_root>/shared/daily_raw/<date>/slack/meta.json",
            ),
            unknowns=(
                "Which channels the legacy token could see on any given date.",
                "Whether a channel absent from a date was empty, unreadable or skipped.",
            ),
        ),
        SourceRule(
            source="notion",
            scope=(
                "Pages, blocks and attribution-derived comments for one KST date under "
                "<date>/notion/, and for 2025-05-16..2026-03-31 directly under <date>/common/."
            ),
            density="한 날짜 = 한 슬라이스 (day slice)",
            density_kind="day_slice",
            includes=(
                "Page objects with properties and, where walked, block text",
                "meta.json with api_calls, common_sources, workspace_users and "
                "personal_search counters",
                "all_users.json workspace user list per date",
            ),
            excludes=(
                "attribution/ buckets, which duplicate a page once per person",
                "File bodies referenced by file properties and file blocks",
            ),
            known_limitations=(
                "legacy_notion.property_values_reduced_to_type_labels: unsupported "
                "property types were stored as a <type_label> string instead of a value.",
                "legacy_notion.block_walk_was_partial: only some pages were block-walked "
                "(meta.json records weekly_pages_block_walked), so absent block text is "
                "not evidence of an empty page.",
                "legacy_notion.comments_derived_from_attribution: comments were "
                "reconstructed from attribution buckets, not fetched per block.",
                "legacy_notion.layout_changed_mid_history: the notion/ directory did not "
                "exist before 2026-04-01, so a date-level reader must handle both layouts.",
            ),
            evidence=(
                "src/rlwrld_worklog/ledger/legacy_notion.py",
                "src/rlwrld_worklog/ledger/common.py (classify_path layout "
                "`legacy_no_source_dir`, count_type_labels)",
                "<legacy_root>/shared/daily_raw/<date>/notion/meta.json",
            ),
            unknowns=(
                "Which pages the legacy integration was shared into on any given date.",
                "Whether a page absent from a date was unchanged, unshared or missed.",
            ),
        ),
        SourceRule(
            source="google_calendar",
            scope="One events file per KST date at <date>/gcal/common/all_events.json.",
            density="한 날짜 = 한 슬라이스 (day slice)",
            density_kind="day_slice",
            includes=(
                "Event objects as returned by the legacy collector",
                "meta.json with total_events, calendars_scanned and per-member stats",
            ),
            excludes=("Attachment bodies", "Per-calendar sync state"),
            known_limitations=(
                "legacy_gcal.no_per_calendar_state: the dump records calendars_scanned as "
                "a count only, so which calendars were read on a date is not recorded.",
                "legacy_gcal.cancellations_not_distinguished: the day file is a snapshot, "
                "so a cancelled event is absent rather than present with status=cancelled.",
                "legacy_gcal.calendar_id_may_be_absent: some events carry no calendar_id "
                "and fall back to organizer or creator email.",
            ),
            evidence=(
                "src/rlwrld_worklog/legacy_import.py (iter_legacy_calendar)",
                "<legacy_root>/shared/daily_raw/<date>/gcal/meta.json",
            ),
            unknowns=("The set of calendars in scope on any given date.",),
        ),
    ),
)


# --------------------------------------------------------------------- V1

V1 = CollectionRule(
    version="V1",
    title="공식 API 기반 원본 원장 (official-API immutable raw ledger)",
    status="superseded",
    effective=EffectivePeriod(
        start="2026-08-25",
        end=None,
        basis=(
            "observed: the first run manifest written by this repository's collectors "
            "(manifests/slack/test/20260825T105742Z-66cc09832a.json). Still active; no "
            "successor rule is published."
        ),
    ),
    summary=(
        "A daily incremental capture over the official read-only APIs into an immutable "
        "raw archive. Every run gets its own raw directory, an append-only page file per "
        "API response, one run manifest, and a per-source checkpoint that advances only "
        "over what the run actually finished. Density is continuous and resumable rather "
        "than day-sliced: a run covers the window between its checkpoint and its finish, "
        "not a calendar date."
    ),
    manifest_schema_version=2,
    ledger_schema_version="1.0",
    source_schema_version=None,
    capture_profiles=(
        "live-slack-web-api/v1",
        "live-notion-api/v1",
        "live-google-calendar-api/v1",
    ),
    storage_layout=(
        "<archive_root>/raw/<collector_source>/<environment>/<YYYY>/<MM>/<DD>/<run_id>/"
        "<seq>-<kind>-<digest>.json.gz -- append-only, never overwritten",
        "<archive_root>/manifests/<collector_source>/<environment>/<run_id>.json -- one "
        "run manifest; a repeat finish writes <run_id>.<n>.json rather than replacing it",
        "<archive_root>/manifests/<collector_source>/<environment>/checkpoint.json plus "
        "checkpoints/<previous>.json history",
        "<ledger_root>/ledger/<ledger_source>/live-<run_id>.jsonl -- standard v1 ledger "
        "projection of one run",
    ),
    unknowns=(
        "A manifest records the run's own view of its coverage. It cannot assert what the "
        "upstream API chose not to return.",
        "A run that crashed before finish() writes no manifest. Such a run is visible only "
        "as a raw directory with no manifest and is reported running or stale, never "
        "success.",
    ),
    sources=(
        SourceRule(
            source="slack",
            scope=(
                "Every conversation the authenticated user token can see -- public, "
                "private, MPIM and DM -- listed with exclude_archived=false, with "
                "conversations.history resumed per channel from the checkpoint's own high "
                "watermark, plus a bounded re-poll of watched threads and the workspace-wide "
                "search.messages mention queries."
            ),
            density=(
                "연속 증분 (incremental continuous): 채널별 워터마크부터 실행 시각까지. "
                "기본 요청 창은 26시간이며 체크포인트가 있으면 그 위치가 우선한다."
            ),
            density_kind="incremental_continuous",
            includes=(
                "auth.test identity page",
                "conversations.list for every conversation type",
                "conversations.history per channel from the per-channel watermark",
                "conversations.replies for watched threads carried in the checkpoint",
                "search.messages for direct mentions, DMs to self, self-authored messages, "
                "<!channel>/<!here>/<!everyone> broadcasts and every usergroup the user is in",
                "File metadata and links only: id, name, title, mimetype, filetype, size, "
                "permalink, url_private",
            ),
            excludes=(
                "File bodies: no binary is ever fetched",
                "Any conversation the token cannot see; skips are recorded per channel with "
                "the Slack error code",
                "search.messages entirely, when a run is channel- or message-bounded "
                "(smoke/dry-run)",
            ),
            known_limitations=(
                "slack.message_deletion_not_exposed: the Web API has no deleted-message "
                "feed; conversations.history simply stops returning a deleted message. "
                "Tombstones are preserved only where Slack exposes them (subtype=tombstone).",
                "slack.thread_replies_need_supplements: conversations.history omits thread "
                "replies, so replies to older threads are covered by the bounded "
                "watched-thread re-poll and by search.messages, not by history alone.",
                "slack.search_index_lag: search.messages is an index and can lag the live "
                "channel, so a same-minute mention may first appear on the following run.",
                "slack.search_skipped_for_bounded_capture: the workspace-wide mention "
                "searches are skipped when a capture is channel- or message-bounded.",
                "slack.checkpoint_withheld_truncated: a bounded or truncated run leaves the "
                "checkpoint at its previous position, so the next run repeats the window.",
            ),
            evidence=(
                "src/rlwrld_worklog/slack_collector.py (module docstring, COVERAGE_NOTES, "
                "FILE_LINK_FIELDS, SKIPPABLE_CONVERSATION_ERRORS)",
                "manifest fields: api_coverage, coverage_notes, skips, truncation, "
                "rate_limit_hits, checkpoint_in/checkpoint_out",
            ),
            unknowns=(
                "Whether a channel skipped with channel_not_found was deleted or merely "
                "invisible to this token.",
            ),
        ),
        SourceRule(
            source="notion",
            scope=(
                "Every object the integration can see through /search ordered by "
                "last_edited_time, plus the Notion link queue fed by Slack and Calendar, "
                "plus a bounded re-check of objects this collector has already seen. Each "
                "object is walked recursively for blocks, per-block comments and paginated "
                "title/rich-text/relation properties."
            ),
            density=(
                "연속 증분 (incremental continuous): last_edited_time 워터마크 기준. "
                "객체 하나가 완전성의 단위이며, 완료된 객체의 연속 접두사까지만 워터마크가 전진한다."
            ),
            density_kind="incremental_continuous",
            includes=(
                "/search over pages, databases and data sources with no object filter",
                "Recursive block walk including child blocks of meeting-notes blocks",
                "/comments per block of every fetched object",
                "Paginated title, rich-text and relation properties",
                "Notion URLs discovered by the Slack and Calendar captures in the same run",
                "A bounded re-check of known objects, which is the only way an archive, a "
                "trash or a lost share becomes observable",
            ),
            excludes=(
                "File bodies: file blocks and file properties are kept as JSON with their "
                "expiring URLs, and nothing is downloaded",
                "Objects never shared with the integration",
                "Archived and trashed objects, which /search omits",
            ),
            known_limitations=(
                "notion.search_is_not_a_change_feed: /search omits archived and trashed "
                "objects and anything not shared with the integration, and can lag an edit.",
                "notion.database_rows_come_from_search: data-source rows are pages and are "
                "captured through /search rather than by querying every data source daily.",
                "notion.attachments_are_metadata_only: no file body is downloaded.",
                "notion.comments_are_per_block: /comments is queried for every block of "
                "every fetched object; a full-density run is exhaustive and the API "
                "client's rate-limit handling is the bound.",
                "notion.comment_requests_capped: an explicit comment-request budget makes "
                "the per-block sweep non-exhaustive, marks the run truncated and withholds "
                "the checkpoint.",
                "notion.link_queue_not_persisted_in_dry_run: a dry or smoke run reads the "
                "link queue and writes nothing back to it.",
                "notion.object_is_the_unit_of_completeness: one unfinished object is "
                "recorded as a failed object and skipped alone; the run continues and the "
                "raw pages already written for it are kept.",
                "notion.unresolved_objects_present: an object the API never gave a final "
                "answer for leaves the run degraded and the checkpoint held below it.",
                "notion.watermark_held_for_unknown_position: an unresolved object with no "
                "last_edited_time holds the watermark where it was.",
                "notion.permanent_skips_do_not_hold_the_watermark: an object answered "
                "definitively (400/401/403/404) with no last_edited_time does not hold it.",
                "notion.search_walk_incomplete: a /search walk that stopped on an error "
                "marks the run truncated and withholds the checkpoint.",
                "notion.users_not_collected: when the client exposes no /users iterator, "
                "the workspace user list is not captured for that run.",
                "notion.checkpoint_withheld_truncated: a bounded capture leaves the "
                "checkpoint at its previous position.",
            ),
            evidence=(
                "src/rlwrld_worklog/notion_collector.py (module docstring, COVERAGE_NOTES "
                "and the named *_NOTE constants)",
                "src/rlwrld_worklog/link_queue.py",
                "manifest fields: coverage_notes, skips, errors, truncation, "
                "objects_failed_unresolved, checkpoint_in/checkpoint_out",
            ),
            unknowns=(
                "Whether an object absent from /search is unchanged, unshared, archived or "
                "merely lagging the index; only the bounded re-check can distinguish these.",
            ),
        ),
        SourceRule(
            source="google_calendar",
            scope=(
                "calendarList.list walked in full on every run with showDeleted and "
                "showHidden, and events per calendar through one nextSyncToken each with "
                "showDeleted=true and singleEvents=false."
            ),
            density=(
                "연속 증분 (incremental continuous): 캘린더별 syncToken 기준. "
                "토큰이 없거나 만료(410)된 캘린더만 timeMin 이후로 전체 재동기화한다."
            ),
            density_kind="incremental_continuous",
            includes=(
                "Calendar metadata: summary, timezone, accessRole, primary, deleted",
                "Recurrence masters, recurringEventId and originalStartTime",
                "Attendee response states, organizers, conference data and reminders",
                "updated, etag and status verbatim, so cancellations arrive as "
                "status=cancelled rows",
                "Attachment metadata: fileId, fileUrl and title",
            ),
            excludes=(
                "Attachment bodies: no file is downloaded",
                "Events older than timeMin on a first sync or after a 410 resync",
            ),
            known_limitations=(
                "google_calendar.first_sync_is_window_bounded: a calendar with no sync "
                "token, and a calendar recovering from an expired token, is read from "
                "timeMin forward; older events keep their previous raw observation.",
                "google_calendar.deletions_arrive_as_cancelled: a removed event returns "
                "with status=cancelled; a calendar removed from calendarList is preserved "
                "with deleted=true instead of being dropped.",
                "google_calendar.attachments_are_metadata_only: no file body is downloaded.",
                "google_calendar.calendar_requested_but_not_listed: an explicitly requested "
                "calendar missing from calendarList is still queried directly.",
                "google_calendar.no_sync_token_returned: a full listing that returns no "
                "nextSyncToken makes the next run repeat a window-bounded listing.",
            ),
            evidence=(
                "src/rlwrld_worklog/calendar_collector.py (module docstring, COVERAGE_NOTES)",
                "manifest fields: api_coverage, coverage_notes, skipped_calendars, "
                "reset_calendars, checkpoint_in/checkpoint_out",
            ),
            unknowns=(
                "Whether an event older than timeMin still exists upstream; the archive "
                "keeps its last observation and does not re-observe it.",
            ),
        ),
    ),
    supersedes="V0",
)


# --------------------------------------------------------------------- V2

V2 = CollectionRule(
    version="V2",
    title="공식 API 원본 원장 + 노션 날짜 슬라이스 (date-sliced Notion capture)",
    status="superseded",
    effective=EffectivePeriod(
        start="2026-09-02",
        end=None,
        basis=(
            "observed: the first date-sliced Notion run "
            "(manifests/notion/production/20260902T131021Z-4f3d2288913a.json, capture_density "
            "date-slice). Still active; no successor rule is published."
        ),
    ),
    summary=(
        "V1 with one addition: Notion can be captured one bounded last_edited_time window "
        "at a time instead of only from a checkpoint watermark forward. Slack and Google "
        "Calendar are unchanged from V1. A bounded slice is a historical observation, not "
        "the live head, so it does not advance the checkpoint and does not consult the "
        "Notion link queue or the known-object re-check."
    ),
    manifest_schema_version=2,
    ledger_schema_version="1.0",
    source_schema_version=None,
    capture_profiles=(
        "live-slack-web-api/v1",
        "live-notion-api/v1",
        "live-google-calendar-api/v1",
    ),
    storage_layout=V1.storage_layout,
    unknowns=V1.unknowns
    + (
        "A date slice observes one window. It says nothing about whether anything edited "
        "before that window was ever captured, which is why it may not move the watermark.",
    ),
    sources=(
        V1.source_rule("slack"),
        replace(
            V1.source_rule("notion"),
            density=(
                "연속 증분 또는 날짜 슬라이스 (incremental continuous or date slice). "
                "슬라이스 모드에서는 last_edited_time 창 하나가 한 실행의 범위이며, "
                "그 창을 벗어난 객체는 수집하지 않는다."
            ),
            density_kind="incremental_or_date_slice",
            includes=V1.source_rule("notion").includes
            + (
                "A bounded last_edited_time window (`until`), so one run covers one KST day "
                "and terminates by construction rather than after the whole workspace",
            ),
            excludes=V1.source_rule("notion").excludes
            + (
                "In slice mode: the link queue and the known-object re-check, which target "
                "the live head and would make the slice unbounded again",
            ),
            known_limitations=V1.source_rule("notion").known_limitations
            + (
                "notion.date_slice_capture: a bounded window run captured one "
                "last_edited_time slice instead of the live head. The link queue and the "
                "known-object re-check were not consulted and the checkpoint was not "
                "advanced: a historical slice proves nothing about everything edited "
                "before its end.",
            ),
            evidence=V1.source_rule("notion").evidence
            + (
                "src/rlwrld_worklog/notion_collector.py (`collect(until=…)`, `_search` "
                "upper bound, DATE_SLICE_NOTE)",
                "manifest fields: requested_window.until, requested_window.mode, "
                "counters.search.skipped_above_window",
            ),
        ),
        V1.source_rule("google_calendar"),
    ),
    supersedes="V1",
)


V3 = CollectionRule(
    version="V3",
    title="공식 API 원본 원장 + 스레드 답글 전수 (thread replies swept)",
    status="superseded",
    effective=EffectivePeriod(
        start="2026-09-02",
        end=None,
        # States only the observation that dates the start. No claim about
        # being current: V1 and V2 both carry a frozen "Still active; no
        # successor rule is published" that stopped being true the moment they
        # were superseded, and the digest guard makes that text uncorrectable.
        # Whether a version is current is derived from the registry instead.
        basis=(
            "observed: commit 3b301b6, which made the Slack sweep reach "
            "conversations.replies for every thread parent."
        ),
    ),
    summary=(
        "V2 with one correction: Slack thread replies are actually collected. Slack stamps a "
        "thread parent with a `thread_ts` equal to its own `ts`, so the guard meant to skip "
        "replies skipped every parent too, and the replies were never requested. Notion and "
        "Google Calendar are unchanged from V2. This is a boundary in what the archive holds, "
        "not only in what the registry says: a Slack window captured under V0, V1 or V2 is "
        "missing its thread replies and has to be re-run to gain them."
    ),
    manifest_schema_version=2,
    ledger_schema_version="1.0",
    source_schema_version=None,
    capture_profiles=V2.capture_profiles,
    storage_layout=V2.storage_layout,
    unknowns=V2.unknowns
    + (
        "How many replies a pre-V3 Slack window is missing is knowable only by re-running it: "
        "the parents were archived with their declared reply_count, so the shortfall is "
        "measurable after the fact, but no manifest written before V3 records it.",
    ),
    sources=(
        replace(
            V2.source_rule("slack"),
            includes=V2.source_rule("slack").includes
            + (
                "Thread replies for every parent the sweep sights, through "
                "conversations.replies; a parent is no longer mistaken for a reply because "
                "Slack stamps it with its own thread_ts",
            ),
            known_limitations=V2.source_rule("slack").known_limitations
            + (
                "slack.thread_replies_incomplete: the parent sweep sighted more declared "
                "replies than it archived. Replies posted before the requested window, and "
                "replies lost to a rate limit or a truncated run, account for the difference. "
                "The note carries the counts, so the shortfall is measurable rather than "
                "invisible, and re-running the window closes it.",
            ),
            evidence=V2.source_rule("slack").evidence
            + (
                "src/rlwrld_worklog/slack_collector.py (the conversations.replies sweep and "
                "its declared/archived reply counters)",
                "commit 3b301b6",
            ),
        ),
        V2.source_rule("notion"),
        V2.source_rule("google_calendar"),
    ),
    supersedes="V2",
)


GITHUB_V4 = SourceRule(
    source="github",
    scope=(
        "조직의 저장소 전체 (every repository the token can list for the org). "
        "커밋은 로컬 베어 미러에서, PR·리뷰·댓글·이슈는 REST 로 받는다. 경로가 둘이다."
    ),
    density=(
        "전밀도 (full). 창 안의 모든 저장소를 시도하며, 받지 못한 저장소는 "
        "'커밋 없는 날'이 아니라 skip 으로 기록한다."
    ),
    density_kind="full",
    includes=(
        "Commits from bare mirrors via `git log --all`, so the window is not bounded by "
        "API retention and no request budget is spent on them",
        "Merge commits, with parent_count",
        "Archived repositories, for the activity they held during the window",
        "Pull requests, reviews, review comments and issues, over the REST API",
        "Commit file statistics, computed from the mirror's own diff",
    ),
    excludes=(
        "Blobs, file bodies and source trees. Private repository content is metadata only",
        "Slurm. It has no collector yet and gets its own rule version when it does",
    ),
    known_limitations=(
        "github.commits_come_from_local_mirrors: commits are read from bare mirrors, not "
        "the commits API. A repository with no mirror is a skip, never a day with no commits.",
        "github.commit_coverage_is_bounded_by_mirror_freshness: a mirror last fetched before "
        "the window closed cannot hold every commit pushed inside it. Each repository's last "
        "fetch time is recorded and a stale mirror is reported as a skip rather than as a "
        "repository with fewer commits.",
        "github.merge_commits_are_kept: merge commits are captured with parent_count, unlike "
        "the legacy collector's --no-merges, so a merged pull request is visible on the "
        "commit side too.",
        "github.archived_repositories_are_captured: archived state is a field, not a filter.",
        "github.pull_requests_have_no_since_parameter: the pulls endpoint cannot be filtered "
        "by time, so it is paginated newest-updated-first and stopped at the window edge. A "
        "pull request whose last update predates the window is not re-observed even if it "
        "was open.",
        "github.review_bodies_follow_their_pull_request: reviews are fetched per pull request "
        "found in the window. Submitting a review updates the pull request, so a review on an "
        "untouched pull request cannot occur.",
        "github.private_repository_content_is_metadata_only: no blob, no file body and no "
        "source tree is fetched.",
        "github.checkpoint_held_back_on_truncation: a truncated run leaves the watermark "
        "where it was, so the next run repeats the window rather than stepping over it.",
        "github.reviews_require_pull_requests: a run that asks for reviews without pull "
        "requests knows of no pull request to fetch reviews for, and collects none.",
        # The two below are not collector notes but observed facts about the
        # mirror set, which is the only evidence for part of this source.
        "17 repositories exist only as a mirror: the API does not list them, so a deletion, "
        "a rename or a transfer cannot be told apart, and the mirror is the sole evidence "
        "that their history existed (observed 2026-09-02, boa).",
        "The mirror set and the org listing disagree in both directions: 6 repositories the "
        "API lists have no mirror, and 14 August commits were missing from the backfill for "
        "that reason (observed 2026-09-02, boa).",
    ),
    evidence=(
        "src/rlwrld_worklog/github_client.py, src/rlwrld_worklog/github_collector.py",
        "manifests/github/production/20260902T140337Z-0aa0aa98d8ce.json "
        "(capture_profile live-github-api/v1, capture_density full)",
        "manifest counters: repositories_listed, repositories_attempted, "
        "repositories_skipped, repositories_with_stale_mirror, stale_mirrors, per_repository",
        "boa's survey of the mirror set, reported 2026-09-02 in msg_d54a8aca03d0d996a4",
    ),
    unknowns=(
        "Whether a repository present only as a mirror was deleted, renamed or transferred. "
        "The API answers none of the three.",
        "What a stale mirror missed. The skip says the window is not covered; it cannot say "
        "how many commits are behind it.",
    ),
)


V4 = CollectionRule(
    version="V4",
    title="공식 API 원본 원장 + 깃헙 (GitHub added)",
    status="superseded",
    effective=EffectivePeriod(
        start="2026-09-02",
        end=None,
        basis=(
            "observed: the first GitHub run "
            "(manifests/github/production/20260902T140337Z-0aa0aa98d8ce.json, capture_profile "
            "live-github-api/v1)."
        ),
    ),
    summary=(
        "V3 with GitHub added as a collected source. Slack, Notion and Google Calendar are "
        "unchanged from V3. GitHub is captured over two paths: commits from local bare "
        "mirrors and everything else over the REST API, which is why the mirror set's "
        "agreement with the org listing is itself a coverage question."
    ),
    manifest_schema_version=2,
    ledger_schema_version="1.0",
    source_schema_version=None,
    capture_profiles=V3.capture_profiles + ("live-github-api/v1",),
    storage_layout=V3.storage_layout,
    unknowns=V3.unknowns
    + (
        "Whether the GitHub mirror set is complete. It is the sole evidence for 17 "
        "repositories the API no longer lists, and it is missing 6 the API does list.",
    ),
    sources=(
        V3.source_rule("slack"),
        V3.source_rule("notion"),
        V3.source_rule("google_calendar"),
        GITHUB_V4,
    ),
    supersedes="V3",
)


SLURM_V5 = SourceRule(
    source="slurm",
    scope=(
        "세 클러스터의 종료된 잡 전체 (every finished job on kakao, aws and naver). "
        "sacct 117컬럼 원본을 그대로 보관하며, 파생 뷰는 쓰지 않는다."
    ),
    density=(
        "전밀도, 종료일 기준 (full, keyed on the KST day a job ended). "
        "실행 중인 잡은 담지 않고 종료된 뒤의 실행에서 한 번 담는다."
    ),
    density_kind="full",
    includes=(
        "All 117 sacct columns as a header plus rows, with no field dropped and no "
        "derived value computed",
        "`.batch` and `.extern` step rows, under their parent job's end date, because "
        "they carry the only real resource usage",
        "Any finished state, including one never seen before: the finished test is a "
        "blacklist of not-finished states, not a whitelist",
    ),
    excludes=(
        "Jobs still running at capture time. They are archived in a later run, once ended",
        "The derived `data/jobs_v2/` view. It is cheaper to fetch and it loses jobs -- see "
        "the limitation below",
        "Any efficiency figure or job classification. Those are the consumer's to compute",
    ),
    known_limitations=(
        "slurm.api_retention_floor: the dump begins 2025-07-08. Days in a requested window "
        "before that are outside what the API can answer, not days without work, and the "
        "legacy hk_private/slurm_archive is their only evidence. Roughly 18 months sit "
        "below the floor.",
        "slurm.day_key_is_end_not_submit: a job is filed under the KST day it ended. The "
        "naver cluster (mlxp) reports an empty Submit on all 17,220 of its jobs, so keying "
        "on Submit would drop that cluster entirely.",
        "slurm.finished_without_end_timestamp: some jobs report a finished state with no End "
        "value, so there is no day to file them under. They are counted rather than dropped "
        "silently, which is what the legacy collector did.",
        "slurm.finished_state_is_a_blacklist: any state outside the not-finished list counts "
        "as finished, including one never seen before. A whitelist previously discarded "
        "6,836 SUCCEEDED jobs without saying so.",
        "slurm.unrecognised_finished_states_were_kept: a state outside the reference list "
        "was treated as finished and archived, rather than repeating the whitelist mistake.",
        "slurm.running_jobs_are_not_captured: only finished jobs are archived, once, on the "
        "day they ended.",
        "slurm.step_rows_follow_their_parent: `.batch` and `.extern` rows have no ledger "
        "entity type yet, so they are raw-only.",
        "slurm.all_117_columns_preserved: the export is archived whole. Nothing is projected "
        "away at capture time, because a projection cannot be undone later.",
        "slurm.checkpoint_held_back_on_partial_run: if at least one cloud did not answer, the "
        "watermark stays where it was and the next run repeats the window.",
        # Observed facts about the API, not collector notes. They are the reason
        # the expensive path is the only correct one.
        "The derived view loses jobs. Measured on naver for KST 2026-08-16: the 117-column "
        "raw held 232 jobs and `jobs_v2` held 180, so 52 were raw-only -- 43 with an empty "
        "state, 7 SUCCEEDED, 2 FAILED, and 51 of the 52 were GPU jobs (observed 2026-09-02, "
        "boa).",
        "The derived view's `date=` partition is not an end date either: `date=2026-08-16` "
        "contained 11 jobs that ended on 8-17 and one that ended on 8-31, so it cannot serve "
        "as a day key (observed 2026-09-02, boa).",
        "The API offers neither a time window nor pagination. A 191MB dump (3.4GB "
        "uncompressed) is fetched whole and sliced on the client.",
        "The download is a 302 to a presigned S3 URL whose signature lasts 900 seconds. The "
        "URL is never cached and never written to a log or a manifest, because it is a "
        "credential.",
    ),
    evidence=(
        "src/rlwrld_worklog/slurm_collector.py, src/rlwrld_worklog/slurm_client.py",
        "capture_profile live-slurm-sacct-dump/v1, clouds kakao / aws / naver",
        "tests/test_slurm_collector.py (26), tests/test_slurm_client.py (8)",
        "boa's infra-node dry run, naver 2026-08-16: 232 jobs, 0 duplicate JobIDs "
        "(reported 2026-09-02 in msg_7148cd5be0d5d63e44)",
    ),
    unknowns=(
        "What sits below the retention floor. The API cannot answer for it and the legacy "
        "archive has not been reconciled against this collector yet.",
        "How many jobs across all three clouds finish with no End value. It is counted per "
        "run, not known in advance.",
    ),
)


# V4 recorded the mirror/API divergence as observed facts, because that is all
# it was. The collector now acts on it: it identifies the two disagreeing sets,
# names them per repository, and falls back to the commits API where a mirror is
# missing. That is a change in what is collected, so V5 restates github rather
# than reusing V4's rule.
GITHUB_V5 = replace(
    GITHUB_V4,
    includes=GITHUB_V4.includes
    + (
        "Commits over the REST commits API for a repository with no mirror, carrying the "
        "REST capture profile and no local diff statistics",
        "Per-repository lists of the two disagreeing sets, in "
        "counters.repositories_mirror_only and counters.repositories_api_only",
    ),
    known_limitations=tuple(
        limitation
        for limitation in GITHUB_V4.known_limitations
        # The two observed-fact entries are superseded by the collector notes
        # below, which say the same thing and are emitted per run.
        if not limitation.startswith(("17 repositories exist only", "The mirror set and the org"))
    )
    + (
        "github.repos_absent_from_api_mirror_is_sole_evidence: a repository that has a mirror "
        "but is no longer listed by the API was deleted, renamed or transferred, and the three "
        "cannot be told apart. Its mirror is the only remaining evidence of its history and "
        "must not be tidied away. Named in counters.repositories_mirror_only; 17 were observed "
        "on 2026-09-02.",
        "github.repos_absent_from_mirror_lose_commits: a repository the API lists with no "
        "mirror has no local history, so its commits are read over REST instead. If neither "
        "is available the run reports it rather than showing a repository with no commits. "
        "Named in counters.repositories_api_only; 6 were observed on 2026-09-02, and 14 "
        "August commits were missing from the first backfill for that reason.",
        "github.commits_read_over_rest_for_unmirrored_repository: those records carry the REST "
        "capture profile and no local diff statistics, so commit file statistics are absent "
        "for them rather than zero.",
        "github.mirror_api_divergence_not_computed: a run that saw only one of the two "
        "listings cannot identify either disagreeing set. An empty divergence there means "
        "unknown, not none.",
    ),
    evidence=GITHUB_V4.evidence
    + (
        "manifest counters: repositories_mirror_only, repositories_api_only, "
        "divergence_computed",
    ),
)


V5 = CollectionRule(
    version="V5",
    title="공식 API 원본 원장 + 슬럼 (Slurm added)",
    status="superseded",
    effective=EffectivePeriod(
        start="2026-09-02",
        end=None,
        basis=(
            "observed: src/rlwrld_worklog/slurm_collector.py and slurm_client.py exist and "
            "were exercised against infra-node (naver 2026-08-16, 232 jobs). Published from "
            "the collector code rather than from a manifest, because the rule has to be "
            "right before the first run stamps it."
        ),
    ),
    summary=(
        "V4 with Slurm added as a collected source. Slack, Notion, Google Calendar and "
        "GitHub are unchanged from V4. Slurm is archived as the whole 117-column sacct "
        "export, keyed on the KST day a job ended, because the cheap derived view loses "
        "jobs and its date partition is not an end date. The API has a retention floor at "
        "2025-07-08, below which the legacy archive is the only evidence."
    ),
    manifest_schema_version=2,
    ledger_schema_version="1.0",
    source_schema_version=None,
    capture_profiles=V4.capture_profiles + ("live-slurm-sacct-dump/v1",),
    storage_layout=V4.storage_layout,
    unknowns=V4.unknowns
    + (
        "Slurm before 2025-07-08. The API's dump does not reach it, so for roughly 18 "
        "months the legacy archive is the only record and this rule cannot speak for it.",
    ),
    sources=(
        V4.source_rule("slack"),
        V4.source_rule("notion"),
        V4.source_rule("google_calendar"),
        GITHUB_V5,
        SLURM_V5,
    ),
    supersedes="V4",
)


# Slack gained a bounded-window capture, the same shape as the Notion date
# slice: one month per run, ending where `until` says rather than at the live
# head. Github's mirror-freshness limitation is restated because the mtime test
# it described was replaced -- see the note itself for why.
SLACK_V6 = replace(
    V5.source_rule("slack"),
    includes=V5.source_rule("slack").includes
    + (
        "A bounded window (`until`), passed to conversations.history and "
        "conversations.replies as `latest`, so one run covers one month and ends by "
        "construction",
    ),
    excludes=V5.source_rule("slack").excludes
    + (
        "In slice mode: the checkpoint watermark, the watched-thread re-poll and the "
        "lookback, all of which track the live front and would empty a past window",
    ),
    known_limitations=V5.source_rule("slack").known_limitations
    + (
        "slack.date_slice_capture: a bounded window run captured one month instead of the "
        "live head. It reads from `since` and ignores the checkpoint watermark, because the "
        "watermark tracks the incremental front and would leave the past window empty. The "
        "watched-thread re-poll and the lookback are skipped for the same reason. "
        "`advance_checkpoint` is forced false: a run that saw only one month must not move a "
        "channel's watermark past it, or everything after that month is skipped forever. "
        "Workspace search is bounded with `before:`, and the window is applied again on the "
        "client because the server's timezone need not match ours -- anything the server "
        "returned above the window is counted in counters.search_matches_after_window rather "
        "than silently kept.",
    ),
    evidence=V5.source_rule("slack").evidence
    + (
        "src/rlwrld_worklog/slack_collector.py (`until` passed as `latest`, the client-side "
        "re-filter, and counters.search_matches_after_window)",
        "cowork/staging/roa-slack-month-backfill.py",
    ),
)


GITHUB_V6 = replace(
    V5.source_rule("github"),
    known_limitations=tuple(
        limitation
        for limitation in V5.source_rule("github").known_limitations
        if not limitation.startswith("github.commit_coverage_is_bounded_by_mirror_freshness")
    )
    + (
        "github.commit_coverage_is_bounded_by_mirror_freshness: mirror coverage is decided by "
        "the newest commit in the mirror's refs, not by the directory's mtime. A bare clone "
        "with no fetch refspec updates FETCH_HEAD without moving refs, so mtime gives false "
        "reassurance -- it hid 576 August commits, and three days that read 1, 0 and 1 were "
        "actually 152, 71 and 255. A mirror holding a commit after the window's end proves "
        "coverage; anything else is unknown. There is deliberately no False: a dormant "
        "repository and one that never received a fetch cannot be told apart from refs alone, "
        "and asserting either way makes one of them quietly wrong. The manifest counter is "
        "mirrors_with_unproven_coverage.",
    ),
    evidence=V5.source_rule("github").evidence
    + ("manifest counter: mirrors_with_unproven_coverage",),
)


V6 = CollectionRule(
    version="V6",
    title="공식 API 원본 원장 + 슬랙 날짜 슬라이스 (bounded Slack capture)",
    status="superseded",
    effective=EffectivePeriod(
        start="2026-09-03",
        end=None,
        basis=(
            "observed: slack_collector gained a bounded `until` window, and the github "
            "collector replaced its mtime freshness test with a refs-based one."
        ),
    ),
    summary=(
        "V5 with two corrections. Slack can be captured one bounded window at a time, the "
        "same shape as the Notion date slice, which is what makes a month-by-month backfill "
        "terminate. And GitHub decides mirror coverage from refs rather than directory mtime, "
        "which had been hiding commits behind a clone that fetched without moving its refs. "
        "Notion, Google Calendar and Slurm are unchanged from V5."
    ),
    manifest_schema_version=2,
    ledger_schema_version="1.0",
    source_schema_version=None,
    capture_profiles=V5.capture_profiles,
    storage_layout=V5.storage_layout,
    unknowns=V5.unknowns
    + (
        "Whether a GitHub mirror without a post-window commit is dormant or simply never "
        "fetched. Refs cannot separate the two, so coverage there is unknown rather than "
        "absent.",
    ),
    sources=(
        SLACK_V6,
        V5.source_rule("notion"),
        V5.source_rule("google_calendar"),
        GITHUB_V6,
        V5.source_rule("slurm"),
    ),
    supersedes="V5",
)


# --------------------------------------------------------------------- V7

# One completed one-day production manifest
# (20260903T021233Z-fd43af8d6700, window 2026-08-31 KST) spent 6,053 block
# requests and 1,500 comment requests across 176 pages, and the entire comment
# sweep returned one comment. A comment hangs off the block it was left on, so
# the exhaustive sweep asked every block; the collector now asks a page for its
# own comments and walks its blocks only when that answers. A page with no
# discussion costs one request instead of dozens. That is a narrowing of what
# gets collected, not a free saving, and it is written down as one below.
#
# Separately, the user mentions already inside every fetched block and comment
# are extracted instead of discarded, which answers "who mentioned whom" for
# Notion at no request that was not already being made.
NOTION_V7 = replace(
    V6.source_rule("notion"),
    includes=V6.source_rule("notion").includes
    + (
        "User mentions read out of the rich_text of blocks and comments already fetched, "
        "carried both on the timeline event (`mentions`) and on every ledger record "
        "(`relations.mentioned_user_ids`, with `relations.mentions_extracted` true)",
    ),
    excludes=V6.source_rule("notion").excludes
    + (
        "Under the page_first comment strategy: the per-block comment sweep for any object "
        "whose own /comments query returned nothing",
        "Page, database, date and link_preview mentions, which are not people and so are "
        "not given a direction; they stay in the raw block JSON the ledger keeps verbatim",
    ),
    known_limitations=V6.source_rule("notion").known_limitations
    + (
        "notion.comments_page_first: /comments is asked of each object first and the "
        "per-block sweep runs only for objects that answered with at least one comment. An "
        "inline comment on a block of a page carrying no page-level comment is therefore "
        "not fetched, and its absence is not evidence it does not exist. Measured: 1,500 "
        "per-block requests over one day returned one comment. The run is not marked "
        "truncated for it -- the narrowing is the rule the run followed, not a bound it hit "
        "-- so the checkpoint still advances; counters.comment_strategy names the sweep and "
        "counters.comment_blocks_unswept counts what it skipped.",
    ),
    evidence=V6.source_rule("notion").evidence
    + (
        "src/rlwrld_worklog/notion_collector.py (COMMENT_STRATEGIES, "
        "PAGE_FIRST_COMMENTS_NOTE, EXHAUSTIVE_COMMENTS_NOTE, `_collect_comments`)",
        "src/rlwrld_worklog/normalizers.py (notion_mention_user_ids, "
        "extract_notion_mentions)",
        "manifest fields: requested_window.comment_strategy, counters.comment_strategy, "
        "counters.comment_block_sweeps, counters.comment_blocks_unswept",
        "measured: manifests/notion/production/20260903T021233Z-fd43af8d6700.json "
        "(2,000 search candidates, 176 pages, 6,053 block requests, 1,500 comment "
        "requests, 1 comment)",
    ),
    unknowns=V6.source_rule("notion").unknowns
    + (
        "How many inline comments the page_first sweep misses. Counting them would cost "
        "exactly the exhaustive sweep it exists to avoid, so what is recorded is how many "
        "blocks went unasked, never how many comments were on them.",
    ),
)


V7 = CollectionRule(
    version="V7",
    title="공식 API 원본 원장 + 노션 멘션·페이지 우선 댓글 (Notion mentions and a page-first comment sweep)",
    # Superseded by V8 on 2026-09-08, when the Notion document set stopped
    # being whatever the block walk reached. The runs stamped V7 between
    # 2026-09-05 and then followed this rule and still verify against its
    # pinned digest; a version's end is derived from its successor rather than
    # written in here.
    status="superseded",
    effective=EffectivePeriod(
        start="2026-09-05",
        end=None,
        basis=(
            "observed: notion_collector gained a page-first comment sweep "
            "(COMMENT_STRATEGIES, default page_first) and the normalizer and ledger "
            "converter began extracting Notion user mentions."
        ),
    ),
    summary=(
        "V6 with the Notion capture corrected, driven by measurement. The comment sweep "
        "asks each object for its own comments and walks its blocks only when that answered, "
        "in place of a per-block sweep that spent 1,500 requests in one day to return one "
        "comment; a page with no discussion now costs one request. That narrows coverage -- "
        "an inline comment on a page with nothing at page level is missed -- so every run "
        "names the sweep it used. And the user mentions already sitting in the blocks and "
        "comments a run fetches are extracted rather than discarded, which answers who "
        "mentioned whom without one extra request. Slack, Google Calendar, GitHub and Slurm "
        "are unchanged from V6."
    ),
    manifest_schema_version=2,
    ledger_schema_version="1.0",
    source_schema_version=None,
    capture_profiles=V6.capture_profiles,
    storage_layout=V6.storage_layout,
    unknowns=V6.unknowns
    + (
        "How much Notion discussion the page-first comment sweep leaves unread. The only "
        "way to find out is the exhaustive sweep it replaces.",
    ),
    sources=(
        V6.source_rule("slack"),
        NOTION_V7,
        V6.source_rule("google_calendar"),
        V6.source_rule("github"),
        V6.source_rule("slurm"),
    ),
    supersedes="V6",
)


# --------------------------------------------------------------------- V8

# What a day's set of Notion documents is, decided by measurement rather than
# by what the walk happened to reach.
NOTION_V8 = replace(
    V7.source_rule("notion"),
    includes=V7.source_rule("notion").includes
    + (
        "Every row a data source seen in the window reports as edited in it, read from "
        "POST /data_sources/{id}/query with a last_edited_time filter rather than trusted to "
        "/search",
        "Each operator-configured seed page, retrieved once per run and collected when its "
        "own last_edited_time falls in the window",
    ),
    excludes=V7.source_rule("notion").excludes
    + (
        "Everything beneath a child_page or child_database block. The block itself is "
        "recorded; the walk does not enter it",
    ),
    known_limitations=V7.source_rule("notion").known_limitations
    + (
        "notion.document_set_is_search_plus_seeds: a day's documents are what /search "
        "listed in the window, plus queried data-source rows, plus seed pages that changed. "
        "Nothing is collected merely because its parent was edited. Measured on two "
        "collected days before the change: of the blocks below a child page that /search had "
        "not itself listed for that day, 0 of 2,767 on 2026-08-25 and 566 of 25,809 (2.2%) "
        "on 2026-09-02 had been edited in the window, while that descent cost 34% and 67% of "
        "the run's block requests. Almost every in-window block it found sat under a child "
        "page /search had listed, which the run walks as its own root anyway. The residual "
        "is real and unexplained -- search lag is the likeliest cause -- and "
        "counters.child_object_refs_unlisted is what makes a change in it visible.",
        "notion.data_source_rows_are_queried: every data source /search listed in the window "
        "is asked which of its rows changed in it, rather than trusting /search to have "
        "listed them all. The pass reaches listed sources only: a database whose own object "
        "search did not list is not queried, and its rows are covered by /search alone.",
        "notion.data_source_query_unavailable: a client build without the row query records "
        "this and the run carries on with /search alone, so a run that could not query says "
        "so instead of looking like one that found nothing to query.",
        "notion.seed_pages_are_checked_not_walked: a seed page is retrieved every run and "
        "captured only when it changed in the window, so a seed list is insurance against a "
        "missed page rather than a standing re-capture.",
    ),
    evidence=V7.source_rule("notion").evidence
    + (
        "src/rlwrld_worklog/notion_collector.py (PAGE_BOUNDARY_BLOCK_TYPES, "
        "`_collect_blocks`, `_query_data_sources`, `_check_seeds`)",
        "src/rlwrld_worklog/notion_client.py (`iter_data_source_rows`)",
        "manifest fields: counters.child_object_refs, counters.child_object_refs_unlisted, "
        "counters.data_source_query, counters.seed_pages",
        "measured from collected ledgers: live-20260906T121439Z-35a7acf7178c (2026-08-25) "
        "and live-20260906T130520Z-a496a1b444ed (2026-09-02), by attributing each block to "
        "its nearest child-page ancestor and splitting on whether /search had listed that "
        "ancestor",
    ),
    unknowns=V7.source_rule("notion").unknowns
    + (
        "Why /search had not listed the child pages holding the 566 in-window blocks "
        "measured on 2026-09-02. Index lag is the likeliest explanation and is not "
        "distinguishable from a permanent omission without asking Notion for the same day "
        "twice, days apart.",
    ),
)


V8 = CollectionRule(
    version="V8",
    title="공식 API 원본 원장 + 검색·시드로 정의된 문서 집합 (a day's documents are search plus seeds)",
    # Active from the day the collector changed, which is this one. The pending
    # Slack repair above it moved up a number rather than this one queueing
    # behind it: a stamp that does not describe the run is worth less than no
    # stamp, so the version describing what the collector now does takes force
    # immediately.
    status="superseded",
    effective=EffectivePeriod(
        start="2026-09-08",
        end=None,
        basis=(
            "observed: notion_collector stopped entering child_page and child_database "
            "blocks (PAGE_BOUNDARY_BLOCK_TYPES), gained a per-data-source row query "
            "(`_query_data_sources`) and an operator seed list (`_check_seeds`)."
        ),
    ),
    summary=(
        "V7 with the Notion document set stated instead of inherited. A day's documents are "
        "what /search listed inside the window, plus the rows each data source in that "
        "window reports as edited, plus the operator's seed pages that changed; the block "
        "walk stops at child_page and child_database rather than descending through them. "
        "The descent was not coverage: measured on two collected days, the blocks it reached "
        "under pages search had not listed were 0% and 2.2% in-window while costing a third "
        "to two thirds of the run's block requests, and what it did find in-window sat under "
        "pages search had listed and the run walks anyway. Every run now counts the child "
        "objects it declined to enter that no discovery pass had named, so a search that "
        "starts leaking is visible from inside the run rather than inferred later. Slack, "
        "Google Calendar, GitHub and Slurm are unchanged from V7."
    ),
    manifest_schema_version=2,
    ledger_schema_version="1.0",
    source_schema_version=None,
    capture_profiles=V7.capture_profiles,
    storage_layout=V7.storage_layout,
    unknowns=V7.unknowns
    + (
        "Whether an unlisted child object is ever a page /search will never return, as "
        "opposed to one it returns a run later. counters.child_object_refs_unlisted measures "
        "the population; only re-asking for an old day would separate the two.",
    ),
    sources=(
        V7.source_rule("slack"),
        NOTION_V8,
        V7.source_rule("google_calendar"),
        V7.source_rule("github"),
        V7.source_rule("slurm"),
    ),
    supersedes="V7",
)


# --------------------------------------------------------------------- V9

# V6 wrote the slice's shortcuts down as facts: in a bounded window the
# watched-thread re-poll and the lookback are skipped. Measurement says those
# shortcuts are why a whole class of message never arrives. The production
# checkpoint holds 356 watched threads spanning four days, because the list
# only grows at the incremental front and the 30-day lookback keeps trimming
# it, so a run reading August has almost nothing to re-poll no matter where
# the boundary is moved. And the 622 replies the legacy ledger has and the new
# one does not all hang off parents older than the window's own start.
#
# A thread's parent is therefore not reachable from inside the window at all.
# `conversations.history` returns a reply only when it was also broadcast to
# the channel, so for an ordinary reply there is never a message in the window
# that names its parent -- the slice cannot discover what to ask for. Closing
# that needs a pass that reads backwards from the window's start looking for
# parents, which is a change in what gets collected, so it is a new version
# rather than an edit to a published one.
SLACK_V9 = replace(
    V7.source_rule("slack"),
    includes=V7.source_rule("slack").includes
    + (
        "In slice mode: replies under threads whose parent sits before the window, found "
        "by reading the channel backwards from the window's start for parents rather than "
        "by re-polling the watched-thread list, which only ever holds the live front",
    ),
    excludes=tuple(
        exclude
        for exclude in V7.source_rule("slack").excludes
        if not exclude.startswith("In slice mode: the checkpoint watermark")
    )
    + (
        "In slice mode: the checkpoint watermark and the 30-day lookback, both of which "
        "track the live front and would empty a past window. The watched-thread re-poll is "
        "no longer in this list -- see slack.date_slice_capture",
    ),
    known_limitations=tuple(
        limitation
        for limitation in V7.source_rule("slack").known_limitations
        if not limitation.startswith("slack.date_slice_capture")
    )
    + (
        "slack.date_slice_capture: a bounded window run captures one month instead of the "
        "live head. It reads from `since` and ignores the checkpoint watermark, because the "
        "watermark tracks the incremental front and would leave the past window empty. "
        "`advance_checkpoint` is forced false: a run that saw only one month must not move a "
        "channel's watermark past it, or everything after that month is skipped forever. "
        "Workspace search is bounded with `before:`, and the window is applied again on the "
        "client because the server's timezone need not match ours -- anything the server "
        "returned above the window is counted in counters.search_matches_after_window rather "
        "than silently kept. V6 also said the watched-thread re-poll was skipped here. It is "
        "not, from this version: the re-poll runs inside the window, selecting the threads "
        "whose in-window activity proves they were active in it and resuming each from "
        "`since` rather than from the watermark, which is a destination and not a starting "
        "point. The 30-day lookback stays out -- it is measured from the live head and means "
        "nothing to a run reading a past month.",
        "slack.prewindow_parent_discovery: replies whose parent predates the window are "
        "reachable only by looking for the parent before the window opens, so a slice reads "
        "backwards from `since` for thread parents. The watched-thread list cannot stand in "
        "for that pass: it is written when a reply names its parent, and an ordinary reply "
        "is not returned by conversations.history at all, so for most threads no message "
        "inside the window ever names the parent. Measured: the production checkpoint held "
        "86 channels and 356 watched threads whose oldest last-reply was 2026-08-30, four "
        "days wide, while the 622 replies missing from the new ledger all hang off parents "
        "before 2026-08-01. How far back the pass reads bounds what it can recover, and a "
        "parent older than that is still missed; the reach used by a run is recorded rather "
        "than assumed.",
    ),
    evidence=V7.source_rule("slack").evidence
    + (
        "checkpoint measurement: manifests/slack/production/checkpoint.json, 86 channels and "
        "356 watched threads spanning 2026-08-30 to 2026-09-03",
        "cowork/logs/roa-legacy-vs-new-202608-recon.md (the 622 replies and their parents)",
    ),
)


V9 = CollectionRule(
    version="V9",
    title="공식 API 원본 원장 + 창 앞 부모 탐색 (slice recovers pre-window parents)",
    # Active from the day the bounded pre-window discovery pass landed. The
    # earlier pending draft stamped no manifest and pinned no digest.
    status="active",
    effective=EffectivePeriod(
        start="2026-09-09",
        end=None,
        basis=(
            "observed: slack_collector gained a bounded pre-window parent-discovery pass "
            "and window-qualified watched-thread re-polling for date slices."
        ),
    ),
    summary=(
        "V8 with the Slack slice corrected. A bounded window run now recovers replies whose "
        "parent predates it, by reading backwards from the window's start for thread "
        "parents and by re-polling watched threads inside the window instead of skipping "
        "the re-poll. V6 described the skips as deliberate; measurement showed they are why "
        "a month-by-month backfill silently misses replies to older threads. Notion, Google "
        "Calendar, GitHub and Slurm are unchanged from V8. Published first as V7 and "
        "renumbered twice as Notion repairs landed and took the numbers below it; it had "
        "stamped nothing and pinned no digest either time."
    ),
    manifest_schema_version=2,
    ledger_schema_version="1.0",
    source_schema_version=None,
    capture_profiles=V8.capture_profiles,
    storage_layout=V8.storage_layout,
    unknowns=V8.unknowns
    + (
        "How far before a window a parent can sit and still be recovered. The backward pass "
        "has to stop somewhere, and a thread whose parent is older than it reaches is missed "
        "the same way it is missed today -- less often, but not never.",
    ),
    sources=(
        SLACK_V9,
        V8.source_rule("notion"),
        V8.source_rule("google_calendar"),
        V8.source_rule("github"),
        V8.source_rule("slurm"),
    ),
    supersedes="V8",
)


# --------------------------------------------------------------- registry

RULES: tuple[CollectionRule, ...] = (V0, V1, V2, V3, V4, V5, V6, V7, V8, V9)

ACTIVE_RULE_VERSION = "V9"

# Content digests of every published version. A published rule is frozen: if
# editing one changes its meaning, the digest moves and import fails here,
# which is the signal to append a new version instead of rewriting history.
# Digests these versions carried under registry schema 1, when `status` was
# still part of the hash. Manifests written then recorded these, and they must
# keep verifying: the rule content did not change, the digest definition did.
HISTORICAL_DIGESTS: dict[str, tuple[str, ...]] = {
    "V0": ("sha256:0be5ca5652806fc7e0c1439c3a860c2dd290a7c2be9e9b53fb25c30332edcc4a",),
    "V1": ("sha256:2a7c9b6d1357927a85057917729dd810fa2f2f438f3c7b744bb2a4590476186a",),
}


def effective_window(
    version: str, rules: tuple[CollectionRule, ...] | None = None
) -> dict[str, Any]:
    """A version's applicable period and successor, derived rather than stored.

    The rule's own `basis` prose was frozen at publication and may still say it
    is current. This is what the registry actually knows now, and it is what a
    view should show.
    """
    catalogue = rules if rules is not None else RULES
    index = {rule.version: position for position, rule in enumerate(catalogue)}
    position = index.get(version)
    if position is None:
        return {"start": None, "end": None, "superseded_by": None, "is_current": False}
    rule = catalogue[position]
    successor = next(
        (
            later
            for later in catalogue[position + 1 :]
            # A pending version already names what it will supersede, but no
            # run has followed it yet, so there is no day on which this
            # version stopped applying. Only a version in force closes one.
            if later.supersedes == version and later.status != "pending"
        ),
        None,
    )
    return {
        "start": rule.effective.start,
        # The successor's start is this version's end: the day collection began
        # following the new rule is the day it stopped following this one.
        "end": successor.effective.start if successor is not None else None,
        "superseded_by": successor.version if successor is not None else None,
        "is_current": successor is None and rule.status == "active",
    }


def digest_is_recognised(version: str, digest: str | None) -> bool:
    """Does this digest identify that version, now or under an earlier schema?"""
    if not digest:
        return False
    rule = rule_for_version(version)
    if rule is not None and digest == rule.digest:
        return True
    return digest in HISTORICAL_DIGESTS.get(version, ())


# Re-pinned once, at registry schema 2, when `status` left the digest. The rule
# *content* of V0 and V1 is byte-for-byte what it was; only what the hash covers
# changed. HISTORICAL_DIGESTS above keeps the earlier values verifying.
PUBLISHED_DIGESTS: dict[str, str] = {
    "V0": "sha256:1987bcce80c135426788e3975eac168312136665cb6e8239fba7114a85339cac",
    "V1": "sha256:75c1314212d733305fb2acfaf337b033a9489e4eb81b1b6005bd564f486cb7d8",
    "V2": "sha256:831aec5edb7e4a349798ec1ec8d9e5bd23f75b052d09c7b157ab5d1dafbfe939",
    "V3": "sha256:52a5f9e46a1a1af21fa391f43be294ea87f1505a43b4eb94b3f76492205dadd8",
    "V4": "sha256:da7f50ccadb136c979097e6929af10c41b11855203bfa0eaaae6ca1355875e20",
    "V5": "sha256:ba77758f618dc29f60d48adc3a47f8b17867aa6b9683ada5e3d63a15ae213941",
    "V6": "sha256:ddbf228989f159091e7b02f9fc6ce7acd73713e26f3fdaa39ca92d2cb637a5f3",
    "V7": "sha256:09b4586496cc1f2a403b112e30e6b3ff81409ff325cb72391dc35dccc43b78fc",
    "V8": "sha256:0c44b9e1e3c3abe12801f3543f5a7b4ccf4adcc5ddba1bc4dd47021cd3e89aaa",
    "V9": "sha256:df78b1d883f31568093d35e36ac5ed587b6b10a2dfadb16a6dd15cb4bed6a293",
}


def _validate_registry(rules: tuple[CollectionRule, ...]) -> None:
    seen: set[str] = set()
    active: list[str] = []
    for rule in rules:
        if rule.version in seen:
            raise RuleRegistryError(
                f"collection rule {rule.version} is declared twice; the registry is append-only"
            )
        seen.add(rule.version)
        if rule.status not in RULE_STATUSES:
            raise RuleRegistryError(f"collection rule {rule.version} has an unknown status")
        if rule.status == "active":
            active.append(rule.version)
        if rule.status == "pending":
            # A pending rule is not frozen and has not started. Both follow
            # from the same fact -- nothing has run under it -- and both are
            # written in by the same change that activates it, so enforcing
            # them here keeps that flip from being half done.
            if rule.effective.start is not None:
                raise RuleRegistryError(
                    f"collection rule {rule.version} is pending but stores an effective "
                    "start; a version starts on the day a run first follows it"
                )
            if rule.version in PUBLISHED_DIGESTS:
                raise RuleRegistryError(
                    f"collection rule {rule.version} is pending but its digest is pinned; "
                    "a rule is frozen when it takes effect, not before"
                )
        declared: set[str] = set()
        for source_rule in rule.sources:
            if source_rule.source not in SOURCES:
                raise RuleRegistryError(
                    f"collection rule {rule.version} names an unknown source "
                    f"{source_rule.source!r}"
                )
            if source_rule.source in declared:
                raise RuleRegistryError(
                    f"collection rule {rule.version} defines {source_rule.source!r} twice"
                )
            declared.add(source_rule.source)
        if not declared:
            raise RuleRegistryError(f"collection rule {rule.version} defines no source")
        if rule.effective.end is not None:
            # Storing an end would have to be written in after a successor
            # appears, which means editing digest-frozen content. Derived by
            # `effective_window` instead, so publishing a successor never has
            # to reach back into a published rule.
            raise RuleRegistryError(
                f"collection rule {rule.version} stores an effective end; a version's end is "
                "derived from its successor, never written into the published rule"
            )
    if active != [ACTIVE_RULE_VERSION]:
        raise RuleRegistryError(
            f"exactly one rule must be active and it must be {ACTIVE_RULE_VERSION}; got {active}"
        )
    # A retired version is a record of what a past collector did, so it defines
    # the sources it actually knew about and nothing more. Demanding that every
    # version define every source would mean back-dating a source into rules
    # written before that collector existed, which would make those rules lie.
    # The guarantee that matters is about collection happening now, so it is
    # the active version that must account for every source we collect.
    for rule in rules:
        if rule.status != "active":
            continue
        uncovered = set(SOURCES) - {source_rule.source for source_rule in rule.sources}
        if uncovered:
            raise RuleRegistryError(
                f"the active collection rule {rule.version} does not define "
                f"{', '.join(sorted(uncovered))}; every collected source needs a published rule"
            )
    if rule_digest_mismatches(rules):
        raise RuleRegistryError(
            "a published collection rule was edited: "
            + "; ".join(
                f"{version} is now {actual} but was published as {expected}"
                for version, expected, actual in rule_digest_mismatches(rules)
            )
            + ". A published rule is frozen -- append a new version instead."
        )


def rule_digest_mismatches(
    rules: tuple[CollectionRule, ...] | None = None,
) -> list[tuple[str, str, str]]:
    """(version, published digest, current digest) for every edited rule."""
    mismatches: list[tuple[str, str, str]] = []
    for rule in rules if rules is not None else RULES:
        expected = PUBLISHED_DIGESTS.get(rule.version)
        if expected is not None and expected != rule.digest:
            mismatches.append((rule.version, expected, rule.digest))
    return mismatches


def rule_for_version(version: str | None) -> CollectionRule | None:
    for rule in RULES:
        if rule.version == version:
            return rule
    return None


def active_rule() -> CollectionRule:
    rule = rule_for_version(ACTIVE_RULE_VERSION)
    if rule is None:  # pragma: no cover - _validate_registry rules this out
        raise RuleRegistryError("no active collection rule is published")
    return rule


def active_rule_stamp() -> dict[str, Any]:
    """The three fields every new manifest carries."""
    rule = active_rule()
    return {
        "collection_rule_version": rule.version,
        "collection_rule_digest": rule.digest,
        "collection_rule_schema_version": RULE_REGISTRY_SCHEMA_VERSION,
    }


def registry_as_dict() -> dict[str, Any]:
    return {
        "registry_schema_version": RULE_REGISTRY_SCHEMA_VERSION,
        "active_version": ACTIVE_RULE_VERSION,
        "sources": list(SOURCES),
        "source_labels": dict(SOURCE_LABELS),
        "digests_pinned": not rule_digest_mismatches(),
        # `effective_window` is attached per rule, outside the frozen body: a
        # published rule's own prose was written before it had a successor and
        # can still say it is current. The derived window is what the registry
        # knows now, and a view showing the two together should trust this one.
        "rules": [
            {**rule.as_dict(), "effective_window": effective_window(rule.version)}
            for rule in RULES
        ],
    }


def stamp_from_manifest(manifest: Mapping[str, Any]) -> dict[str, Any] | None:
    """The rule stamp a manifest declares, or None when it declares none."""
    version = manifest.get("collection_rule_version")
    if not isinstance(version, str) or not version:
        return None
    digest = manifest.get("collection_rule_digest")
    schema = manifest.get("collection_rule_schema_version")
    return {
        "version": version,
        "digest": digest if isinstance(digest, str) else None,
        "schema_version": schema if isinstance(schema, int) else None,
    }


_validate_registry(RULES)
