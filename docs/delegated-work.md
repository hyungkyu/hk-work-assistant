# Delegated work tracking

First version of the `업무 현황` backoffice page and its shared store.  It answers
four questions only: what HK delegated to Codex, what is being worked on now and
by whom, what remains, and what was finished recently.

Team views, 1:1 views, collaboration, Slack/Notion inference, and collector
operation dashboards are deliberately out of scope.  The schema already carries
`assigned_to` and `parent_id`, so more agents and parent/child jobs can be added
later without a migration.

## Where the data lives

Everything sits under `APP_CONFIG_ROOT` (default
`~/.config/hk-work-assistant`, `/config/hk-work-assistant` in the containers).
Nothing is written to Git, the settings database, or `/data`.

| Path | Format | Purpose |
| --- | --- | --- |
| `work/items.json` | versioned JSON object, mode `0600` | every work item |
| `work/history.jsonl` | append-only JSON lines, mode `0600` | change history |
| `work/items.lock` | empty lock file, mode `0600` | `flock` for writers |

The directory is created on first use with mode `0700`.

`items.json` looks like:

```json
{
  "version": 1,
  "revision": 12,
  "updated_at": "2026-09-01T02:56:27.290568+00:00",
  "items": [ { "id": "wi_846fb2146674febb", "title": "…", "revision": 3, "…": "…" } ]
}
```

Writers hold an exclusive `flock` for the whole read-modify-write cycle and
replace `items.json` atomically through a temporary file in the same directory.

Every read validates the complete document before anything else happens: the
exact top-level and item key sets, every field's type and constraints, item
revisions, unique ids, timestamp format and the ordering of the stamps the store
owns, and the whole parent graph (missing, archived, self-referential, cyclic,
or too-deep parents).  Nothing stored is normalized, defaulted, repaired, or
dropped.  Any violation - including an unknown `version` or a field the v1
schema does not define - is reported as corruption, `items.json` is left
byte-for-byte unchanged, and a mutation that fails this way appends nothing to
the history either.  The operator repairs or removes the file by hand.

Reading is dispatched on the stored `version`.  A future version 2 registers its
own strict loader plus a migration from v1; versions are never bridged by
silently keeping or discarding fields the current schema does not know.

### Work item fields

`id`, `title`, `detail`, `status`, `priority`, `requested_by`, `assigned_to`,
`parent_id`, `progress_summary`, `next_action`, `blocker`, `created_at`,
`updated_at`, `started_at`, `completed_at`, `due_at`, `source_ref`,
`archived_at`, `revision`.

- `status`: the board's primary classification is a four-stage queue -
  `in_progress` (진행 중), `ready` (다음 할 일), `todo` (해야 할 일), `backlog` (백로그).
  `waiting` (대기), `blocked` (막힘), `done` (완료) and `cancelled` (취소) are supporting
  states: they say why an item is not moving, or that it has left the queue.

  The board is a **total partition** of this set. `work_store.BOARD_COLUMNS` gives every
  status exactly one column, `_validate_board_columns()` enforces that at import, and any
  status a client's own column list fails to claim goes to an explicit `미분류` residue
  column. An item can never fall between two columns and disappear. `GET
  /api/v1/admin/work/meta` serves the labels and the columns, so the page derives its
  layout from the schema instead of hard-coding it, and `list_items` returns `total` and
  `status_counts` for the whole live set beside the filtered slice, so a filtered view
  can never be mistaken for the complete one.
- `priority`: `urgent`, `high`, `normal`, `low`
- `started_at` is set on the first move to `in_progress`; `completed_at` is set
  on `done`/`cancelled` and cleared when an item is reopened.
- `parent_id` must reference an existing, unarchived item.  Self-parenting,
  cycles, and chains deeper than eight levels are rejected.
- Archiving is a soft delete: `archived_at` is stamped, the item stays in the
  document, and its children must be archived first.

`id`, `created_at`, `updated_at`, `revision`, and `archived_at` are owned by the
store; a caller that tries to set them, or any field outside the list above, is
rejected.

### Change history

`work/history.jsonl` records one line per change with `at`, `action`, `actor`,
`item_id`, `revision`, the sorted names of the fields that changed, and the
status (plus `status_from` on a transition).  It deliberately holds no work item
free text, so nothing a person or agent typed can leak into the stream.  API
mutations additionally append to the existing admin `audit.jsonl`.

## Concurrent writers

Two agents can edit at the same time without silently overwriting each other:

