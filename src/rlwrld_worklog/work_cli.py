"""Local `worklog work` commands for the delegated-work board.

Agents and scripts run these on the same machine as the backoffice.  They use
the same WorkStore, the same validation, and the same change history as the web
API, but need no web session, so a headless agent can record its own progress.

Standard output always holds exactly one JSON document: ``{"ok": true, ...}`` on
success or ``{"ok": false, "error": {...}}`` on failure.  Exit codes are 0 on
success, and 2/3/4/5/6 for validation, missing item, conflict, corruption, and
lock timeout respectively.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from .work_store import (
    PRIORITIES,
    describe_roles,
    STATUSES,
    WorkConflictError,
    WorkCorruptionError,
    WorkLockTimeout,
    WorkNotFoundError,
    WorkStore,
    WorkStoreError,
    WorkValidationError,
    group_into_columns,
    status_metadata,
)


EXIT_CODES: tuple[tuple[type[Exception], str, int], ...] = (
    (WorkValidationError, "validation", 2),
    (WorkNotFoundError, "not_found", 3),
    (WorkConflictError, "conflict", 4),
    (WorkCorruptionError, "corruption", 5),
    (WorkLockTimeout, "lock_timeout", 6),
)

# CLI flag -> work item field.  Only these may be written from the CLI.
FIELD_FLAGS: tuple[tuple[str, str], ...] = (
    ("--title", "title"),
    ("--detail", "detail"),
    ("--status", "status"),
    ("--priority", "priority"),
    ("--requested-by", "requested_by"),
    ("--assigned-to", "assigned_to"),
    ("--parent-id", "parent_id"),
    ("--progress", "progress_summary"),
    ("--next-action", "next_action"),
    ("--blocker", "blocker"),
    ("--due-at", "due_at"),
    ("--started-at", "started_at"),
    ("--completed-at", "completed_at"),
    ("--source-ref", "source_ref"),
)
CLEARABLE_FIELDS = (
    "detail",
    "blocker",
    "parent_id",
    "due_at",
    "started_at",
    "completed_at",
    "source_ref",
)


def add_work_parser(subparsers: Any) -> None:
    work = subparsers.add_parser("work", help="Track delegated work in the local shared store")
    commands = work.add_subparsers(dest="work_command", required=True)

    create = commands.add_parser("create", help="Create one work item")
    _add_common(create)
    _add_field_flags(create)

    upsert = commands.add_parser("upsert", help="Create, or update the item with this identity")
    _add_common(upsert)
    _add_field_flags(upsert)
    upsert.add_argument("--id", dest="item_id", help="Existing work item identifier")
    upsert.add_argument("--match-source-ref", help="Identity key when --id is unknown")
    _add_expectations(upsert)
    upsert.add_argument(
        "--force-overwrite",
        action="store_true",
        help="Deliberate last-write-wins when the item already exists. Without it, "
        "updating an existing match requires --expected-revision or --expected-updated-at.",
    )

    update = commands.add_parser("update", help="Update one work item")
    _add_common(update)
    update.add_argument("item_id")
    _add_field_flags(update)
    _add_expectations(update)

    archive = commands.add_parser("archive", help="Soft delete one work item")
    _add_common(archive)
    archive.add_argument("item_id")
    _add_expectations(archive)

    listing = commands.add_parser("list", help="List work items")
    _add_common(listing)
    listing.add_argument("--status", action="append", default=[], choices=list(STATUSES))
    listing.add_argument("--assigned-to")
    listing.add_argument("--parent-id")
    listing.add_argument("--include-archived", action="store_true")

    board = commands.add_parser(
        "board", help="Group work items into the four-stage queue plus supporting columns"
    )
    _add_common(board)
    board.add_argument("--assigned-to")
    board.add_argument("--include-archived", action="store_true")

    meta = commands.add_parser("meta", help="Show the status schema and board layout")
    _add_common(meta)

    apply_outbox = commands.add_parser(
        "apply-outbox",
        help="Apply queued ticket edits dropped as JSON files, and file the results",
    )
    apply_outbox.add_argument("--outbox", required=True, help="Directory holding the queued files")
    apply_outbox.add_argument(
        "--limit", type=int, default=50, help="Most files to apply in one pass"
    )
    _add_common(apply_outbox)

    agent_token = commands.add_parser(
        "agent-token", help="Issue a long-lived board session for one agent"
    )
    agent_token.add_argument("name", help="Agent name, one of the known roster")
    _add_common(agent_token)

    agent_revoke = commands.add_parser(
        "agent-revoke", help="End one agent's sessions without touching the others"
    )
    agent_revoke.add_argument("name", help="Agent name, one of the known roster")
    _add_common(agent_revoke)

    agent_list = commands.add_parser(
        "agent-list", help="Show the agent roster and how often each was revoked"
    )
    _add_common(agent_list)

    show = commands.add_parser("show", help="Show one work item")
    _add_common(show)
    show.add_argument("item_id")

    history = commands.add_parser("history", help="Read the append-only change history")
    _add_common(history)
    history.add_argument("--limit", type=int, default=100)
    history.add_argument("--item-id")

    timeline = commands.add_parser(
        "timeline", help="Show one item's activity with actors resolved and receipts linked"
    )
    _add_common(timeline)
    timeline.add_argument("item_id")
    timeline.add_argument("--limit", type=int, default=200)


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config-root",
        type=Path,
        default=None,
        help="APP_CONFIG_ROOT override; the store never reads or writes anywhere else",
    )
    parser.add_argument(
        "--actor",
        default=None,
        help="Who is making the change (default: WORKLOG_ACTOR, else local-cli)",
    )


def _add_field_flags(parser: argparse.ArgumentParser) -> None:
    for flag, field in FIELD_FLAGS:
        if field == "status":
            parser.add_argument(flag, dest=field, default=None, choices=list(STATUSES))
        elif field == "priority":
            parser.add_argument(flag, dest=field, default=None, choices=list(PRIORITIES))
        else:
            parser.add_argument(flag, dest=field, default=None)
    parser.add_argument(
        "--clear",
        action="append",
        default=[],
        choices=list(CLEARABLE_FIELDS),
        help="Set an optional field back to empty. Repeatable.",
    )


def _add_expectations(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--expected-revision",
        type=int,
        default=None,
        help="Fail instead of overwriting a concurrent change",
    )
    parser.add_argument("--expected-updated-at", default=None)


def _store(args: argparse.Namespace) -> WorkStore:
    if args.config_root is not None:
        return WorkStore(args.config_root)
    return WorkStore.from_environment()


def _actor(args: argparse.Namespace) -> str:
    return args.actor or os.environ.get("WORKLOG_ACTOR") or "local-cli"


def _fields(args: argparse.Namespace) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for _, field in FIELD_FLAGS:
        value = getattr(args, field, None)
        if value is not None:
            fields[field] = value
    for field in args.clear:
        fields[field] = None
    return fields


def _emit(payload: Mapping[str, Any], stream: Any = None) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), file=stream or sys.stdout)


def run_work(args: argparse.Namespace) -> int:
    try:
        return _dispatch(args)
    except WorkStoreError as error:
        for error_type, kind, code in EXIT_CODES:
            if isinstance(error, error_type):
                _emit({"ok": False, "error": {"kind": kind, "message": str(error)}})
                return code
        _emit({"ok": False, "error": {"kind": "store", "message": str(error)}})
        return 1
    except ValueError as error:
        # An unknown agent name is a refusal, not a crash. It used to be
        # neither: revoke_agent returned a generation number for a name no
        # token carried, which reads as success.
        #
        # Below WorkStoreError, never above it: WorkValidationError is also a
        # ValueError, so an arm here first swallowed every validated field in
        # the CLI and relabelled it "argument". The store's errors carry their
        # own kinds and exit codes; this arm is only for the refusals that
        # reach us as a plain ValueError, which today means AdminStore's
        # unknown-agent guard.
        _emit({"ok": False, "error": {"kind": "argument", "message": str(error)}})
        return 2


# Only these two fields are taken from a queued file. The others are the
# executor's own account of the work: a queue that could set `status` or
# `progress_summary` would let the requester write the report as well as the
# request, and the board would no longer say who observed what.
OUTBOX_FIELDS = ("next_action", "detail")

# A queued file may also create an item, with `"op": "create"`. Creation is the
# requester's own act, so the fields it may set are the request - what is
# wanted, of whom, by when - and never the account of the work.
#
# `status` is allowed, but only among the stages that mean "nobody has started
# this". A queue that could file an item straight to `in_progress` or `done`
# would let a requester close work no one did, which is the same hole that
# keeping `progress_summary` out of OUTBOX_FIELDS closes for updates.
OUTBOX_CREATE_FIELDS = (
    "title",
    "detail",
    "next_action",
    "assigned_to",
    "priority",
    "due_at",
    "parent_id",
    "source_ref",
    "status",
)
OUTBOX_CREATE_STATUSES = ("backlog", "todo", "ready")


def _outbox_result(name: str, ok: bool, reason: str, **extra: Any) -> dict[str, Any]:
    return {"file": name, "ok": ok, "reason": reason, **extra}


def _apply_one_create(
    store: WorkStore, path: Path, payload: Mapping[str, Any], actor: str
) -> dict[str, Any]:
    """Create one item from a queued file. Never raises."""
    fields = {key: payload[key] for key in OUTBOX_CREATE_FIELDS if key in payload}
    ignored = sorted(set(payload) - set(OUTBOX_CREATE_FIELDS) - {"op", "requested_by"})
    for key, value in fields.items():
        if not isinstance(value, str):
            return _outbox_result(path.name, False, f"{key} must be a string", ignored=ignored)

    status = fields.get("status")
    if status is not None and status not in OUTBOX_CREATE_STATUSES:
        return _outbox_result(
            path.name,
            False,
            f"status {status!r} may not be set on create: only "
            f"{', '.join(OUTBOX_CREATE_STATUSES)}",
            ignored=ignored,
        )

    # The queue's owner is the requester, by construction. A file that names
    # someone else is refused rather than quietly corrected, because a board
    # that misattributes who asked for the work is worse than a rejected file.
    declared = payload.get("requested_by")
    if isinstance(declared, str) and declared.strip() and declared.strip() != actor:
        return _outbox_result(
            path.name,
            False,
            f"requested_by must be {actor!r}: a queued file may not record "
            "someone else as the requester",
            ignored=ignored,
        )
    fields["requested_by"] = actor

    try:
        item = store.create_item(fields, actor=actor)
    except WorkStoreError as error:
        return _outbox_result(
            path.name, False, f"{error.__class__.__name__}: {error}", ignored=ignored
        )
    return _outbox_result(
        path.name,
        True,
        "created",
        work_id=item["id"],
        revision=item["revision"],
        applied=sorted(fields),
        ignored=ignored,
    )


def _apply_one_outbox(store: WorkStore, path: Path, actor: str) -> dict[str, Any]:
    """Apply one queued file. Never raises: every outcome is a filed reason."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as error:
        return _outbox_result(path.name, False, f"unreadable: {error.__class__.__name__}")
    if len(raw.encode("utf-8")) > 64_000:
        return _outbox_result(path.name, False, "file is larger than 64000 bytes")
    try:
        payload = json.loads(raw)
    except ValueError as error:
        # The file is data, never a command. A malformed one is refused, not
        # interpreted and not executed.
        return _outbox_result(path.name, False, f"not JSON: {error}")
    if not isinstance(payload, dict):
        return _outbox_result(path.name, False, "top level must be a JSON object")

    op = payload.get("op", "update")
    if op not in ("update", "create"):
        return _outbox_result(
            path.name, False, f"unknown op {op!r}: expected 'create' or 'update'"
        )
    if op == "create":
        return _apply_one_create(store, path, payload, actor)

    work_id = payload.get("work_id")
    if not isinstance(work_id, str) or not work_id:
        return _outbox_result(path.name, False, "work_id is required")

    fields = {key: payload[key] for key in OUTBOX_FIELDS if key in payload}
    ignored = sorted(set(payload) - set(OUTBOX_FIELDS) - {"work_id", "expected_revision"})
    if not fields:
        return _outbox_result(
            path.name, False, f"nothing to apply: only {', '.join(OUTBOX_FIELDS)} are read",
            ignored=ignored,
        )
    for key, value in fields.items():
        if not isinstance(value, str):
            return _outbox_result(path.name, False, f"{key} must be a string", ignored=ignored)

    expected = payload.get("expected_revision")
    if expected is not None and (not isinstance(expected, int) or isinstance(expected, bool)):
        return _outbox_result(path.name, False, "expected_revision must be an integer or absent")

    try:
        item = store.update_item(work_id, fields, actor=actor, expected_revision=expected)
    except WorkConflictError as error:
        # Deliberately not merged. Merging here would silently overwrite a
        # change the requester never saw, and deciding that is theirs.
        return _outbox_result(
            path.name, False, f"revision conflict, not merged: {error}", ignored=ignored
        )
    except WorkStoreError as error:
        return _outbox_result(
            path.name, False, f"{error.__class__.__name__}: {error}", ignored=ignored
        )
    return _outbox_result(
        path.name, True, "applied", work_id=work_id,
        revision=item["revision"], applied=sorted(fields), ignored=ignored,
    )


