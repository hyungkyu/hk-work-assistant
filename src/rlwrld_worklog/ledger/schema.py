"""Standard v1 ledger record: dataclasses plus the JSON Schema.

Field set is Codex's standard v1 (17 fields) with the five approved ledger
extensions: observation_window, capture_completeness, supplement_provenance,
visibility_routing, denormalized_label_snapshot.

Deliberately NOT ledger fields:
  * extracted_text        -> separate preserved artifact (ExtractedText)
  * derived_attribution   -> service-derived area, never the ledger
  * roster_identity_link  -> service-derived area
  * computed_metrics      -> service-derived area
  * legacy_layout_version -> parser provenance, inside `provenance`
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any
from uuid import NAMESPACE_URL, uuid5

LEDGER_SCHEMA_VERSION = "1.0"
CONVERTER_VERSION = "legacy-converter/1.0.0"

# capture_profile identifies *how* a record was captured, independently of
# whether that capture was complete (principle 8 in the Phase 1 standard).
CAPTURE_PROFILES = {
    "slack_message": "legacy-slack-slim12/v1",
    "slack_thread_store": "legacy-slack-thread-store/v1",
    "notion_page": "legacy-notion-page/v1",
    "notion_block": "legacy-notion-block/v1",
    "notion_comment": "legacy-notion-comment-from-attribution/v1",
    "github_commit": "live-github-commit-from-mirror/v1",
    "github_rest": "live-github-rest/v1",
    "slurm_job": "live-slurm-sacct-dump/v1",
    "slurm_job_legacy": "legacy-slurm-sacct-archive/v1",
}

# Activity entities carry a timeline projection. Dimension entities describe
# the containers and actors those activities refer to: they are part of the
# ledger because rule 2 requires the service database to be rebuildable from
# ledger data alone, and a message without its channel and its author is not
# rebuildable. They are deliberately not projected onto the timeline.
ACTIVITY_ENTITY_TYPES = (
    "message",
    "page",
    "block",
    "comment",
    "event",
    # GitHub. A commit is read from a bare mirror, the other five from REST.
    "commit",
    "pull_request",
    "review",
    "review_comment",
    "issue",
    "issue_comment",
    # Slurm. One record per finished job, keyed on its end date. Slurm step
    # rows (`.batch`, `.extern`) are sub-resources of a job rather than
    # activities of their own: they hold the real resource usage, the raw
    # archive keeps all 117 columns of them, but projecting them onto the
    # timeline would turn one job into two or three timeline events. They
    # need a third category alongside activity and dimension, so they have no
    # ledger entity type yet.
    "job",
)
DIMENSION_ENTITY_TYPES = (
    "user",
    "usergroup",
    "conversation",
    "calendar",
    "data_source",
    # GitHub. A commit without its repository is not rebuildable, and the
    # repository is a container, not an activity.
    "repository",
)
ENTITY_TYPES = ACTIVITY_ENTITY_TYPES + DIMENSION_ENTITY_TYPES
SOURCES = ("slack", "notion", "google_calendar", "github", "slurm")
UNKNOWN_STATUSES = ("observed", "unknown", "not_recorded", "recorded")


def ledger_id_for(
    *,
    source: str,
    entity_type: str,
    tenant_id: str,
    scope_key: str,
    source_entity_id: str,
    window_start: str | None,
    content_hash: str,
) -> str:
    """Deterministic id.

    The observation window is part of the identity because legacy records are
    day slices: the same object seen on two days is two historical
    observations, not one row. `content_hash` collapses the genuine
    within-store duplicates (a thread_broadcast indexed twice) without
    collapsing distinct observations.

    `scope_key` participates only when it is part of the entity's natural key:
    a Slack message is identified by its channel, and a Notion block by its
    page. A Notion page is identified by its page id alone -- which database
    query happened to surface it is scope, not identity -- so callers pass an
    empty scope_key there. Otherwise the same page dumped by two source
    queries on one day would produce two byte-identical rows.
    """
    identity = "|".join(
        [
            source,
            entity_type,
            tenant_id,
            scope_key,
            source_entity_id,
            window_start or "",
            content_hash,
        ]
    )
    return str(uuid5(NAMESPACE_URL, identity))


@dataclass(slots=True)
class LedgerRecord:
    ledger_id: str
    schema_version: str
    capture_profile: str
    source: str
    tenant: dict[str, Any]
    scope: dict[str, Any]
    entity_type: str
    source_entity_id: str
    source_entity_key: dict[str, Any]
    source_revision_id: str | None
    source_created_at: str | None
    source_updated_at: str | None
    source_updated_at_status: str
    collected_at: str | None
    deleted_state: dict[str, Any]
    raw_payload: dict[str, Any]
    content_hash: str
    relations: dict[str, Any]
    provenance: dict[str, Any]
    coverage: dict[str, Any]
    observation_window: dict[str, Any]
    capture_completeness: dict[str, Any]
    supplement_provenance: dict[str, Any]
    visibility_routing: dict[str, Any]
    denormalized_label_snapshot: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ExtractedText:
    """Text that exists only because the legacy collector extracted it.

    Gemini meeting notes and Notion `_blocks_text` cannot be re-fetched from
    the live API, so they are preserved verbatim as their own artifact rather
    than folded into raw_payload (principle 4).
    """

    artifact_id: str
    schema_version: str
    ledger_id: str
    source: str
    kind: str
    text: str
    text_sha256: str
    char_length: int
    byte_length: int
    extractor: str
    source_ref: dict[str, Any]
    provenance: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _obj(properties: dict[str, Any], *, required: list[str] | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


_NULLABLE_STRING = {"type": ["string", "null"]}
_NULLABLE_BOOL = {"type": ["boolean", "null"]}
_NULLABLE_INT = {"type": ["integer", "null"]}


def ledger_json_schema() -> dict[str, Any]:
    """JSON Schema (draft 2020-12) for a standard v1 ledger record."""
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://rlwrld.ai/schemas/worklog-ledger/v1.json",
        "title": "RLWRLD worklog standard ledger v1",
        "type": "object",
        "additionalProperties": False,
        "required": [
            "ledger_id",
            "schema_version",
            "capture_profile",
            "source",
            "tenant",
            "scope",
            "entity_type",
            "source_entity_id",
            "source_entity_key",
            "source_created_at",
            "source_updated_at_status",
            "deleted_state",
            "raw_payload",
            "content_hash",
            "relations",
            "provenance",
            "coverage",
            "observation_window",
            "capture_completeness",
            "supplement_provenance",
            "visibility_routing",
            "denormalized_label_snapshot",
        ],
        "properties": {
            "ledger_id": {"type": "string", "format": "uuid"},
            "schema_version": {"const": LEDGER_SCHEMA_VERSION},
            "capture_profile": {"type": "string", "minLength": 1},
            "source": {"enum": list(SOURCES)},
            "tenant": _obj(
                {
                    "workspace_id": {"type": "string"},
                    "status": {"type": "string"},
                },
                required=["workspace_id", "status"],
            ),
            "scope": _obj(
                {
                    "kind": {"type": "string"},
                    "channel_id": _NULLABLE_STRING,
                    "is_private": _NULLABLE_BOOL,
                    "container": _NULLABLE_STRING,
                    "calendar_id": _NULLABLE_STRING,
                    "calendar_id_status": _NULLABLE_STRING,
                    "notion_source_key": _NULLABLE_STRING,
                    "notion_source_id": _NULLABLE_STRING,
                    "notion_source_type": _NULLABLE_STRING,
                    "parent_page_id": _NULLABLE_STRING,
                },
                required=["kind"],
            ),
            "entity_type": {"enum": list(ENTITY_TYPES)},
            "source_entity_id": {"type": "string", "minLength": 1},
            "source_entity_key": {"type": "object"},
            "source_revision_id": _NULLABLE_STRING,
            "source_created_at": _NULLABLE_STRING,
            "source_updated_at": _NULLABLE_STRING,
            "source_updated_at_status": {"enum": ["observed", "unknown"]},
            "collected_at": _NULLABLE_STRING,
            "deleted_state": _obj(
                {
                    "is_deleted": _NULLABLE_BOOL,
                    "kind": _NULLABLE_STRING,
                    "status": {"enum": ["observed", "unknown"]},
                },
                required=["is_deleted", "status"],
            ),
            "raw_payload": {"type": "object"},
            "content_hash": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"},
            "relations": {"type": "object"},
            "provenance": _obj(
                {
                    "source_file": {"type": "string"},
                    "source_file_sha256": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"},
                    "source_file_kind": {"type": "string"},
                    "record_pointer": {"type": "string"},
                    "legacy_layout_version": {"type": "string"},
                    "converter_version": {"type": "string"},
                    "api_endpoint": _NULLABLE_STRING,
                    "cursor": _NULLABLE_STRING,
                    "collector_run_id": _NULLABLE_STRING,
                },
                required=[
                    "source_file",
                    "source_file_sha256",
                    "record_pointer",
                    "legacy_layout_version",
                    "converter_version",
                ],
            ),
            "coverage": _obj(
                {
                    "observation_role": {"enum": ["historical_observation", "current_head"]},
                    "record_count_in_file": _NULLABLE_INT,
                    "declared_count_in_file": _NULLABLE_INT,
                    "count_matches_declared": _NULLABLE_BOOL,
                    "permission_gap": {"type": ["object", "null"]},
                    "errors": {"type": "array"},
                },
                required=["observation_role"],
            ),
            "observation_window": _obj(
                {
                    "start": _NULLABLE_STRING,
                    "end": _NULLABLE_STRING,
                    "tz": {"type": "string"},
                    "granularity": {"enum": ["day", "range", "unknown"]},
                },
                required=["start", "end", "tz", "granularity"],
            ),
            "capture_completeness": _obj(
                {
                    "status": {"enum": ["recorded", "not_recorded", "unknown"]},
                    "truncated": _NULLABLE_BOOL,
                    "truncation_events": _NULLABLE_INT,
                    "rate_limit_hits": _NULLABLE_INT,
                    "channels_scanned": _NULLABLE_INT,
                    "channels_empty": _NULLABLE_INT,
                    "channels_active": _NULLABLE_INT,
                    "legacy_status_field": _NULLABLE_STRING,
                    "legacy_status_is_trustworthy": {"type": "boolean"},
                    "lossy_fields": {"type": "object"},
                    "notes": _NULLABLE_STRING,
                },
                required=["status", "legacy_status_is_trustworthy"],
            ),
            "supplement_provenance": _obj(
                {
                    "is_supplement": _NULLABLE_BOOL,
                    "supplement_kind": _NULLABLE_STRING,
                    "schema_variant": {"type": "string"},
                    "merged_into_primary": _NULLABLE_BOOL,
                    "file_supplement_runs": _NULLABLE_INT,
                },
                required=["schema_variant"],
            ),
            "visibility_routing": _obj(
                {
                    "storage_root": {"type": "string"},
                    "container": _NULLABLE_STRING,
                    "visibility": _NULLABLE_STRING,
                    "access_list": {"type": ["array", "null"]},
                    "collected_by": _NULLABLE_STRING,
                    "source_schema_version": _NULLABLE_STRING,
                    "patched_at": _NULLABLE_STRING,
                    "meta_present": {"type": "boolean"},
                    "routing_anomaly": _NULLABLE_STRING,
                },
                required=["storage_root", "meta_present"],
            ),
            "denormalized_label_snapshot": {"type": "object"},
        },
    }


_VALIDATOR = None


def _validator():
    global _VALIDATOR
    if _VALIDATOR is None:
        from jsonschema import Draft202012Validator

        _VALIDATOR = Draft202012Validator(ledger_json_schema())
    return _VALIDATOR


def validate_record(record: dict[str, Any]) -> list[str]:
    """Returns a list of human-readable validation errors (empty when valid)."""
    return [
        f"{'/'.join(str(part) for part in error.path) or '<root>'}: {error.message}"
        for error in sorted(_validator().iter_errors(record), key=lambda item: list(item.path))
    ]
