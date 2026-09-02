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

# Canonical ledger source names, as used by the ledger and the service DB.
SOURCES = ("slack", "notion", "google_calendar")

# Collector directory names, as used under raw/ and manifests/.
COLLECTOR_SOURCES = ("slack", "notion", "google-calendar")

SOURCE_TO_COLLECTOR = {
    "slack": "slack",
    "notion": "notion",
    "google_calendar": "google-calendar",
}
COLLECTOR_TO_SOURCE = {value: key for key, value in SOURCE_TO_COLLECTOR.items()}

SOURCE_LABELS = {
    "slack": "Slack",
    "notion": "Notion",
    "google_calendar": "Google Calendar",
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
    """When a rule applied, and how that is known."""

    start: str | None
    end: str | None
    basis: str

    def as_dict(self) -> dict[str, Any]:
        return {"start": self.start, "end": self.end, "basis": self.basis}


@dataclass(frozen=True)
class CollectionRule:
    version: str
    title: str
    status: str  # active | superseded
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
    status="active",
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


# --------------------------------------------------------------- registry

RULES: tuple[CollectionRule, ...] = (V0, V1, V2)

ACTIVE_RULE_VERSION = "V2"

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
        if rule.status not in {"active", "superseded"}:
            raise RuleRegistryError(f"collection rule {rule.version} has an unknown status")
        if rule.status == "active":
            active.append(rule.version)
        for source_rule in rule.sources:
            if source_rule.source not in SOURCES:
                raise RuleRegistryError(
                    f"collection rule {rule.version} names an unknown source "
                    f"{source_rule.source!r}"
                )
        missing = set(SOURCES) - {source_rule.source for source_rule in rule.sources}
        if missing:
            raise RuleRegistryError(
                f"collection rule {rule.version} does not define {', '.join(sorted(missing))}"
            )
    if active != [ACTIVE_RULE_VERSION]:
        raise RuleRegistryError(
            f"exactly one rule must be active and it must be {ACTIVE_RULE_VERSION}; got {active}"
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
        "rules": [rule.as_dict() for rule in RULES],
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