def _apply_outbox(args: argparse.Namespace) -> int:
    """Drain a queue of ticket edits, filing every outcome where it can be seen.

    Nothing here fails quietly. A file that cannot be applied moves to
    `rejected/` beside a `.reason.json`, because the worst state is the one
    where someone drops a file and nothing happens anywhere.
    """
    outbox = Path(args.outbox)
    applied_dir = outbox / "applied"
    rejected_dir = outbox / "rejected"
    for directory in (applied_dir, rejected_dir):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)

    store = _store(args)
    actor = _actor(args)
    results: list[dict[str, Any]] = []
    queued = sorted(path for path in outbox.glob("*.json") if path.is_file())
    for path in queued[: max(1, args.limit)]:
        result = _apply_one_outbox(store, path, actor)
        destination = (applied_dir if result["ok"] else rejected_dir) / path.name
        reason_path = destination.with_suffix(destination.suffix + ".reason.json")
        reason_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.chmod(reason_path, 0o600)
        path.replace(destination)
        results.append(result)

    _emit({
        "ok": True,
        "actor": actor,
        "queued": len(queued),
        "applied": sum(1 for result in results if result["ok"]),
        "rejected": sum(1 for result in results if not result["ok"]),
        "results": results,
    })
    return 0


def _admin_store(args: argparse.Namespace) -> Any:
    from .admin_store import AdminStore

    if args.config_root is not None:
        return AdminStore(Path(args.config_root))
    return AdminStore.from_environment()


