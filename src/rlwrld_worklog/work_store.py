"""Shared local store for delegated work items.

One JSON document under APP_CONFIG_ROOT holds every tracked work item, so the
backoffice, the local CLI, and any agent script observe exactly the same state.
Writers take an exclusive file lock and replace the document atomically.  Every
read validates the complete stored document - exact shape, every field's type
and constraints, and the whole parent graph - so a document that is unreadable
or does not match the schema is surfaced as corruption and is never overwritten,
repaired, or silently defaulted.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

from .admin_store import _atomic_private_write
from .cowork import resolve_actor


DOCUMENT_VERSION = 2
MAX_ITEMS = 5_000
MAX_PARENT_DEPTH = 8

# The four stages every item flows through, most advanced first.  These are the
# board's primary classification: an item is always at exactly one of them.
QUEUE_STAGES = ("in_progress", "ready", "todo", "backlog")

# Conditions rather than queue positions.  `waiting` and `blocked` say why an
# item is not moving; `done` and `cancelled` say it has left the queue.
SUPPORTING_STATUSES = ("waiting", "blocked", "done", "cancelled")

STATUSES = QUEUE_STAGES + SUPPORTING_STATUSES

# The v1 enum, frozen.  A document that still declares version 1 is validated
# against exactly the statuses v1 could produce, so a file containing `todo`
# while claiming to be v1 is reported as corruption instead of being accepted
# under the wider v2 enum.
V1_STATUSES = ("backlog", "ready", "in_progress", "waiting", "blocked", "done", "cancelled")

STATUS_LABELS = {
    "in_progress": "진행 중",
    "ready": "다음 할 일",
    "todo": "해야 할 일",
    "backlog": "백로그",
    "waiting": "대기",
    "blocked": "막힘",
    "done": "완료",
    "cancelled": "취소",
}
STATUS_GROUPS = {name: "queue" for name in QUEUE_STAGES} | {
    name: "supporting" for name in SUPPORTING_STATUSES
}
STATUS_ORDER = {name: index for index, name in enumerate(STATUSES)}

# How a board lays the statuses out.  This is part of the schema, not of the
# page, because the board must be a *total* partition of STATUSES: an item may
# never fall between two columns and disappear.  `_validate_board_columns`
# enforces that at import, and any status a client's own column list fails to
# claim belongs in an explicit residue column rather than nowhere.
BOARD_COLUMNS: tuple[dict[str, Any], ...] = (
    {"key": "in_progress", "title": "진행 중", "statuses": ("in_progress",), "recent_days": None},
    {"key": "ready", "title": "다음 할 일", "statuses": ("ready",), "recent_days": None},
    {"key": "todo", "title": "해야 할 일", "statuses": ("todo",), "recent_days": None},
    {"key": "backlog", "title": "백로그", "statuses": ("backlog",), "recent_days": None},
    {"key": "held", "title": "대기 · 막힘", "statuses": ("waiting", "blocked"), "recent_days": None},
    {"key": "closed", "title": "최근 완료", "statuses": ("done", "cancelled"), "recent_days": 14},
)
RESIDUE_COLUMN_KEY = "unclassified"
RESIDUE_COLUMN_TITLE = "미분류"

TERMINAL_STATUSES = frozenset({"done", "cancelled"})
PRIORITIES = ("urgent", "high", "normal", "low")
PRIORITY_RANK = {name: index for index, name in enumerate(PRIORITIES)}

# Fields a caller may set on create or update.  Everything else - including the
# identifier, the revision, and the archive marker - is owned by the store.
MUTABLE_FIELDS = (
    "title",
    "detail",
    "status",
    "priority",
    "requested_by",
    "assigned_to",
    "parent_id",
    "progress_summary",
    "next_action",
    "blocker",
    "due_at",
    "started_at",
    "completed_at",
    "source_ref",
)
REQUIRED_ON_CREATE = ("title", "requested_by", "assigned_to")
SERVER_OWNED_FIELDS = ("id", "created_at", "updated_at", "revision", "archived_at")

TEXT_FIELDS = {
    "title": (1, 200),
    "detail": (0, 8_000),
    "progress_summary": (0, 2_000),
    "next_action": (0, 500),
    "blocker": (0, 500),
    "source_ref": (0, 500),
}
OPTIONAL_TEXT_FIELDS = frozenset({"detail", "blocker", "source_ref"})
TIMESTAMP_FIELDS = ("due_at", "started_at", "completed_at")

# Agent or person identifiers such as codex, claude-code, user, or a company
# Google address.  Deliberately narrow so identifiers stay comparable.
_ACTOR = re.compile(r"^[a-z0-9][a-z0-9._@+-]{0,79}$")
_ITEM_ID = re.compile(r"^wi_[0-9a-f]{16}$")


class WorkStoreError(RuntimeError):
    """Base class for every work-store failure."""


class WorkValidationError(WorkStoreError, ValueError):
    """A caller supplied an unusable field, value, or relationship."""


class WorkNotFoundError(WorkStoreError):
    """The requested work item does not exist."""


class WorkConflictError(WorkStoreError):
    """Another writer changed the item since the caller last read it."""


class WorkCorruptionError(WorkStoreError):
    """The stored document is unreadable; it is left untouched on disk."""


class WorkLockTimeout(WorkStoreError):
    """Another writer held the store lock for too long."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_item_id() -> str:
    return "wi_" + secrets.token_hex(8)


