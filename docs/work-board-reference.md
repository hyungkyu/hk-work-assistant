# 업무 보드 reference

Exhaustive reference for the delegated-work subsystem: item schema, enums,
validation layers, history format, outbox protocol, CLI surface, HTTP surface.

The narrative — what the board is, why the write limits exist, and the two open
authorization gaps — is in [delegated-work.md](delegated-work.md).  Read that
first; this file assumes it.

All line references are to `src/rlwrld_worklog/` unless stated otherwise.

## Contents

- [Storage](#storage)
- [Item schema](#item-schema)
- [Enums](#enums)
- [Validation layers](#validation-layers)
- [History](#history)
- [Outbox protocol](#outbox-protocol)
- [CLI](#cli)
- [HTTP API](#http-api)
- [Exceptions](#exceptions)

## Storage

### Root resolution

`WorkStore.from_environment()` (`work_store.py:613-617`):

```python
configured = os.environ.get("APP_CONFIG_ROOT")
root = Path(configured) if configured else Path.home() / ".config/hk-work-assistant"
```

`AdminStore.from_environment()` (`admin_store.py:98-102`) resolves identically,
so both stores share one root.

### Paths

Set in `WorkStore.__init__` (`work_store.py:604-611`).

| Path | Purpose | Mode | Format |
|---|---|---|---|
| `$APP_CONFIG_ROOT/work/` | store directory; `mkdir(mode=0o700)` **and** `chmod(0o700)` on every construction | `0700` | dir |
| `work/items.json` | the work document | `0600` | `json.dumps(..., ensure_ascii=False, indent=2, sort_keys=True) + "\n"` (`work_store.py:1170-1173`) |
| `work/history.jsonl` | append-only change history | `0600` | JSON Lines, `ensure_ascii=False, sort_keys=True` (`work_store.py:1228`) |
| `work/items.lock` | writer lock | `0600` | opaque; opened `"a+"`, `fchmod 0o600` (`work_store.py:1242-1244`) |

Read-only inputs the timeline merges from, never written by this subsystem:

| Path | Used for |
|---|---|
| `cowork/events.jsonl` | `source: "cowork_event"` entries where `event["work_id"] == item_id` (`work_store.py:833-859`) |
| `cowork/handoffs/*.json` | `source: "cowork_handoff"`; a file is read only if `item_id in name` **and** `payload["work_id"] == item_id` (`work_store.py:861-891`) |
| `cowork/mailbox/.processed-<agent>`, `.cursor-<agent>` | agent liveness via `agent_activity` (`cowork.py:484`, `work_web.py:309`) |

Admin-side files in the same root: `settings.json`, `audit.jsonl`,
`credentials/` (`admin_store.py:84-90`).

### Document shape

**On disk** — exactly four keys, enforced by `DOCUMENT_FIELDS`
(`work_store.py:400`), written by `_commit` (`work_store.py:1163-1173`):

| Key | Type | Constraint |
|---|---|---|
| `version` | `int` | 1 or 2; `DOCUMENT_VERSION = 2` (`work_store.py:30`) |
| `revision` | `int` | `>= 0`, not `bool`; incremented once per commit |
| `updated_at` | `str \| None` | ISO 8601 with UTC offset; `null` **iff** `revision == 0` |
| `items` | `list` | item objects |

**In memory** — `read_document` returns those four plus a fifth
(`work_store.py:653`):

| Key | Type | Meaning |
|---|---|---|
| `migrated_from` | `int \| None` | the version actually on disk, when it differs from `DOCUMENT_VERSION` |

`empty_document()` (`work_store.py:389-396`) returns all five with
`migrated_from: None`.  Reading never rewrites the file
(`work_store.py:650-652`).

### Atomicity

`_commit` delegates to `_atomic_private_write`, imported from `admin_store`
(`work_store.py:26`).  That function (`admin_store.py:61-78`): parent
`mkdir(0o700)` + `chmod(0o700)`, `tempfile.mkstemp(prefix=f".{path.name}.",
dir=path.parent)`, `os.fchmod(fd, 0o600)`, write, `flush()`, `os.fsync()`,
`os.replace()`, `path.chmod(0o600)`; on any `BaseException` the temp file is
unlinked and the error re-raised.

The containing directory is not fsynced after `os.replace`, so the rename is not
crash-durable on every filesystem.  The file contents are.

### Lock

`WorkStore._locked` (`work_store.py:1234-1261`):

- `open(lock_path, "a+")`, `os.fchmod(..., 0o600)`
- `fcntl.flock(fd, LOCK_EX | LOCK_NB)` in a spin loop, `time.sleep(0.02)`
  between attempts, deadline `time.monotonic() + timeout`, `timeout: float = 10.0`
- on deadline: `WorkLockTimeout("another writer is holding the work store lock")`
- released with `LOCK_UN` in `finally`, handle closed in an outer `finally`
- must never be nested (flock is per open file description).  Every public
  mutation takes it exactly once: `create_item` (`907`), `update_item` (`928`),
  `upsert_item` (`961`), `archive_item` (`1003`).

**Reads take no lock.**  `read_document` (`work_store.py:621-654`) is called
without it from `list_items`, `get_item`, `read_history`, `read_timeline`;
`os.replace` makes a reader see the old file or the new one, never a partial one.

### Whole-store bounds

| Constant | Value | Where enforced |
|---|---|---|
| `MAX_ITEMS` (`work_store.py:31`) | `5_000` | create only (`work_store.py:1044-1045`); **not** re-checked on load |
| `MAX_PARENT_DEPTH` (`work_store.py:32`) | `8` | write (`_check_parent`) and read (`_validate_stored_graph`) |

## Item schema

19 fields, fixed by `_blank_item()` (`work_store.py:365-386`).
`STORED_ITEM_FIELDS = frozenset(_blank_item())` (`work_store.py:401`) makes the
set exact on read: missing *or* unknown keys are corruption
(`work_store.py:431-436`).

The partition — 14 + 5 = 19, no overlap:

- `MUTABLE_FIELDS` — 14 (`work_store.py:87-102`)
- `SERVER_OWNED_FIELDS = ("id", "created_at", "updated_at", "revision", "archived_at")` — 5 (`work_store.py:104`)
- `REQUIRED_ON_CREATE = ("title", "requested_by", "assigned_to")` (`work_store.py:103`)

| Field | Type | Owner | Required on create | Constraint (exact) |
|---|---|---|---|---|
| `id` | `str` | **server** | n/a | `^wi_[0-9a-f]{16}$` (`_ITEM_ID`, `work_store.py:120`); minted `"wi_" + secrets.token_hex(8)` (`work_store.py:151-152`) |
| `title` | `str` | mutable | **yes** | stripped length 1..200 (`TEXT_FIELDS`, `work_store.py:107`); not nullable |
| `detail` | `str \| None` | mutable | no | stripped 0..8000 (`:108`); in `OPTIONAL_TEXT_FIELDS` (`:114`), so `None`/`""` → `None` |
| `status` | `str` | mutable | no — defaults `"backlog"` (`:370`) | one of `STATUSES` |
| `priority` | `str` | mutable | no — defaults `"normal"` (`:371`) | one of `PRIORITIES` |
| `requested_by` | `str` | mutable | **yes** | `^[a-z0-9][a-z0-9._@+-]{0,79}$` after `.strip().lower()` (`_ACTOR`, `:119`; `_normalize_actor`, `:192-200`) — max 80 chars |
| `assigned_to` | `str` | mutable | **yes** | same as `requested_by` |
| `parent_id` | `str \| None` | mutable | no | `None`/blank → `None`; else `^wi_[0-9a-f]{16}$` (`:355-361`) |
| `progress_summary` | `str` | mutable | no — defaults `""` | stripped 0..2000 (`:109`); **not** optional, so `None` raises `"progress_summary must be a string"` (`:177-179`) |
| `next_action` | `str` | mutable | no — defaults `""` | stripped 0..500 (`:110`); not nullable |
| `blocker` | `str \| None` | mutable | no | stripped 0..500 (`:111`); optional, so nullable |
| `due_at` | `str \| None` | mutable | no | ISO 8601, raw ≤64 chars; trailing `Z`/`z` → `+00:00`; naive assumed UTC; stored as `.astimezone(utc).isoformat()` (`_normalize_timestamp`, `:155-171`) |
| `started_at` | `str \| None` | mutable | no | as `due_at`; also auto-set (below) |
| `completed_at` | `str \| None` | mutable | no | as `due_at`; also auto-set and auto-cleared (below) |
| `source_ref` | `str \| None` | mutable | no | stripped 0..500 (`:112`); optional; doubles as the upsert identity key |
| `created_at` | `str` | **server** | n/a | set once at create (`:1050`) |
| `updated_at` | `str` | **server** | n/a | rewritten on every mutation (`:1051`, `:1092`, `:1020`); must be `>= created_at` on read (`:481-482`) |
| `archived_at` | `str \| None` | **server** | n/a | set only by `archive_item` (`:1019`); on read must satisfy `created_at <= archived_at <= updated_at` (`:483-484`) |
| `revision` | `int` | **server** | n/a | starts at 1 (`:1052`), `+1` per mutation (`:1093`, `:1021`); on read must be `int`, not `bool`, `>= 1` (`:443-445`) |

Setting a `SERVER_OWNED_FIELDS` key is rejected first: `"read-only fields cannot
be set: …"` (`work_store.py:333-335`).  Any key outside `MUTABLE_FIELDS`:
`"unknown fields: …"` (`work_store.py:336-338`).

### Text field limits, in one place

`TEXT_FIELDS` (`work_store.py:106-113`), as `(minimum, maximum)` on the
**stripped** value for the minimum and the **raw** value for the maximum:

| Field | min | max | optional? |
|---|---|---|---|
| `title` | 1 | 200 | no |
| `detail` | 0 | 8000 | yes |
| `progress_summary` | 0 | 2000 | no |
| `next_action` | 0 | 500 | no |
| `blocker` | 0 | 500 | yes |
| `source_ref` | 0 | 500 | yes |

`OPTIONAL_TEXT_FIELDS = {"detail", "blocker", "source_ref"}`
(`work_store.py:114`).  `TIMESTAMP_FIELDS = ("due_at", "started_at",
"completed_at")` (`work_store.py:115`).

### Derived timestamps

`_apply_status_timestamps` (`work_store.py:1499-1512`), run on both create and
update:

1. `status == "in_progress"` and `started_at is None` → `started_at = now`
2. `status in TERMINAL_STATUSES` and `completed_at is None` → `completed_at = now`
3. leaving a terminal status (`previous_status in TERMINAL_STATUSES`, new status
   not) and `completed_at` not explicitly named in this change →
   `completed_at = None`

`TERMINAL_STATUSES = frozenset({"done", "cancelled"})` (`work_store.py:81`).

## Enums

### Statuses

```python
QUEUE_STAGES = ("in_progress", "ready", "todo", "backlog")            # work_store.py:36
SUPPORTING_STATUSES = ("waiting", "blocked", "done", "cancelled")     # work_store.py:40
STATUSES = QUEUE_STAGES + SUPPORTING_STATUSES                          # work_store.py:42
```

| Status | Label (`STATUS_LABELS`, `:50-59`) | Group (`STATUS_GROUPS`, `:60-62`) | Terminal |
|---|---|---|---|
| `in_progress` | 진행 중 | `queue` | no |
| `ready` | 다음 할 일 | `queue` | no |
| `todo` | 해야 할 일 | `queue` | no |
| `backlog` | 백로그 | `queue` | no |
| `waiting` | 대기 | `supporting` | no |
| `blocked` | 막힘 | `supporting` | no |
| `done` | 완료 | `supporting` | **yes** |
| `cancelled` | 취소 | `supporting` | **yes** |

The queue stages are listed most-advanced-first and an item is always at exactly
one of them; the supporting statuses are conditions rather than queue positions
(`work_store.py:34-41`).  `describe_roles` reports the distinction per item as
`stage.kind` = `"queue"` or `"condition"` (`work_store.py:1300-1307`).

`STATUS_ORDER` (`work_store.py:63`) is index-in-`STATUSES` and is not read
anywhere else in the subsystem.

Frozen v1 enum (`work_store.py:48`):

```python
V1_STATUSES = ("backlog", "ready", "in_progress", "waiting", "blocked", "done", "cancelled")
```

A file declaring `version: 1` while containing `todo` is corruption, not a
silent upgrade (`work_store.py:44-47`; `tests/test_work_queue.py:167`).

### Priorities

```python
PRIORITIES = ("urgent", "high", "normal", "low")                        # work_store.py:82
PRIORITY_RANK = {name: index for index, name in enumerate(PRIORITIES)}  # work_store.py:83
```

Used in `list_items` as the outer stable sort key (`work_store.py:683`), with
`(updated_at, id)` descending as the inner one (`work_store.py:682`).  There is
no server-side Korean label map for priorities; the page hard-codes
`{urgent: '긴급', high: '높음', normal: '보통', low: '낮음'}`
(`static/admin.html:737`).

### Phases

```python
PHASES = ("assigned", "started", "progress", "review",
          "build", "deploy", "verified", "failed")                      # work_store.py:205-208
```

Published at `GET /meta` (`work_web.py:185`) and validated by
`_normalize_context`, but no shipped caller can set one — see *History →
context* below.

### Board columns

`BOARD_COLUMNS` (`work_store.py:70-77`), six columns, a total partition of
`STATUSES`:

| key | title | statuses | recent_days |
|---|---|---|---|
| `in_progress` | 진행 중 | `in_progress` | `None` |
| `ready` | 다음 할 일 | `ready` | `None` |
| `todo` | 해야 할 일 | `todo` | `None` |
| `backlog` | 백로그 | `backlog` | `None` |
| `held` | 대기 · 막힘 | `waiting`, `blocked` | `None` |
| `closed` | 최근 완료 | `done`, `cancelled` | `14` |

`RESIDUE_COLUMN_KEY = "unclassified"`, `RESIDUE_COLUMN_TITLE = "미분류"`
(`work_store.py:78-79`).

`group_into_columns` (`work_store.py:1340-1391`) places every leftover into the
residue column and counts `aged_out` separately — an item a dated column
excluded is deliberate, a status no column claims is a layout defect
(`work_store.py:1375-1377`).  `_within_recent_days`
(`work_store.py:1394-1403`) uses `completed_at or updated_at` and treats an
unparseable stamp as *recent*, so a bad timestamp cannot delete an item from the
board.

## Validation layers

Ten layers, each named with the function that implements it and what it catches.

### 1 — CLI argument surface

`argparse` `choices` in `_add_field_flags` (`work_cli.py:181-195`) and
`add_work_parser` (`work_cli.py:108`, `:194`).  Catches an out-of-enum
`--status`/`--priority` and a `--clear` naming a field outside
`CLEARABLE_FIELDS`.  Rejected before the store is opened
(`tests/test_work_cli.py:209`).

### 2 — HTTP request surface

- `_json_object` (`work_web.py:74-81`) — body must parse as JSON and be an
  object → 400.
- `_concurrency` (`work_web.py:144-156`) — `expected_revision` must be `int` and
  not `bool`; `expected_updated_at` must be `str` → 400.
- FastAPI `Query` bounds — `limit` on `/items` `ge=1, le=2_000` default 500
  (`work_web.py:198`); on `/timeline` `ge=1, le=1_000` default 200
  (`work_web.py:275`); on `/history` `ge=1, le=500` default 100
  (`work_web.py:322`).
- `_require_csrf` (`admin_web.py:148-151`) — header `x-csrf-token` must equal
  `session["csrf"]` → 403.  Applied to POST/PATCH/archive only.

### 3 — Authorization

`require_board_session` / `require_super_admin_session`
(`admin_web.py:123-145`), `_require_may_write` (`work_web.py:91-107`),
`_require_may_name_only_self` (`work_web.py:109-141`).  Web only — see
[delegated-work.md](delegated-work.md) for the CLI gap.

### 4 — Field normalization on input

`_normalize_changes` (`work_store.py:329-362`), dispatching to `_normalize_text`
(`:174-189`), `_normalize_timestamp` (`:155-171`), `_normalize_actor`
(`:192-200`), plus inline enum checks for `status`/`priority` and the
`parent_id` regex.  Catches a non-Mapping bundle, server-owned keys, unknown
keys, wrong types, length violations, bad enum values, malformed identifiers and
timestamps.

`_require_create_fields` (`work_store.py:1425-1428`) then catches a create
missing `title`/`requested_by`/`assigned_to`.  It tests `not
changes.get(name)`, so absent, `None` and `""` all read as missing.

`update_item` additionally refuses an empty bundle: `"no fields to update"`
(`work_store.py:925-926`).

### 5 — Timeline context validation

`_normalize_context` (`work_store.py:290-326`) and `_normalize_receipt`
(`:271-287`).  Catches: non-Mapping context; any key outside `{directed_by,
phase, session_id, summary, receipt}`; `phase` outside `PHASES`; `session_id`
non-string, empty, or >120 chars; `summary` non-string, empty, or
>`MAX_CONTEXT_SUMMARY` (500, `work_store.py:268`); `receipt` non-string, empty,
>300 chars, absolute (`/` or `~`), or containing a `..` path segment.

Unreachable from any shipped caller — see [History → context](#context).

### 6 — Relational and invariant checks at write time

| Check | Function | Raises |
|---|---|---|
| parent is self | `_check_parent` (`:1447-1471`) | `"a work item cannot be its own parent"` |
| parent missing | `_check_parent` | `"parent work item not found: …"` |
| parent archived | `_check_parent` | `"parent work item is archived: …"` |
| parent cycle | `_check_parent` | `"parent_id would create a cycle"` |
| chain > 8 | `_check_parent` | `"parent chain is deeper than 8 levels"` |
| `expected_revision` not a non-bool `int` | `_check_expectations` (`:1481-1496`) | `WorkValidationError` |
| revision or `updated_at` mismatch | `_check_expectations` | `WorkConflictError` |
| store already holds 5,000 items | `_create_within` (`:1044-1045`) | `WorkValidationError` |
| already archived | `archive_item` (`:1007-1008`) | `WorkConflictError` |
| live children exist | `archive_item` (`:1009-1017`) | `"archive the child items first: …"` |
| updating an archived item | `_update_within` (`:1078-1079`) | `WorkConflictError` |
| upsert by `id` onto an archived item | `_match_identity` (`:1431-1444`) | `WorkConflictError` |
| `source_ref` matches >1 live item | `_match_identity` | `"source_ref matches N items"` |
| expectation supplied but nothing matches | `upsert_item` (`:964-968`) | `WorkValidationError` |
| existing match with neither expectation nor `force_overwrite` | `upsert_item` (`:976-981`) | `WorkValidationError` naming the current revision |

### 7 — Whole-file integrity check, on every read

`read_document` (`work_store.py:621-654`) catches, in order:

1. `OSError` reading the file → `"work store is unreadable: …"` (`:629-632`)
2. `json.JSONDecodeError` → `"work store is not valid JSON and was left unchanged"` (`:633-638`)
3. top level not a dict → `"document must be a JSON object"` (`:639-640`)
4. `version` not an `int`, a `bool`, or absent from `_LOADERS` →
   `"unsupported work store version {version!r}; expected 2"` (`:641-646`)

Then the version-specific loader `_load_document(parsed, version=…,
statuses=…)` (`work_store.py:523-558`):

- missing top-level fields → `"document is missing fields: …"`
- unknown top-level fields → `"document has unknown fields: …"`
- `revision` not `int`, or `bool`, or `< 0` → `"document revision must be a non-negative integer"`
- `items` not a `list` → `"document items must be a list"`
- `revision == 0` and `updated_at is not None` → `"document updated_at must be null before the first write"`
- `revision != 0` → `updated_at` must be a valid non-optional stored timestamp

Per item, `_validate_stored_item` (`work_store.py:426-485`): not a dict; missing
or unknown fields against `STORED_ITEM_FIELDS`; `id` not matching `_ITEM_ID`;
`revision` not `int`, or `bool`, or `< 1`; `status` not in **the version's**
status tuple; `priority` not in `PRIORITIES`; `requested_by`/`assigned_to` not
matching `_ACTOR`; each `TEXT_FIELDS` entry (`None` only where optional, must be
`str`, `len(value.strip()) >= minimum`, `len(value) <= maximum`); `parent_id`
neither `None` nor an item id; `created_at`/`updated_at` as required timestamps
and `due_at`/`started_at`/`completed_at`/`archived_at` as optional ones, all via
`_stored_timestamp` (`work_store.py:408-423`), which additionally requires a UTC
offset (`parsed.tzinfo is None` → `"must carry a UTC offset"`) and a raw length
≤ 64; `updated_at < created_at`; `archived_at` outside `created_at..updated_at`.

Only the stamps the store owns get ordering checks: `due_at`, `started_at` and
`completed_at` are caller-settable and may legitimately be backdated
(`work_store.py:477-479`).

Migration is a separate, explicit step: `_LOADERS = {1: …, 2: …}` and
`_MIGRATIONS = {1: _migrate_v1_to_v2}` (`work_store.py:586-587`); `_migrate`
loops until `DOCUMENT_VERSION` and raises `"no migration from work store version
X to 2"` if a step is missing (`work_store.py:590-598`).

### 8 — Graph integrity check

`_validate_stored_graph` (`work_store.py:488-520`).  Every condition it raises:

| # | Condition | Message | Line |
|---|---|---|---|
| 1 | duplicate id | `duplicate work item id: {id}` | 492 |
| 2 | self-parent | `item {id} is its own parent` | 499 |
| 3 | dangling parent | `item {id} references a missing parent: {parent_id}` | 502 |
| 4 | live child of an archived parent | `item {id} has an archived parent: {parent_id}` | 503-506 |
| 5 | parent cycle | `parent cycle through item {id}` | 511-512 |
| 6 | chain too deep | `item {id} has a parent chain deeper than 8 levels` | 515-518 |

Condition 4 is unreachable through the write path, since `archive_item` requires
children be archived first; its presence means the file was written by something
else (`work_store.py:504-505`).  Items with `parent_id is None` are skipped
(`:495-497`); the walk carries `seen = {item["id"]}` and terminates on a missing
ancestor (`:519-520`).

### 9 — Import-time board invariant

`_validate_board_columns()` (`work_store.py:1406-1419`), **called at module
import** (`work_store.py:1422`).  Raises `RuntimeError` — not a `WorkStoreError`
— for a status claimed by two columns, a status in `STATUSES` no column claims
(`"give every status a column"`), or a column claiming a status not in
`STATUSES`.  Asserted by `tests/test_work_queue.py:64`.

### 10 — Outbox file validation

`_apply_one_outbox` / `_apply_one_create` (`work_cli.py:290-400`).  See
[Outbox protocol](#outbox-protocol).

## History

### Record shape

`_append_history` (`work_store.py:1175-1220`) writes one line:

| Key | Type | Meaning |
|---|---|---|
| `at` | `str` | `_utc_now()` at write time — a separate call from the item's `updated_at` |
| `action` | `str` | `"work.created"` \| `"work.updated"` \| `"work.archived"` |
| `actor` | `str` | the normalized actor |
| `item_id` | `str` | |
| `revision` | `int` | the item's revision **after** the change |
| `fields` | `list` | sorted names of the changed fields |
| `values` | `dict` | `{field: {"before": {…}, "after": {…}}}` |
| `status` | `str` | the item's status **after** the change |
| `requested_by` | `str` | copied off the item |
| `assigned_to` | `str` | copied off the item |

`requested_by` and `assigned_to` are copied so a timeline never has to join back
to the document to be read (`work_store.py:1212-1215`).

Plus `status_from`, **only** when `status_from is not None and status_from !=
item["status"]` (`work_store.py:1217-1218`), and then `entry.update(context or
{})` (`:1219`) folds any validated context keys in at top level.

### Before/after values

`_field_changes` (`work_store.py:255-262`) builds `{key: {"before": …, "after":
…}}` per changed key.  `_history_value` (`work_store.py:238-252`) shapes one
side:

| Input | Output |
|---|---|
| absent (`_MISSING`, the sentinel at `work_store.py:212`) | `{"present": false}` |
| non-string | `{"present": true, "value": value}` |
| string within the cap | `{"present": true, "value": value}` |
| string over the cap | `{"present": true, "value": value[:MAX_HISTORY_VALUE], "truncated": true, "length": <int>, "sha256": "<hex>"}` |

`_MISSING` exists so that a field that was absent and a field that held an empty
string stay different facts (`work_store.py:1082-1084`).

Per operation:

- **create** (`work_store.py:1064`) — `values=_field_changes({key: _MISSING for
  key in changes}, changes)`, so every `before` is `{"present": false}`.
- **update** (`work_store.py:1085`, `:1103`) — `before` captured from the live
  item **before** `item.update(changes)`.
- **archive** (`work_store.py:1022-1030`) — `fields=["archived_at"]` and **no
  `values` argument**, so `values` is `{}` (`:1210`).  The archive timestamp
  itself is not in the value record.

### The derived value cap

```python
MAX_HISTORY_VALUE = max(maximum for _, maximum in TEXT_FIELDS.values())   # work_store.py:235
```

Evaluates to **8000**, the `detail` maximum.  It was a flat `2_000` while
`detail` accepts `8_000`, so a 3,615-character detail edit on 2026-09-04 reached
the stream clipped (`work_store.py:217-234`).  Deriving it from `TEXT_FIELDS`
makes the bound "the largest value the board can hold", and
`tests/test_work_store.py:812-815` asserts the **relation** (`MAX_HISTORY_VALUE
>= maximum` for every `TEXT_FIELDS` entry), not the number, so raising a field's
cap past the history's fails the test rather than the history.

The clip is kept as a floor under a string arriving with no length of its own,
and it announces itself with `truncated`, `length` and `sha256`
(`work_store.py:231-234`).

There is no rotation, no size cap and no compaction for `history.jsonl` anywhere
in the subsystem.

### The commit-failed compensating entry

`_record` (`work_store.py:1107-1161`) writes the history line first, then
commits.  If the commit raises, it appends a second entry and re-raises:

```json
{
  "action": "work.commit_failed",
  "fields": [], "values": {},
  "did_not_land": "<the action that is void>",
  "reason": "<type(error).__name__>"
}
```

carrying the same `at`, `actor`, `item_id`, `revision`, `status`,
`requested_by`, `assigned_to` keys as an ordinary entry.  Only the exception
*type* is recorded, because the message can carry a path and a history line is
not the place to widen what the file holds (`work_store.py:1154-1156`).

### TIMELINE_FIELDS

A class attribute on `WorkStore` (`work_store.py:747-748`), 8 names:

```python
TIMELINE_FIELDS = ("requested_by", "assigned_to", "directed_by", "phase",
                   "session_id", "summary", "receipt", "did_not_land")
```

`read_timeline` computes `present = {key for key in TIMELINE_FIELDS if key in
raw}` (`work_store.py:764`) and sets `record_schema: "current" if present else
"legacy"` plus `unknown_fields: sorted(set(TIMELINE_FIELDS) - present)`
(`:789-790`).  An older entry does not carry them, and the reader says so
instead of rendering an empty value as if it were an observed one
(`work_store.py:744-746`).

### Context

`_normalize_context` accepts `directed_by`, `phase`, `session_id`, `summary`,
`receipt`.  **No caller in `src/` ever passes `context=`**: the only two
occurrences (`work_store.py:1063`, `:1102`) are the store forwarding its own
parameter.  `work_web.py:235`, `:253`, `:266` and `work_cli.py:325`, `:386`,
`:495`, `:499`, `:511`, `:521` all omit it.  Only
`tests/test_work_timeline.py` exercises it.  `PHASES` is advertised at
`GET /meta` to clients that cannot set one.

### Reading it back

`read_history` (`work_store.py:723-740`) — reads the whole file, skips blank
lines, **silently skips** lines that fail to parse, filters by `item_id` when
given, applies `entry.setdefault("values", {})` for pre-values entries, returns
`entries[-limit:][::-1]` (newest first).  Default `limit=100`.

`read_timeline` (`work_store.py:750-807`) — merges work history, cowork events
and cowork handoffs; sorts ascending by `(str(at or ""), source or "")`; returns
`entries[-limit:]` with `truncated: len(entries) > limit` and `count:
len(entries)`.  Actors pass through `cowork.resolve_actor(name, at=at)`.  Each
entry carries `source` = `"work_history"` \| `"cowork_event"` \|
`"cowork_handoff"`.  The envelope carries `item_id`, `title`, `requested_by`,
`assigned_to`, `status`, `revision`, `parent_id`, `entries`, `truncated`,
`count`.

## Outbox protocol

Implemented entirely in `work_cli.py`.  No HTTP equivalent.

### Directory layout

`--outbox <DIR>` is **required** and arbitrary; no default location and no
canonical path is named anywhere in the repo.

```
<DIR>/*.json                            queued files, non-recursive glob (work_cli.py:419)
<DIR>/applied/                          created mode 0o700 (work_cli.py:411-414)
<DIR>/rejected/                         created mode 0o700
<DIR>/applied/<name>.json               the original file, moved by path.replace (work_cli.py:429)
<DIR>/applied/<name>.json.reason.json   the receipt, chmod 0o600 (work_cli.py:423-428)
<DIR>/rejected/<name>.json
<DIR>/rejected/<name>.json.reason.json
```

Files are processed in `sorted()` order, `queued[: max(1, args.limit)]`
(`work_cli.py:419-420`) — `max(1, …)` means `--limit 0` or a negative limit
still processes one file.  `--limit` defaults to 50.  The receipt lands beside
the moved file in the same subdirectory and is written **before** the move.

### Allowlists, quoted exactly

```python
# What a queued file may edit. One line divides this list from the fields left
# out of it: the requester writes the request, and the executor writes the
# report. `progress_summary`, `blocker`, `started_at` and `completed_at` are
# the executor's account of the work, and a queue that could set them would
# let whoever asked for the work also describe how it went.
#
# Re-queueing and re-assigning are on the request side. An item sitting in
# `in_progress` that nobody is working is the exact defect P0 exists to
# prevent, and until this list included `status` the requester could see it
# and had no way to correct it.
OUTBOX_FIELDS = (
    "next_action",
    "detail",
    "assigned_to",
    "priority",
    "due_at",
    "status",
)

# The four stages a requester may move an item to. All four mean "nobody has
# started this, or nobody is going to" - they place work in the queue or take
# it out. The four left out (`in_progress`, `waiting`, `blocked`, `done`) are
# all claims about what happened, and only the executor may make those.
#
# Moving *out* of `in_progress` back into the queue is allowed and is not a
# claim: it disclaims progress rather than asserting it.
OUTBOX_STATUSES = ("backlog", "todo", "ready", "cancelled")
```

A status outside `OUTBOX_STATUSES` **refuses the whole file** rather than
being dropped from the payload: a queue that silently ignored the field would
leave the sender believing the board says something it does not.

```python
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
```
(`work_cli.py:264-283`)

### Payload — edit (`op` absent or `"update"`)

```json
{
  "op": "update",
  "work_id": "wi_846fb2146674febb",
  "next_action": "…",
  "detail": "…",
  "assigned_to": "local",
  "status": "ready",
  "expected_revision": 7
}
```

- `op` optional, defaults to `"update"`
- `work_id` required, a non-empty string
- `next_action`, `detail`, `assigned_to`, `priority`, `due_at`, `status`
  optional, each must be a `str`
- `status`, if present, must be one of `backlog`, `todo`, `ready`,
  `cancelled`; anything else refuses the file
- `expected_revision` optional; `int`, not `bool`

Everything else lands in `ignored = sorted(set(payload) - set(OUTBOX_FIELDS) -
{"work_id", "expected_revision", "op"})` and is reported, not applied —
`progress_summary`, `blocker`, `started_at` and `completed_at` among them.  Applied as `store.update_item(work_id, fields, actor=actor,
expected_revision=expected)` (`work_cli.py:386`).

### Payload — create (`"op": "create"`)

```json
{
  "op": "create",
  "title": "…",
  "assigned_to": "noa",
  "detail": "…",
  "next_action": "…",
  "priority": "high",
  "due_at": "2026-09-10T09:00:00Z",
  "parent_id": "wi_846fb2146674febb",
  "source_ref": "github:RLWRLD/worklog#12",
  "status": "backlog",
  "requested_by": "<must equal the draining actor, or be absent>"
}
```

Every allowlisted value must be a `str`.  `ignored = sorted(set(payload) -
set(OUTBOX_CREATE_FIELDS) - {"op", "requested_by"})` (`work_cli.py:295`) — so
`expected_revision` in a create payload lands in `ignored`; **creates carry no
concurrency check**.  `fields["requested_by"] = actor` is forced unconditionally
(`work_cli.py:322`).

### Receipt shape

`_outbox_result` (`work_cli.py:286-287`):

```json
{"file": "<name>", "ok": true, "reason": "<string>", "…": "extra"}
```

On success `extra` carries `work_id`, `revision`, `applied` (sorted field
names), `ignored`; `reason` is `"created"` or `"applied"`.  On most failures it
carries `ignored`.

The command's own stdout document (`work_cli.py:432-439`):

```json
{"ok": true, "actor": "…", "queued": 3, "applied": 2, "rejected": 1, "results": []}
```

`apply-outbox` always exits 0, even when every file was rejected
(`work_cli.py:440`).

### Every refusal

| Condition | `reason` | Line |
|---|---|---|
| file unreadable | `unreadable: <ExcName>` | 345-346 |
| > 64,000 bytes UTF-8 | `file is larger than 64000 bytes` | 347-348 |
| not JSON | `not JSON: <err>` | 349-354 |
| top level not an object | `top level must be a JSON object` | 355-356 |
| `op` not `create`/`update` | `unknown op <op!r>: expected 'create' or 'update'` | 358-362 |
| **update:** `work_id` missing or not a non-empty string | `work_id is required` | 366-368 |
| **update:** no allowlisted field present | `nothing to apply: only next_action, detail are read` | 372-376 |
| **update:** a field value is not a string | `<key> must be a string` | 377-379 |
| **update:** `expected_revision` not `int` / is `bool` | `expected_revision must be an integer or absent` | 381-383 |
| **update:** revision mismatch | `revision conflict, not merged: <err>` | 387-392 |
| **update:** any other store error | `<ExcName>: <msg>` | 393-396 |
| **create:** a field value is not a string | `<key> must be a string` | 296-298 |
| **create:** `status` outside `OUTBOX_CREATE_STATUSES` | `status <s!r> may not be set on create: only backlog, todo, ready` | 300-308 |
| **create:** `requested_by` names someone else | `requested_by must be <actor!r>: a queued file may not record someone else as the requester` | 310-321 |
| **create:** any store error (missing `title`/`assigned_to`, …) | `<ExcName>: <msg>` | 326-329 |

`_apply_one_outbox` and `_apply_one_create` are documented as never raising
(`work_cli.py:292`, `:342`): every outcome is a filed reason.  The 64,000-byte
check runs before the op dispatch, so it covers creates too; there is no
per-field size check beyond what the store enforces.

## CLI

Wired at `cli.py:15`, `cli.py:236` (`add_work_parser(subparsers)`) and
`cli.py:871` (`return run_work(args)`).  The parser is built in
`work_cli.py:74-164`; `work_command` is `required=True` (`work_cli.py:76`).

### Common flags — every subcommand

`_add_common` (`work_cli.py:167-179`):

| Flag | Default | Help |
|---|---|---|
| `--config-root PATH` | `None` | *APP_CONFIG_ROOT override; the store never reads or writes anywhere else* |
| `--actor NAME` | `None` | *Who is making the change (default: WORKLOG_ACTOR, else local-cli)* |

### Field flags — `create`, `upsert`, `update`

`_add_field_flags` (`work_cli.py:181-195`), from `FIELD_FLAGS`
(`work_cli.py:47-62`):

`--title`, `--detail`, `--status` (choices = `STATUSES`), `--priority` (choices
= `PRIORITIES`), `--requested-by`, `--assigned-to`, `--parent-id`, `--progress`
→ `progress_summary`, `--next-action`, `--blocker`, `--due-at`, `--started-at`,
`--completed-at`, `--source-ref`.

Plus `--clear FIELD` — `action="append"`, repeatable, choices =
`CLEARABLE_FIELDS = ("detail", "blocker", "parent_id", "due_at", "started_at",
"completed_at", "source_ref")` (`work_cli.py:63-71`).  `_fields`
(`work_cli.py:218-226`) collects the non-`None` value flags first and then sets
each `--clear` field to `None`, so **`--clear` wins over a same-named value
flag**.

### Expectation flags — `upsert`, `update`, `archive`

`_add_expectations` (`work_cli.py:198-205`): `--expected-revision INT` (*Fail
instead of overwriting a concurrent change*), `--expected-updated-at STR`.

### Subcommands

| Subcommand | Positional | Additional flags | Help |
|---|---|---|---|
| `create` | — | field flags | Create one work item (`:78-80`) |
| `upsert` | — | field flags, `--id` (`dest=item_id`), `--match-source-ref`, expectations, `--force-overwrite` (`store_true`) | Create, or update the item with this identity (`:82-93`) |
| `update` | `item_id` | field flags, expectations | Update one work item (`:95-99`) |
| `archive` | `item_id` | expectations | Soft delete one work item (`:101-104`) |
| `list` | — | `--status` (append, default `[]`, choices `STATUSES`), `--assigned-to`, `--parent-id`, `--include-archived` | List work items (`:106-111`) |
| `board` | — | `--assigned-to`, `--include-archived` | Group work items into the four-stage queue plus supporting columns (`:113-118`) |
| `meta` | — | — | Show the status schema and board layout (`:120-121`) |
| `apply-outbox` | — | `--outbox` (**required**), `--limit INT` (default 50) | Apply queued ticket edits dropped as JSON files, and file the results (`:123-131`) |
| `agent-token` | `name` | — | Issue a long-lived board session for one agent (`:133-137`) |
| `agent-revoke` | `name` | — | End one agent's sessions without touching the others (`:139-143`) |
| `agent-list` | — | — | Show the agent roster and how often each was revoked (`:145-148`) |
| `show` | `item_id` | — | Show one work item (`:150-152`) |
| `history` | — | `--limit INT` (default 100), `--item-id` | Read the append-only change history (`:154-157`); limit clamped to 1..500 at `:574` |
| `timeline` | `item_id` | `--limit INT` (default 200) | Show one item's activity with actors resolved and receipts linked (`:159-164`); limit clamped to 1..1000 at `:578` |

`--force-overwrite` help text: *"Deliberate last-write-wins when the item
already exists. Without it, updating an existing match requires
--expected-revision or --expected-updated-at."*

`agent-token` prints the token once, here and nowhere else
(`work_cli.py:459-468`), with the note *"Put this in the agent's environment. Do
not write it to a log or a receipt."*  Its response carries `subject`, `token`,
`csrf`, `expires_in_seconds` (`90 * 24 * 60 * 60`, `admin_store.py:216`) and
that note.

`board` (`work_cli.py:538-561`) emits `placed` and `aged_out` beside `shown`;
`placed + aged_out == shown` is the invariant that makes a silent disappearance
impossible (`work_cli.py:552-555`).

### Examples

```bash
# Delegate something
worklog work create --title "업무 현황 1차 구현" --requested-by hk \
    --assigned-to noa --priority high --next-action "스키마 확정"

# An agent reports progress, refusing to clobber a concurrent edit
worklog work update wi_846fb2146674febb --actor noa \
    --status in_progress --progress "설계 완료" --expected-revision 1

# Idempotent sync from an external reference.  Creating needs no expectation;
# updating an existing match requires one, so a concurrent edit is reported
# instead of overwritten.
worklog work upsert --match-source-ref "github:RLWRLD/worklog#12" \
    --title "리뷰 반영" --requested-by hk --assigned-to noa
worklog work upsert --match-source-ref "github:RLWRLD/worklog#12" \
    --status waiting --expected-revision 3

# A mechanical caller that genuinely wants last-write-wins must say so.
worklog work upsert --match-source-ref "github:RLWRLD/worklog#12" \
    --progress "동기화됨" --force-overwrite

# Read back
worklog work list --status in_progress --assigned-to noa
worklog work board --assigned-to hk
worklog work meta
worklog work show wi_846fb2146674febb
worklog work history --limit 20
worklog work timeline wi_846fb2146674febb

# Drain a queue of ticket edits
worklog work apply-outbox --outbox /var/spool/board-outbox --actor hk

# Soft delete
worklog work archive wi_846fb2146674febb --expected-revision 4
```

### Output and exit contract

Stdout always holds exactly one JSON document (`work_cli.py:7-10`):
`{"ok": true, …}` or `{"ok": false, "error": {"kind": …, "message": …}}`.
`_emit` uses `indent=2, sort_keys=True, ensure_ascii=False`
(`work_cli.py:229-230`).

`EXIT_CODES` (`work_cli.py:38-44`) and the fallback arms
(`work_cli.py:236-255`):

| Exception | `kind` | Exit |
|---|---|---|
| `WorkValidationError` | `validation` | 2 |
| `WorkNotFoundError` | `not_found` | 3 |
| `WorkConflictError` | `conflict` | 4 |
| `WorkCorruptionError` | `corruption` | 5 |
| `WorkLockTimeout` | `lock_timeout` | 6 |
| any other `WorkStoreError` | `store` | 1 |
| plain `ValueError` (today: `AdminStore`'s unknown-agent guard) | `argument` | 2 |
| success | — | 0 |

The `ValueError` arm sits **below** `WorkStoreError` deliberately
(`work_cli.py:243-255`): `WorkValidationError` is also a `ValueError`, so an arm
placed first swallowed every validated field in the CLI and relabelled it
`"argument"`.

## HTTP API

Routers are mounted in `web.py:22-25`.

### Work router — prefix `/api/v1/admin/work` (`work_web.py:49`)

| Method | Path | Auth | CSRF | Response |
|---|---|---|---|---|
| `GET` | `/meta` | `require_board_session` (`:181`) | no | `status_metadata()` plus `statuses`, `priorities`, `phases`, `cowork` (the full `registry_as_dict()`) — `work_web.py:173-189` |
| `GET` | `/items` | `require_board_session` (`:200`) | no | the `list_items()` payload with `items` truncated to `limit`.  Query: `include_archived: bool = False`, `status: list[str] \| None`, `assigned_to: str \| None`, `limit: int ge=1 le=2000 = 500` — `work_web.py:192-208` |
| `GET` | `/items/{item_id}` | `require_board_session` (`:213`) | no | `{"item": …, "roles": describe_roles(item, live_items)}` — `work_web.py:211-220` |
| `POST` | `/items` | `require_board_session` (`:225`) **+ `_require_may_name_only_self`** (`:232`) | **yes** (`:226`) | 201, `{"item": …}`.  `fields` from `body["fields"]` if a dict, else from `body` itself; `expected_*` stripped; `requested_by` defaults to the session actor — `work_web.py:223-237` |
| `PATCH` | `/items/{item_id}` | `require_board_session` (`:242`) **+ `_require_may_name_only_self`** (`:250`) **+ `_require_may_write`** (`:252`) | **yes** (`:243`) | `{"item": …}` — `work_web.py:240-255` |
| `POST` | `/items/{item_id}/archive` | **`require_super_admin_session`** (`:260`) | **yes** (`:261`) | `{"item": …}` — `work_web.py:258-268` |
| `GET` | `/items/{item_id}/timeline` | `require_board_session` (`:284`) | no | `read_timeline()`.  Query: `limit ge=1 le=1000 = 200` — `work_web.py:271-286` |
| `GET` | `/agents` | `require_board_session` (`:297`) | no | `{"agents": [… with open_items …], "quiet_after_seconds": 3600, "generated_at": …}` — `work_web.py:289-316` |
| `GET` | `/history` | `require_board_session` (`:325`) | no | `{"items": read_history(...)}`.  Query: `limit ge=1 le=500 = 100`, `item_id: str \| None` — `work_web.py:319-327` |

Mutations take `{"fields": {…}, "expected_revision": N}` or a bare field object.

**The one work route an agent cannot reach at all is
`POST /items/{item_id}/archive`** — the only one requiring
`require_super_admin_session`; an agent session (role `"agent"`) hits
`admin_web.py:143-144` → 403 `"super administrator access required"`.  Asserted
at `tests/test_work_api.py:436-444`, whose docstring gives the reason:
*"Archiving is the one board action that cannot be undone by its author."*
Agents also get 403 on `POST /items` and `PATCH /items/{id}` when the request
names another party, and on `PATCH` when the item is not theirs.

### Error translation

`_translated_errors` (`work_web.py:57-71`):

| Exception | HTTP |
|---|---|
| `WorkValidationError` | 400 |
| `WorkNotFoundError` | 404 |
| `WorkConflictError` | 409 |
| `WorkLockTimeout` | 503 |
| `WorkCorruptionError` | 503 |

Plus 400 from the request-surface checks and 401/403 from the session and CSRF
guards.

### Collection router — prefix `/api/v1/admin/collection` (`collection_web.py:27`)

Included here because it is where an agent session stops.  Every route is
`require_super_admin_session`, so every one returns 403 to an agent.

| Method | Path | Auth | CSRF | Response |
|---|---|---|---|---|
| `GET` | `/rules` | super admin (`:79`) | no | `registry_as_dict()` |
| `GET` | `/overview` | super admin (`:89`) | no | `collection_status.overview(...)`.  Query: `environment`, `limit ge=1 le=200 = 20` |
| `GET` | `/runs` | super admin (`:102`) | no | `collection_status.list_runs(...)`.  Query: `source`, `environment`, `limit ge=1 le=500 = 50` |
| `POST` | `/refresh` | super admin (`:121`) | **yes** (`:122`) | `{"screen": …, "caches_cleared": [...]}`.  Query: `screen` ∈ `overview\|runs\|coverage\|all`, default `all` |
| `GET` | `/coverage` | super admin (`:138`) | no | `collection_status.coverage(...)`.  Query: `start`, `end`, `source` (repeatable), `environment`, `group` ∈ `date\|weekday` |

### Admin router — no prefix (`admin_web.py`)

| Method | Path | Auth | CSRF | Response |
|---|---|---|---|---|
| `GET` | `/` | none | no | `static/service.html`, `Cache-Control: no-store` (`:231-234`) |
| `GET` | `/backoffice` | none | no | `static/admin.html`, `no-store` (`:237-240`) |
| `GET` | `/admin` | none | no | 308 redirect to `/backoffice` (`:243-245`) |
| `GET` | `/api/v1/session` | none | no | `authenticated`, `email`, `role`, `auth_method`, `csrf_token`, `google_login_available` (`:248-259`) |
| `GET` | `/api/v1/admin/session` | none | no | the above plus `setup_required`, `authorized`, `emergency_login_available` (`:262-276`) |
| `POST` | `/api/v1/admin/bootstrap` | none; 404 unless `EMERGENCY_LOGIN_ENABLED`, 409 if already configured | no | `{"ok": true}`, sets the session cookie (`:279-292`) |
| `POST` | `/api/v1/admin/login` | none; password, 404 if emergency login disabled, 401 on a bad password | no | `{"ok": true}`, sets the cookie, audits `admin.login` (`:295-310`) |
| `GET` | `/auth/google/login` | none | no | 302 to Google; `next` must be `/` or `/backoffice` (`:313-339`) |
| `GET` | `/auth/google/data/login` | super admin (`:344`) | no | 302 to Google with data scopes (`:342-364`) |
| `GET` | `/auth/google/callback` | none for `google_login`; super admin when `purpose == "google_data"` (`:390-391`), and the Google account must also be the super admin (`:433-435`) | no | 302; sets the cookie and audits `user.login`, or saves `google_token` and audits `connection.authorized` (`:367-446`) |
| `POST` | `/api/v1/logout` **and** `/api/v1/admin/logout` (two decorators, one function, `:449-451`) | `require_company_session` — `company_user` **or** `super_admin` | **yes** (`:453`) | `{"ok": true}`, deletes the cookie, audits `user.logout` (`:449-456`) |
| `GET` | `/api/v1/admin/settings` | super admin (`:461`) | no | `{"settings": …, "secrets": …}` (`:459-462`) |
| `PUT` | `/api/v1/admin/settings` | super admin (`:467`) | **yes** (`:468`) | `{"settings": …, "secrets": …}` (`:465-474`) |
| `PUT` | `/api/v1/admin/secrets/{name}` | super admin (`:479`) | **yes** (`:480`) | `{"name": …, "configured": true}` (`:477-486`) |
| `POST` | `/api/v1/admin/connections/{name}/test` | super admin (`:491`) | **yes** (`:492`) | `{"name": …, "result": …}`; 404 unsupported name, 409 not configured (`:489-526`) |
| `GET` | `/api/v1/admin/audit` | super admin (`:531`) | no | `{"items": read_audit(limit clamped 1..500)}` (`:529-532`) |

An agent session gets **403** on settings (GET/PUT), secrets, connection tests
and audit (`tests/test_work_api.py:446-459`), and **401** on logout —
`require_company_session` (`admin_web.py:116-120`) raises 401 for any role
outside `{company_user, super_admin}`, so an agent cannot invalidate its own
cookie through the API.

`GET /api/v1/admin/schedules` (`schedule_web.py:20-22`) is likewise super-admin
only.  `GET /healthz` and `GET /api/v1/timeline` (`web.py:38-109`) belong to the
timeline database, not the board; the latter requires a company session.

### Session mechanics

| Aspect | Value | Where |
|---|---|---|
| Cookie name | `hk_work_assistant_session` | `admin_web.py:21` |
| Cookie flags | `httponly=True`, `samesite="lax"`, `secure` from `ADMIN_SESSION_SECURE` (default `false`), `max_age = exp - iat` | `admin_web.py:164-180` |
| Token format | `b64url(json) + "." + b64url(HMAC-SHA256(session_key, encoded))` | `admin_store.py:290-314` |
| Payload | `{sub, email, role, auth_method, iat, exp, csrf}`, plus `gen` when `sub` starts with `agent:` | `admin_store.py:310-311` |
| Signing key | `credentials/admin-session-key`, 32 random bytes, created on first use | `admin_store.py:205-208` |
| Agent lifetime | `AGENT_SESSION_SECONDS = 90 * 24 * 60 * 60` | `admin_store.py:216` |
| Roster | `AGENT_NAMES = ("noa", "boa", "doa", "roa", "soa")` | `admin_store.py:222` |
| Subject prefix | `AGENT_SUBJECT_PREFIX = "agent:"` | `admin_store.py:217` |
| Verification | `hmac.compare_digest`, non-empty `sub`, `exp` in the future, and for agents `payload["gen"]` equal to the current generation | `admin_store.py:316-336` |
| Revocation | `revoke_agent` increments the subject's counter in `credentials/agent-session-generations.json` | `admin_store.py:253-271` |

`agent_subject` (`admin_store.py:238-251`) accepts either `noa` or `agent:noa`,
strips the prefix, and raises `ValueError(f"unknown agent: …")` for anything off
the roster — the roster is closed so a typo cannot quietly mint a sixth identity
(`admin_store.py:218-221`).

`agent_name(current)` (`admin_web.py:49-54`) returns the bare name only when
`role == "agent"` **and** `sub` starts with `agent:`; otherwise `None`, meaning
a person.  `session_actor(current)` (`admin_web.py:57-72`) returns the bare
agent name, else `email`, else `sub`, else `EMERGENCY_ACTOR` — the prefix is
dropped so one party does not get two spellings in one history.

`_emergency_subject` (`admin_web.py:75-113`) refuses a declared actor containing
`@`, refuses anything starting with `agent:` or in `AGENT_NAMES` (the whole
namespace, because the actor pattern permits a colon and `agent:noa` walked past
a check that only knew `noa`), and otherwise requires `EMERGENCY_ACTOR_PATTERN`
(`admin_web.py:26`).

`DIRECTING_PARTIES = frozenset(AUTHORITY_ORDER) = {hk, ari, mori}`
(`cowork.py:54-57`).

## Exceptions

All defined at `work_store.py:123-144`; `WorkStoreError` subclasses
`RuntimeError` and `WorkValidationError` additionally subclasses `ValueError`.

| Exception | Meaning |
|---|---|
| `WorkStoreError` | base class for every work-store failure |
| `WorkValidationError` | a caller supplied an unusable field, value, or relationship |
| `WorkNotFoundError` | the requested work item does not exist |
| `WorkConflictError` | another writer changed the item since the caller last read it |
| `WorkCorruptionError` | the stored document is unreadable; it is left untouched on disk |
| `WorkLockTimeout` | another writer held the store lock for too long |