def _dispatch(args: argparse.Namespace) -> int:
    command = args.work_command
    if command == "apply-outbox":
        return _apply_outbox(args)
    if command == "agent-token":
        admin = _admin_store(args)
        token, csrf = admin.issue_agent_session(args.name, actor=_actor(args))
        # Printed once, here, and nowhere else. It is not written to the audit
        # trail, the manifests or the handoff records - only the fact that a
        # session was issued is.
        _emit({
            "ok": True,
            "subject": f"{admin.AGENT_SUBJECT_PREFIX}{args.name}",
            "token": token,
            "csrf": csrf,
            "expires_in_seconds": admin.AGENT_SESSION_SECONDS,
            "note": "Put this in the agent's environment. Do not write it to a log or a receipt.",
        })
        return 0
    if command == "agent-revoke":
        admin = _admin_store(args)
        subject = admin.agent_subject(args.name)
        generation = admin.revoke_agent(subject, actor=_actor(args))
        _emit({"ok": True, "subject": subject, "generation": generation})
        return 0
    if command == "agent-list":
        admin = _admin_store(args)
        generations = admin.agent_generations()
        _emit({
            "ok": True,
            "agents": [
                {
                    "name": name,
                    "subject": f"{admin.AGENT_SUBJECT_PREFIX}{name}",
                    "revocations": generations.get(f"{admin.AGENT_SUBJECT_PREFIX}{name}", 0),
                }
                for name in admin.AGENT_NAMES
            ],
            "session_seconds": admin.AGENT_SESSION_SECONDS,
        })
        return 0

    store = _store(args)
    if command == "create":
        item = store.create_item(_fields(args), actor=_actor(args))
        _emit({"ok": True, "created": True, "item": item})
        return 0
    if command == "upsert":
        result = store.upsert_item(
            _fields(args),
            actor=_actor(args),
            item_id=args.item_id,
            source_ref=args.match_source_ref,
            expected_revision=args.expected_revision,
            expected_updated_at=args.expected_updated_at,
            force_overwrite=args.force_overwrite,
        )
        _emit({"ok": True, "created": result["created"], "item": result["item"]})
        return 0
    if command == "update":
        item = store.update_item(
            args.item_id,
            _fields(args),
            actor=_actor(args),
            expected_revision=args.expected_revision,
            expected_updated_at=args.expected_updated_at,
        )
        _emit({"ok": True, "item": item})
        return 0
    if command == "archive":
        item = store.archive_item(
            args.item_id,
            actor=_actor(args),
            expected_revision=args.expected_revision,
            expected_updated_at=args.expected_updated_at,
        )
        _emit({"ok": True, "item": item})
        return 0
    if command == "list":
        payload = store.list_items(
            include_archived=args.include_archived,
            statuses=args.status or None,
            assigned_to=args.assigned_to,
            parent_id=args.parent_id,
        )
        _emit({"ok": True, **payload})
        return 0
    if command == "board":
        payload = store.list_items(
            include_archived=args.include_archived, assigned_to=args.assigned_to
        )
        columns = group_into_columns(payload["items"])
        placed = sum(column["count"] for column in columns)
        _emit(
            {
                "ok": True,
                "version": payload["version"],
                "migrated_from": payload["migrated_from"],
                "revision": payload["revision"],
                "total": payload["total"],
                "shown": payload["count"],
                # Every listed item lands in exactly one column, or is counted
                # as aged out of a dated one.  These two adding up to `shown`
                # is what makes a silent disappearance impossible.
                "placed": placed,
                "aged_out": columns[-1]["aged_out"],
                "status_counts": payload["status_counts"],
                "columns": columns,
            }
        )
        return 0
    if command == "meta":
        _emit({"ok": True, **status_metadata()})
        return 0
    if command == "show":
        item = store.get_item(args.item_id)
        _emit({
            "ok": True,
            "item": item,
            "roles": describe_roles(item, store.list_items()["items"]),
        })
        return 0
    if command == "history":
        limit = max(1, min(int(args.limit), 500))
        _emit({"ok": True, "items": store.read_history(limit=limit, item_id=args.item_id)})
        return 0
    if command == "timeline":
        limit = max(1, min(int(args.limit), 1_000))
        _emit({"ok": True, **store.read_timeline(args.item_id, limit=limit)})
        return 0
    raise AssertionError(f"Unhandled work command: {command}")