def _normalize_timestamp(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 64:
        raise WorkValidationError(f"{field} must be an ISO 8601 timestamp")
    text = value.strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as error:
        raise WorkValidationError(f"{field} must be an ISO 8601 timestamp") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _normalize_text(value: Any, field: str) -> str | None:
    minimum, maximum = TEXT_FIELDS[field]
    if value is None:
        if field in OPTIONAL_TEXT_FIELDS:
            return None
        raise WorkValidationError(f"{field} must be a string")
    if not isinstance(value, str):
        raise WorkValidationError(f"{field} must be a string")
    text = value.strip()
    if not text and field in OPTIONAL_TEXT_FIELDS:
        return None
    if len(text) < minimum:
        raise WorkValidationError(f"{field} is required")
    if len(text) > maximum:
        raise WorkValidationError(f"{field} must be at most {maximum} characters")
    return text


def _normalize_actor(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise WorkValidationError(f"{field} must be a string")
    text = value.strip().lower()
    if not _ACTOR.fullmatch(text):
        raise WorkValidationError(
            f"{field} must be a short identifier such as codex, claude-code, or an email address"
        )
    return text


# Where a change sits in the life of a work item. Recorded on the history
# entry so a timeline can be read without re-deriving intent from field names.
PHASES = (
    "assigned", "started", "progress", "review",
    "build", "deploy", "verified", "failed",
)

# An operational note written by the caller for the timeline. It is not the
# item's own text: `detail` and `progress_summary` still never reach the
# history stream, so a summary cannot become a side channel for item content.
MAX_CONTEXT_SUMMARY = 500


def _normalize_receipt(value: Any) -> str | None:
    """A receipt path recorded as a local identifier, never an absolute path.

    Absolute paths and traversal are refused rather than trimmed: a timeline
    that points outside the cowork tree is a worse record than one that
    admits it has no receipt.
    """
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise WorkValidationError("receipt must be a non-empty string")
    text = value.strip()
    if len(text) > 300:
        raise WorkValidationError("receipt path is too long")
    if text.startswith("/") or text.startswith("~") or ".." in text.split("/"):
        raise WorkValidationError("receipt must be a relative path inside the cowork tree")
    return text


def _normalize_context(context: Mapping[str, Any] | None) -> dict[str, Any]:
    """Validate the optional timeline context supplied with a change."""
    if context is None:
        return {}
    if not isinstance(context, Mapping):
        raise WorkValidationError("context must be an object")
    unknown = sorted(set(context) - {"directed_by", "phase", "session_id", "summary", "receipt"})
    if unknown:
        raise WorkValidationError(f"unknown context fields: {', '.join(unknown)}")
    normalized: dict[str, Any] = {}
    if context.get("directed_by") is not None:
        normalized["directed_by"] = _normalize_actor(context["directed_by"], "directed_by")
    phase = context.get("phase")
    if phase is not None:
        if phase not in PHASES:
            raise WorkValidationError(f"phase must be one of {', '.join(PHASES)}")
        normalized["phase"] = phase
    session = context.get("session_id")
    if session is not None:
        if not isinstance(session, str) or not session.strip():
            raise WorkValidationError("session_id must be a non-empty string")
        if len(session) > 120:
            raise WorkValidationError("session_id is too long")
        normalized["session_id"] = session.strip()
    summary = context.get("summary")
    if summary is not None:
        if not isinstance(summary, str) or not summary.strip():
            raise WorkValidationError("summary must be a non-empty string")
        if len(summary) > MAX_CONTEXT_SUMMARY:
            raise WorkValidationError(
                f"summary must be at most {MAX_CONTEXT_SUMMARY} characters"
            )
        normalized["summary"] = summary.strip()
    receipt = _normalize_receipt(context.get("receipt"))
    if receipt is not None:
        normalized["receipt"] = receipt
    return normalized


def _normalize_changes(changes: Mapping[str, Any]) -> dict[str, Any]:
    """Validate one field bundle, rejecting anything the store does not own."""
    if not isinstance(changes, Mapping):
        raise WorkValidationError("fields must be an object")
    unsafe = sorted(set(changes) & set(SERVER_OWNED_FIELDS))
    if unsafe:
        raise WorkValidationError(f"read-only fields cannot be set: {', '.join(unsafe)}")
    unknown = sorted(set(changes) - set(MUTABLE_FIELDS))
    if unknown:
        raise WorkValidationError(f"unknown fields: {', '.join(unknown)}")
    normalized: dict[str, Any] = {}
    for field, value in changes.items():
        if field in TEXT_FIELDS:
            normalized[field] = _normalize_text(value, field)
        elif field in TIMESTAMP_FIELDS:
            normalized[field] = _normalize_timestamp(value, field)
        elif field == "status":
            if value not in STATUSES:
                raise WorkValidationError(f"status must be one of: {', '.join(STATUSES)}")
            normalized[field] = value
        elif field == "priority":
            if value not in PRIORITIES:
                raise WorkValidationError(f"priority must be one of: {', '.join(PRIORITIES)}")
            normalized[field] = value
        elif field in ("requested_by", "assigned_to"):
            normalized[field] = _normalize_actor(value, field)
        elif field == "parent_id":
            if value is None or (isinstance(value, str) and not value.strip()):
                normalized[field] = None
            elif not isinstance(value, str) or not _ITEM_ID.fullmatch(value.strip()):
                raise WorkValidationError("parent_id must be a work item identifier")
            else:
                normalized[field] = value.strip()
    return normalized


def _blank_item() -> dict[str, Any]:
    return {
        "id": "",
        "title": "",
        "detail": None,
        "status": "backlog",
        "priority": "normal",
        "requested_by": "",
        "assigned_to": "",
        "parent_id": None,
        "progress_summary": "",
        "next_action": "",
        "blocker": None,
        "created_at": "",
        "updated_at": "",
        "started_at": None,
        "completed_at": None,
        "due_at": None,
        "source_ref": None,
        "archived_at": None,
        "revision": 0,
    }


def empty_document() -> dict[str, Any]:
    return {
        "version": DOCUMENT_VERSION,
        "revision": 0,
        "updated_at": None,
        "items": [],
        "migrated_from": None,
    }


# The exact v1 shapes.  A stored document must carry these keys and no others.
DOCUMENT_FIELDS = frozenset({"version", "revision", "updated_at", "items"})
STORED_ITEM_FIELDS = frozenset(_blank_item())


def _corrupt(reason: str) -> WorkCorruptionError:
    return WorkCorruptionError(f"work store is corrupt and was left unchanged: {reason}")


def _stored_timestamp(value: Any, where: str, *, optional: bool) -> datetime | None:
    """Validate a stored timestamp exactly as written, without repairing it."""
    if value is None:
        if optional:
            return None
        raise _corrupt(f"{where} must be a timestamp")
    if not isinstance(value, str) or len(value) > 64:
        raise _corrupt(f"{where} must be a timestamp string")
    text = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as error:
        raise _corrupt(f"{where} is not an ISO 8601 timestamp") from error
    if parsed.tzinfo is None:
        raise _corrupt(f"{where} must carry a UTC offset")
    return parsed


def _validate_stored_item(
    item: Any, index: int, *, statuses: Sequence[str] = STATUSES
) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise _corrupt(f"items[{index}] must be an object")
    missing = sorted(STORED_ITEM_FIELDS - set(item))
    unknown = sorted(set(item) - STORED_ITEM_FIELDS)
    if missing:
        raise _corrupt(f"items[{index}] is missing fields: {', '.join(missing)}")
    if unknown:
        raise _corrupt(f"items[{index}] has unknown fields: {', '.join(unknown)}")

    identifier = item["id"]
    if not isinstance(identifier, str) or not _ITEM_ID.fullmatch(identifier):
        raise _corrupt(f"items[{index}].id is not a work item identifier")
    where = f"item {identifier}"

    revision = item["revision"]
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise _corrupt(f"{where}.revision must be an integer of at least 1")
    if item["status"] not in statuses:
        raise _corrupt(f"{where}.status is not one of: {', '.join(statuses)}")
    if item["priority"] not in PRIORITIES:
        raise _corrupt(f"{where}.priority is not one of: {', '.join(PRIORITIES)}")
    for field in ("requested_by", "assigned_to"):
        value = item[field]
        if not isinstance(value, str) or not _ACTOR.fullmatch(value):
            raise _corrupt(f"{where}.{field} is not an identifier")
    for field, (minimum, maximum) in TEXT_FIELDS.items():
        value = item[field]
        if value is None:
            if field not in OPTIONAL_TEXT_FIELDS:
                raise _corrupt(f"{where}.{field} must be a string")
            continue
        if not isinstance(value, str):
            raise _corrupt(f"{where}.{field} must be a string")
        if len(value.strip()) < minimum:
            raise _corrupt(f"{where}.{field} must not be empty")
        if len(value) > maximum:
            raise _corrupt(f"{where}.{field} is longer than {maximum} characters")
    parent_id = item["parent_id"]
    if parent_id is not None and (
        not isinstance(parent_id, str) or not _ITEM_ID.fullmatch(parent_id)
    ):
        raise _corrupt(f"{where}.parent_id is not a work item identifier")

    created = _stored_timestamp(item["created_at"], f"{where}.created_at", optional=False)
    updated = _stored_timestamp(item["updated_at"], f"{where}.updated_at", optional=False)
    for field in TIMESTAMP_FIELDS:
        _stored_timestamp(item[field], f"{where}.{field}", optional=True)
    archived = _stored_timestamp(item["archived_at"], f"{where}.archived_at", optional=True)
    # Only the stamps the store owns can be checked for ordering.  due_at,
    # started_at, and completed_at are caller-settable and may legitimately be
    # backdated, so those get format validation alone.
    assert created is not None and updated is not None
    if updated < created:
        raise _corrupt(f"{where}.updated_at precedes created_at")
    if archived is not None and not created <= archived <= updated:
        raise _corrupt(f"{where}.archived_at is outside created_at..updated_at")
    return item


def _validate_stored_graph(items: Sequence[Mapping[str, Any]]) -> None:
    by_id: dict[str, Mapping[str, Any]] = {}
    for item in items:
        if item["id"] in by_id:
            raise _corrupt(f"duplicate work item id: {item['id']}")
        by_id[item["id"]] = item
    for item in items:
        parent_id = item["parent_id"]
        if parent_id is None:
            continue
        if parent_id == item["id"]:
            raise _corrupt(f"item {item['id']} is its own parent")
        parent = by_id.get(parent_id)
        if parent is None:
            raise _corrupt(f"item {item['id']} references a missing parent: {parent_id}")
        if item["archived_at"] is None and parent["archived_at"] is not None:
            # Archiving a parent requires archiving its live children first, so
            # a live child of an archived parent cannot have been written here.
            raise _corrupt(f"item {item['id']} has an archived parent: {parent_id}")
        seen = {item["id"]}
        cursor: str | None = parent_id
        depth = 0
        while cursor is not None:
            if cursor in seen:
                raise _corrupt(f"parent cycle through item {item['id']}")
            seen.add(cursor)
            depth += 1
            if depth > MAX_PARENT_DEPTH:
                raise _corrupt(
                    f"item {item['id']} has a parent chain deeper than {MAX_PARENT_DEPTH} levels"
                )
            ancestor = by_id.get(cursor)
            cursor = ancestor["parent_id"] if ancestor else None


def _load_document(
    parsed: Mapping[str, Any], *, version: int, statuses: Sequence[str]
) -> dict[str, Any]:
    """Accept a document exactly as stored, or reject the whole file.

    Each version validates against the enum *that version* could have written,
    so widening the enum later can never retroactively bless a file that was
    already invalid when it was written.
    """
    missing = sorted(DOCUMENT_FIELDS - set(parsed))
    unknown = sorted(set(parsed) - DOCUMENT_FIELDS)
    if missing:
        raise _corrupt(f"document is missing fields: {', '.join(missing)}")
    if unknown:
        raise _corrupt(f"document has unknown fields: {', '.join(unknown)}")
    revision = parsed["revision"]
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
        raise _corrupt("document revision must be a non-negative integer")
    items = parsed["items"]
    if not isinstance(items, list):
        raise _corrupt("document items must be a list")
    if revision == 0:
        if parsed["updated_at"] is not None:
            raise _corrupt("document updated_at must be null before the first write")
    else:
        _stored_timestamp(parsed["updated_at"], "document updated_at", optional=False)
    validated = [
        _validate_stored_item(item, index, statuses=statuses) for index, item in enumerate(items)
    ]
    _validate_stored_graph(validated)
    return {
        "version": version,
        "revision": revision,
        "updated_at": parsed["updated_at"],
        "items": validated,
    }


def _load_v1_document(parsed: Mapping[str, Any]) -> dict[str, Any]:
    return _load_document(parsed, version=1, statuses=V1_STATUSES)


def _load_v2_document(parsed: Mapping[str, Any]) -> dict[str, Any]:
    return _load_document(parsed, version=2, statuses=STATUSES)


def _migrate_v1_to_v2(document: dict[str, Any]) -> dict[str, Any]:
    """v1 -> v2: the queue gains an explicit `todo` stage between ready and backlog.

    No stored value changes and nothing is dropped.  Every v1 status keeps its
    exact meaning in v2, and `todo` is new, so no item can need relabelling --
    which is precisely why this migration is written out rather than assumed:
    it states that the widening is total and lossless, and it moves the version
    marker deliberately.  The upgraded document is only persisted by the next
    ordinary write; reading never rewrites the file.
    """
    return {**document, "version": 2}


# Reading is dispatched on the stored version.  A future version 3 registers its
# own strict loader here plus a _MIGRATIONS[2] that rewrites a validated v2
# document into v3 shape.  Bridging versions is always an explicit migration,
# never a silent retention or drop of fields the current schema does not know.
_LOADERS: dict[int, Any] = {1: _load_v1_document, 2: _load_v2_document}
_MIGRATIONS: dict[int, Any] = {1: _migrate_v1_to_v2}


def _migrate(document: dict[str, Any]) -> dict[str, Any]:
    while document["version"] < DOCUMENT_VERSION:
        migrate = _MIGRATIONS.get(document["version"])
        if migrate is None:
            raise _corrupt(
                f"no migration from work store version {document['version']} to {DOCUMENT_VERSION}"
            )
        document = migrate(document)
    return document


class WorkStore:
    """Read/modify/write access to the shared delegated-work document."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.directory = self.root / "work"
        self.items_path = self.directory / "items.json"
        self.history_path = self.directory / "history.jsonl"
        self.lock_path = self.directory / "items.lock"
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.chmod(0o700)

    @classmethod
    def from_environment(cls) -> "WorkStore":
        configured = os.environ.get("APP_CONFIG_ROOT")
        root = Path(configured) if configured else Path.home() / ".config/hk-work-assistant"
        return cls(root)

    # ---------------------------------------------------------------- reading

    def read_document(self) -> dict[str, Any]:
        """Return the fully validated stored document, or raise untouched.

        Every mutation starts here, so a document that fails validation stops
        the write before anything is committed or appended to the history.
        """
        if not self.items_path.exists():
            return empty_document()
        try:
            raw = self.items_path.read_text(encoding="utf-8")
        except OSError as error:
            raise WorkCorruptionError(f"work store is unreadable: {self.items_path}") from error
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as error:
            raise WorkCorruptionError(
                f"work store is not valid JSON and was left unchanged: {self.items_path}"
            ) from error
        if not isinstance(parsed, dict):
            raise _corrupt("document must be a JSON object")
        version = parsed.get("version")
        loader = _LOADERS.get(version) if isinstance(version, int) and not isinstance(version, bool) else None
        if loader is None:
            raise WorkCorruptionError(
                f"unsupported work store version {version!r}; expected {DOCUMENT_VERSION}"
            )
        stored = loader(parsed)
        stored_version = stored["version"]
        document = _migrate(stored)
        # The file on disk is still at its own version until the next write.
        # Saying so lets a reader show that an upgrade is pending instead of
        # quietly presenting a migrated view as if it had been stored.
        document["migrated_from"] = stored_version if stored_version != DOCUMENT_VERSION else None
        return document

    def list_items(
        self,
        *,
        include_archived: bool = False,
        statuses: Sequence[str] | None = None,
        assigned_to: str | None = None,
        parent_id: str | None = None,
    ) -> dict[str, Any]:
        document = self.read_document()
        wanted = set(statuses or ())
        if wanted - set(STATUSES):
            raise WorkValidationError(f"status must be one of: {', '.join(STATUSES)}")
        assignee = _normalize_actor(assigned_to, "assigned_to") if assigned_to else None
        available = [
            item
            for item in document["items"]
            if include_archived or item["archived_at"] is None
        ]
        selected = [
            item
            for item in available
            if (not wanted or item["status"] in wanted)
            and (assignee is None or item["assigned_to"] == assignee)
            and (parent_id is None or item["parent_id"] == parent_id)
        ]
        # Stable sorts: most recently updated first, then grouped by priority.
        selected.sort(key=lambda item: (item["updated_at"] or "", item["id"]), reverse=True)
        selected.sort(key=lambda item: PRIORITY_RANK.get(item["priority"], len(PRIORITIES)))
        return {
            "version": document["version"],
            "migrated_from": document.get("migrated_from"),
            "revision": document["revision"],
            "updated_at": document["updated_at"],
            "count": len(selected),
            # `total` and `status_counts` describe the whole visible set before
            # the status and assignee filters.  A board that renders `items`
            # can compare against them and say how many it is not showing,
            # instead of presenting a filtered count as the complete picture.
            "total": len(available),
            "status_counts": {
                status: sum(1 for item in available if item["status"] == status)
                for status in STATUSES
                if any(item["status"] == status for item in available)
            },
            "items": selected,
        }

    def get_item(self, item_id: str) -> dict[str, Any]:
        for item in self.read_document()["items"]:
            if item["id"] == item_id:
                return item
        raise WorkNotFoundError(f"work item not found: {item_id}")

    def read_history(self, *, limit: int = 100, item_id: str | None = None) -> list[dict[str, Any]]:
        if not self.history_path.exists():
            return []
        entries: list[dict[str, Any]] = []
        for line in self.history_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if item_id is None or entry.get("item_id") == item_id:
                entries.append(entry)
        return entries[-limit:][::-1]

    # --------------------------------------------------------------- timeline

    # Fields the history stream only started carrying once the cowork timeline
    # existed. An older entry does not have them, and the reader says so
    # instead of rendering an empty value as if it were an observed one.
    TIMELINE_FIELDS = ("requested_by", "assigned_to", "directed_by", "phase",
                       "session_id", "summary", "receipt")

    def read_timeline(self, item_id: str, *, limit: int = 200) -> dict[str, Any]:
        """One work item's activity, oldest first, with actors resolved.

        Three sources are merged and each entry says which one it came from:
        the work store's own history, the cowork change receipts, and the
        cowork event stream. Nothing is rewritten -- an entry written before a
        field existed is marked ``legacy`` and that field is reported as
        unknown rather than empty.
        """
        item = self.get_item(item_id)
        entries: list[dict[str, Any]] = []

        for raw in self._history_for(item_id):
            at = raw.get("at")
            present = {key for key in self.TIMELINE_FIELDS if key in raw}
            entry = {
                "source": "work_history",
                "at": at,
                "action": raw.get("action"),
                "revision": raw.get("revision"),
                "status": raw.get("status"),
                "status_from": raw.get("status_from"),
                "fields": raw.get("fields") or [],
                "actor": resolve_actor(raw.get("actor"), at=at),
                "directed_by": (
                    resolve_actor(raw.get("directed_by"), at=at)
                    if "directed_by" in raw
                    else None
                ),
                "requested_by": raw.get("requested_by"),
                "assigned_to": raw.get("assigned_to"),
                "phase": raw.get("phase"),
                "session_id": raw.get("session_id"),
                "summary": raw.get("summary"),
                "receipt": raw.get("receipt"),
                "record_schema": "current" if present else "legacy",
                "unknown_fields": sorted(set(self.TIMELINE_FIELDS) - present),
            }
            entries.append(entry)

        entries.extend(self._cowork_entries(item_id))
        entries.sort(key=lambda e: (str(e.get("at") or ""), e.get("source") or ""))
        return {
            "item_id": item["id"],
            "title": item["title"],
            "requested_by": item["requested_by"],
            "assigned_to": item["assigned_to"],
            "status": item["status"],
            "revision": item["revision"],
            "parent_id": item["parent_id"],
            "entries": entries[-limit:],
            "truncated": len(entries) > limit,
            "count": len(entries),
        }

    def _history_for(self, item_id: str) -> list[dict[str, Any]]:
        if not self.history_path.exists():
            return []
        found: list[dict[str, Any]] = []
        for line in self.history_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(entry, Mapping) and entry.get("item_id") == item_id:
                found.append(dict(entry))
        return found

    def _cowork_entries(self, item_id: str) -> list[dict[str, Any]]:
        """Change receipts and events recorded for this item under cowork/.

        Read-only and best-effort: a missing or unreadable cowork tree yields
        no entries rather than failing the timeline.
        """
        cowork = self.root / "cowork"
        entries: list[dict[str, Any]] = []

        events_path = cowork / "events.jsonl"
        try:
            lines = events_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
        for line in lines:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, Mapping) or event.get("work_id") != item_id:
                continue
            at = event.get("at")
            entries.append({
                "source": "cowork_event",
                "at": at,
                "action": event.get("event"),
                "revision": event.get("revision"),
                "phase": event.get("event"),
                "summary": event.get("summary"),
                "handoff_id": event.get("handoff_id"),
                "actor": resolve_actor("moa", at=at),
                "record_schema": "current",
                "unknown_fields": [],
            })

        handoffs = cowork / "handoffs"
        try:
            names = sorted(
                name for name in os.listdir(handoffs)
                if name.endswith(".json") and item_id in name
            )
        except OSError:
            names = []
        for name in names:
            try:
                payload = json.loads((handoffs / name).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(payload, Mapping) or payload.get("work_id") != item_id:
                continue
            at = payload.get("created_at")
            item_block = payload.get("item") or {}
            entries.append({
                "source": "cowork_handoff",
                "at": at,
                "action": "handoff",
                "revision": item_block.get("revision_after") if isinstance(item_block, Mapping) else None,
                "phase": payload.get("outcome"),
                "summary": payload.get("next_action"),
                "handoff_id": payload.get("handoff_id"),
                # A local identifier, never an absolute path.
                "receipt": f"cowork/handoffs/{name}",
                "actor": resolve_actor(payload.get("actor"), at=at),
                "record_schema": "current",
                "unknown_fields": [],
            })
        return entries

    # ---------------------------------------------------------------- writing

    def create_item(
        self,
        fields: Mapping[str, Any],
        *,
        actor: str,
        context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        actor_name = _normalize_actor(actor, "actor")
        changes = _normalize_changes(fields)
        _require_create_fields(changes)
        entry_context = _normalize_context(context)
        with self._locked():
            document = self.read_document()
            return self._create_within(
                document, changes, actor=actor_name, context=entry_context
            )

    def update_item(
        self,
        item_id: str,
        changes: Mapping[str, Any],
        *,
        actor: str,
        expected_revision: int | None = None,
        expected_updated_at: str | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        actor_name = _normalize_actor(actor, "actor")
        normalized = _normalize_changes(changes)
        if not normalized:
            raise WorkValidationError("no fields to update")
        entry_context = _normalize_context(context)
        with self._locked():
            document = self.read_document()
            item = _find(document["items"], item_id)
            _check_expectations(item, expected_revision, expected_updated_at)
            return self._update_within(
                document, item, normalized, actor=actor_name, context=entry_context
            )

    def upsert_item(
        self,
        fields: Mapping[str, Any],
        *,
        actor: str,
        item_id: str | None = None,
        source_ref: str | None = None,
        expected_revision: int | None = None,
        expected_updated_at: str | None = None,
        force_overwrite: bool = False,
    ) -> dict[str, Any]:
        """Create by identity, or update the item that already carries it.

        Creating needs no expectation.  Updating an existing match does: pass
        ``expected_revision`` (preferred) or ``expected_updated_at`` so a
        concurrent human or agent edit is reported instead of overwritten.  A
        mechanical caller that genuinely wants last-write-wins must say so with
        ``force_overwrite``; an expectation supplied alongside it is still
        checked, because force only waives the requirement to supply one.
        """
        actor_name = _normalize_actor(actor, "actor")
        if not item_id and not source_ref:
            raise WorkValidationError("upsert requires an id or a source_ref")
        reference = _normalize_text(source_ref, "source_ref") if source_ref else None
        expected = expected_revision is not None or expected_updated_at is not None
        with self._locked():
            document = self.read_document()
            existing = _match_identity(document["items"], item_id=item_id, source_ref=reference)
            if existing is None:
                if expected:
                    raise WorkValidationError(
                        "no existing work item matches, so there is nothing to expect a revision of"
                    )
                payload = dict(fields)
                if reference is not None and "source_ref" not in payload:
                    payload["source_ref"] = reference
                changes = _normalize_changes(payload)
                _require_create_fields(changes)
                item = self._create_within(document, changes, actor=actor_name)
                return {"created": True, "item": item}
            if not expected and not force_overwrite:
                raise WorkValidationError(
                    f"work item {existing['id']} already exists at revision {existing['revision']}; "
                    "pass expected_revision or expected_updated_at, or force_overwrite for "
                    "deliberate last-write-wins"
                )
            _check_expectations(existing, expected_revision, expected_updated_at)
            updates = _normalize_changes(
                {key: value for key, value in fields.items() if key != "source_ref"}
            )
            if not updates:
                return {"created": False, "item": existing}
            item = self._update_within(document, existing, updates, actor=actor_name)
            return {"created": False, "item": item}

    def archive_item(
        self,
        item_id: str,
        *,
        actor: str,
        expected_revision: int | None = None,
        expected_updated_at: str | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Soft delete: the item stays in the document with archived_at set."""
        actor_name = _normalize_actor(actor, "actor")
        entry_context = _normalize_context(context)
        with self._locked():
            document = self.read_document()
            item = _find(document["items"], item_id)
            _check_expectations(item, expected_revision, expected_updated_at)
            if item["archived_at"] is not None:
                raise WorkConflictError(f"work item is already archived: {item_id}")
            children = [
                other["id"]
                for other in document["items"]
                if other["parent_id"] == item_id and other["archived_at"] is None
            ]
            if children:
                raise WorkConflictError(
                    f"archive the child items first: {', '.join(sorted(children))}"
                )
            now = _utc_now()
            item["archived_at"] = now
            item["updated_at"] = now
            item["revision"] = int(item["revision"]) + 1
            self._commit(document, now)
            self._append_history(
                "work.archived",
                item,
                actor=actor_name,
                fields=["archived_at"],
                context=entry_context,
            )
            return item

    # ----------------------------------------------------------- internals

    def _create_within(
        self,
        document: dict[str, Any],
        changes: Mapping[str, Any],
        *,
        actor: str,
        context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Append one validated item.  The store lock must already be held."""
        if len(document["items"]) >= MAX_ITEMS:
            raise WorkValidationError(f"work store already holds {MAX_ITEMS} items")
        now = _utc_now()
        item = _blank_item()
        item.update(changes)
        item["id"] = _new_item_id()
        item["created_at"] = now
        item["updated_at"] = now
        item["revision"] = 1
        _apply_status_timestamps(item, previous_status=None, explicit=set(changes), now=now)
        _check_parent(document["items"], item)
        document["items"].append(item)
        self._commit(document, now)
        self._append_history(
            "work.created", item, actor=actor, fields=sorted(changes), context=context
        )
        return item

    def _update_within(
        self,
        document: dict[str, Any],
        item: dict[str, Any],
        changes: Mapping[str, Any],
        *,
        actor: str,
        context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Apply validated changes in place.  The store lock must already be held."""
        if item["archived_at"] is not None:
            raise WorkConflictError(f"work item is archived: {item['id']}")
        previous_status = item["status"]
        now = _utc_now()
        item.update(changes)
        _apply_status_timestamps(
            item, previous_status=previous_status, explicit=set(changes), now=now
        )
        _check_parent(document["items"], item)
        item["updated_at"] = now
        item["revision"] = int(item["revision"]) + 1
        self._commit(document, now)
        self._append_history(
            "work.updated",
            item,
            actor=actor,
            fields=sorted(changes),
            status_from=previous_status,
            context=context,
        )
        return item

    def _commit(self, document: Mapping[str, Any], now: str) -> None:
        payload = {
            "version": DOCUMENT_VERSION,
            "revision": int(document["revision"]) + 1,
            "updated_at": now,
            "items": list(document["items"]),
        }
        _atomic_private_write(
            self.items_path,
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )

    def _append_history(
        self,
        action: str,
        item: Mapping[str, Any],
        *,
        actor: str,
        fields: Sequence[str],
        status_from: str | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> None:
        """Record that a change happened.

        Identifiers, field names, status enum values, and the caller's own
        timeline context. The item's free text -- `detail`, `progress_summary`,
        `blocker`, `next_action` -- is still never written here, so the history
        stream cannot become a copy of the board.
        """
        entry = {
            "at": _utc_now(),
            "action": action,
            "actor": actor,
            "item_id": item["id"],
            "revision": item["revision"],
            "fields": sorted(fields),
            "status": item["status"],
            # Who asked and who is carrying it, taken from the item itself so
            # a timeline never has to join back to the document to be read.
            "requested_by": item.get("requested_by"),
            "assigned_to": item.get("assigned_to"),
        }
        if status_from is not None and status_from != item["status"]:
            entry["status_from"] = status_from
        entry.update(context or {})
        descriptor = os.open(self.history_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            with os.fdopen(descriptor, "a", encoding="utf-8") as stream:
                stream.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            self.history_path.chmod(0o600)

    @contextmanager
    def _locked(self, *, timeout: float = 10.0) -> Iterator[None]:
        """Serialize read-modify-write cycles across processes and threads.

        flock is held per open file description, so this must never be nested:
        every public mutation takes the lock exactly once and calls the
        ``_within`` helpers underneath it.
        """
        handle = open(self.lock_path, "a+", encoding="utf-8")
        try:
            os.fchmod(handle.fileno(), 0o600)
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as error:
                    if time.monotonic() >= deadline:
                        raise WorkLockTimeout(
                            "another writer is holding the work store lock"
                        ) from error
                    time.sleep(0.02)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def status_metadata() -> dict[str, Any]:
    """Everything a client needs to lay out the board without hard-coding it.

    The page derives its columns from this, so the board and the schema cannot
    drift apart: adding a status without giving it a column is caught here at
    import, not by an operator noticing an item is gone.
    """
    return {
        "document_version": DOCUMENT_VERSION,
        "statuses": list(STATUSES),
        "priorities": list(PRIORITIES),
        "queue_stages": list(QUEUE_STAGES),
        "supporting_statuses": list(SUPPORTING_STATUSES),
        "terminal_statuses": sorted(TERMINAL_STATUSES),
        "status_labels": dict(STATUS_LABELS),
        "status_groups": dict(STATUS_GROUPS),
        "columns": [
            {
                "key": column["key"],
                "title": column["title"],
                "statuses": list(column["statuses"]),
                "recent_days": column["recent_days"],
            }
            for column in BOARD_COLUMNS
        ],
        "residue_column": {"key": RESIDUE_COLUMN_KEY, "title": RESIDUE_COLUMN_TITLE},
    }


def group_into_columns(
    items: Sequence[Mapping[str, Any]], *, now: datetime | None = None
) -> list[dict[str, Any]]:
    """Lay items out on the board, with every leftover surfaced explicitly.

    The board is a total partition: an item whose status no column claims goes
    to the residue column rather than being dropped.  That column is the whole
    point -- a status the layout has not been taught about must be visible and
    loud, not invisible.
    """
    moment = now or datetime.now(timezone.utc)
    claimed = {status for column in BOARD_COLUMNS for status in column["statuses"]}
    placed: set[str] = set()
    columns: list[dict[str, Any]] = []
    for column in BOARD_COLUMNS:
        selected = []
        for item in items:
            if item["status"] not in column["statuses"]:
                continue
            if column["recent_days"] is not None and not _within_recent_days(
                item, column["recent_days"], moment
            ):
                continue
            selected.append(item)
            placed.add(item["id"])
        columns.append(
            {
                "key": column["key"],
                "title": column["title"],
                "statuses": list(column["statuses"]),
                "recent_days": column["recent_days"],
                "count": len(selected),
                "items": selected,
            }
        )
    # Two ways an item can be left over: a status no column claims, and an item
    # a dated column aged out.  Only the first is a layout defect; the second is
    # deliberate, so it is counted separately and not shouted about.
    residue = [item for item in items if item["id"] not in placed]
    unclaimed = [item for item in residue if item["status"] not in claimed]
    columns.append(
        {
            "key": RESIDUE_COLUMN_KEY,
            "title": RESIDUE_COLUMN_TITLE,
            "statuses": sorted({item["status"] for item in unclaimed}),
            "recent_days": None,
            "count": len(unclaimed),
            "items": unclaimed,
            "aged_out": len(residue) - len(unclaimed),
        }
    )
    return columns


def _within_recent_days(item: Mapping[str, Any], days: int, now: datetime) -> bool:
    stamp = item.get("completed_at") or item.get("updated_at")
    try:
        moment = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        # An unparseable stamp must not delete the item from the board.
        return True
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment >= now - timedelta(days=days)


def _validate_board_columns() -> None:
    """The board must claim every status exactly once."""
    claimed: list[str] = [status for column in BOARD_COLUMNS for status in column["statuses"]]
    duplicated = sorted({status for status in claimed if claimed.count(status) > 1})
    if duplicated:
        raise RuntimeError(f"board columns claim a status twice: {', '.join(duplicated)}")
    missing = [status for status in STATUSES if status not in claimed]
    if missing:
        raise RuntimeError(
            f"board columns do not claim: {', '.join(missing)}; give every status a column"
        )
    unknown = sorted(set(claimed) - set(STATUSES))
    if unknown:
        raise RuntimeError(f"board columns claim unknown statuses: {', '.join(unknown)}")


_validate_board_columns()


def _require_create_fields(changes: Mapping[str, Any]) -> None:
    missing = [name for name in REQUIRED_ON_CREATE if not changes.get(name)]
    if missing:
        raise WorkValidationError(f"missing required fields: {', '.join(missing)}")


def _match_identity(
    items: Sequence[Mapping[str, Any]], *, item_id: str | None, source_ref: str | None
) -> dict[str, Any] | None:
    if item_id:
        item = _find(items, item_id)
        if item["archived_at"] is not None:
            raise WorkConflictError(f"work item is archived: {item_id}")
        return item
    matches = [
        item for item in items if item["source_ref"] == source_ref and item["archived_at"] is None
    ]
    if len(matches) > 1:
        raise WorkConflictError(f"source_ref matches {len(matches)} items: {source_ref}")
    return matches[0] if matches else None  # type: ignore[return-value]


def _check_parent(items: Iterable[Mapping[str, Any]], item: Mapping[str, Any]) -> None:
    """Reject a missing, archived, self-referential, or cyclic parent."""
    parent_id = item["parent_id"]
    if parent_id is None:
        return
    if parent_id == item["id"]:
        raise WorkValidationError("a work item cannot be its own parent")
    by_id = {other["id"]: other for other in items}
    parent = by_id.get(parent_id)
    if parent is None:
        raise WorkValidationError(f"parent work item not found: {parent_id}")
    if parent["archived_at"] is not None:
        raise WorkValidationError(f"parent work item is archived: {parent_id}")
    seen = {item["id"]}
    cursor: str | None = parent_id
    depth = 0
    while cursor is not None:
        if cursor in seen:
            raise WorkValidationError("parent_id would create a cycle")
        seen.add(cursor)
        depth += 1
        if depth > MAX_PARENT_DEPTH:
            raise WorkValidationError(f"parent chain is deeper than {MAX_PARENT_DEPTH} levels")
        ancestor = by_id.get(cursor)
        cursor = ancestor["parent_id"] if ancestor else None


def _find(items: Sequence[Mapping[str, Any]], item_id: str) -> dict[str, Any]:
    for item in items:
        if item["id"] == item_id:
            return item  # type: ignore[return-value]
    raise WorkNotFoundError(f"work item not found: {item_id}")


def _check_expectations(
    item: Mapping[str, Any], expected_revision: int | None, expected_updated_at: str | None
) -> None:
    if expected_revision is not None:
        if not isinstance(expected_revision, int) or isinstance(expected_revision, bool):
            raise WorkValidationError("expected_revision must be an integer")
        if int(item["revision"]) != expected_revision:
            raise WorkConflictError(
                f"work item is at revision {item['revision']}, not {expected_revision}"
            )
    if expected_updated_at is not None:
        expected = _normalize_timestamp(expected_updated_at, "expected_updated_at")
        if expected != item["updated_at"]:
            raise WorkConflictError(
                f"work item was updated at {item['updated_at']}, not {expected}"
            )


def _apply_status_timestamps(
    item: dict[str, Any], *, previous_status: str | None, explicit: set[str], now: str
) -> None:
    status = item["status"]
    if status == "in_progress" and item["started_at"] is None:
        item["started_at"] = now
    if status in TERMINAL_STATUSES and item["completed_at"] is None:
        item["completed_at"] = now
    if (
        status not in TERMINAL_STATUSES
        and previous_status in TERMINAL_STATUSES
        and "completed_at" not in explicit
    ):
        item["completed_at"] = None