- The `flock` serializes the read-modify-write cycles.
- Each item carries a `revision`.  Pass `expected_revision` (preferred) or
  `expected_updated_at` on an update or archive and the store refuses the write
  with a conflict when the item has moved on.
- `upsert` against an existing match **requires** one of those expectations.
  Without one it fails as a validation error rather than overwriting; a caller
  that intends last-write-wins passes `force_overwrite` / `--force-overwrite`.
  An expectation supplied alongside force is still checked - force only waives
  the requirement to supply one.

## HTTP API

Super-administrator session required, same cookie, CSRF header, and audit
behaviour as the settings API.

| Method | Path |
| --- | --- |
| `GET` | `/api/v1/admin/work/items?status=…&assigned_to=…&include_archived=…` |
| `GET` | `/api/v1/admin/work/items/{id}` |
| `POST` | `/api/v1/admin/work/items` |
| `PATCH` | `/api/v1/admin/work/items/{id}` |
| `POST` | `/api/v1/admin/work/items/{id}/archive` |
| `GET` | `/api/v1/admin/work/history?limit=…&item_id=…` |
| `GET` | `/api/v1/admin/work/meta` |

Mutations take `{"fields": {…}, "expected_revision": N}`.  Errors are explicit:
`400` validation, `401`/`403` authentication and CSRF, `404` unknown item,
`409` conflict, `503` corruption or lock timeout.

## CLI

`worklog work` reaches the same store with the same validation and needs no web
session, so an agent running locally can record its own progress.  Standard
output is always exactly one JSON document.

```bash
# Delegate something to Codex
worklog work create --title "업무 현황 1차 구현" --requested-by hk \
    --assigned-to codex --priority high --next-action "스키마 확정"

# An agent reports progress, refusing to clobber a concurrent edit
worklog work update wi_846fb2146674febb --actor codex \
    --status in_progress --progress "설계 완료" --expected-revision 1

# Idempotent sync from an external reference.  Creating needs no expectation;
# updating an existing match requires one, so a concurrent human or agent edit
# is reported instead of overwritten.
worklog work upsert --match-source-ref "github:RLWRLD/worklog#12" \
    --title "리뷰 반영" --requested-by hk --assigned-to codex
worklog work upsert --match-source-ref "github:RLWRLD/worklog#12" \
    --status waiting --expected-revision 3

# A mechanical caller that genuinely wants last-write-wins must say so.
worklog work upsert --match-source-ref "github:RLWRLD/worklog#12" \
    --progress "동기화됨" --force-overwrite

# Read back
worklog work list --status in_progress --assigned-to codex
worklog work show wi_846fb2146674febb
worklog work history --limit 20

# Soft delete
worklog work archive wi_846fb2146674febb --expected-revision 4
```

Use `--clear FIELD` to empty an optional field, `--config-root` to point at a
different `APP_CONFIG_ROOT`, and `--actor` (or `WORKLOG_ACTOR`) to say who is
making the change.

Exit codes: `0` success, `2` validation, `3` unknown item, `4` conflict,
`5` corruption, `6` lock timeout.

## Backoffice page

`업무 현황` in the backoffice sidebar shows four columns - `진행 중`, `남은 일`,
`대기·막힘`, and `최근 완료` (finished within 14 days) - each card carrying the
assignee, priority, progress, next action, blocker, and last update time.  Items
can be added, edited, moved between statuses, and archived without a page
reload.  Read-only data is polled every 15 seconds while the page is open, and a
poll never re-renders the board while an edit form is open or a write is in
flight.

No sample or default items are seeded anywhere; the board starts empty.


## Document versions

The stored document is versioned and every version has its own strict loader.

| version | statuses |
|---|---|
| 1 | `backlog`, `ready`, `in_progress`, `waiting`, `blocked`, `done`, `cancelled` |
| 2 | version 1 plus `todo` |

A file that declares version 1 is validated against exactly the v1 enum, so a document
containing `todo` while claiming to be v1 is reported as corruption rather than accepted
under the wider enum.  `_migrate_v1_to_v2` is a total, lossless widening: every v1 status
keeps its meaning and `todo` is new, so no item is relabelled or dropped.  Reading
migrates in memory and reports `migrated_from`; the file itself is only rewritten by the
next ordinary write, and the backoffice says an upgrade is pending until then.

## CLI

    worklog work meta                     # the status schema and board layout
    worklog work board --assigned-to hk   # the four-stage board, with residue and totals

`board` reports `total`, `shown`, `placed` and `aged_out`.  `placed + aged_out == shown`
is the invariant that makes a silent disappearance impossible.
