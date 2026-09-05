# Delegated work tracking

The 업무 보드 is one JSON file.

Not a database, not a service, not a queue with a broker: a single document at
`$APP_CONFIG_ROOT/work/items.json` holding every work item, a second file beside
it holding every change ever made to it, and a lock file that serializes the
writers.  The backoffice page, the `worklog work` CLI, and any agent script all
open the same three files.  There is no server between them and the data, and
nothing reconciles them afterwards, so everything in this subsystem is a
consequence of that one fact: correctness has to come from the file format, the
lock, and the code that reads and writes both.

This document is the narrative.  Exact field constraints, both enums, every
validation function, every CLI flag and every HTTP route live in
[work-board-reference.md](work-board-reference.md).

The board answers four questions: what was delegated, what is being worked on
now and by whom, what remains, and what was finished recently.  Team views, 1:1
views, Slack/Notion inference, and collector operation dashboards are out of
scope.

## Where the data lives

Everything sits under `APP_CONFIG_ROOT` (default `~/.config/hk-work-assistant`,
`/config/hk-work-assistant` in the containers).  Nothing is written to Git, the
settings database, or `/data`.

| Path | Format | Purpose |
| --- | --- | --- |
| `work/items.json` | versioned JSON object, mode `0600` | every work item |
| `work/history.jsonl` | append-only JSON Lines, mode `0600` | change history |
| `work/items.lock` | empty lock file, mode `0600` | `flock` for writers |

The directory is created with mode `0700` and re-`chmod`ed on every
`WorkStore()` construction (`src/rlwrld_worklog/work_store.py:604-611`), because
the store's only access control is the filesystem's — see *Authorization* below.

The timeline additionally reads, and never writes, `cowork/events.jsonl`,
`cowork/handoffs/*.json` and the mailbox cursors under `cowork/mailbox/`
(`work_store.py:824-892`).  That read is best-effort: a missing or unreadable
cowork tree yields no entries rather than failing the timeline
(`work_store.py:826-828`).

`items.json` has exactly four top-level keys and no others:

```json
{
  "version": 2,
  "revision": 12,
  "updated_at": "2026-09-01T02:56:27.290568+00:00",
  "items": [ { "id": "wi_846fb2146674febb", "title": "…", "revision": 3, "…": "…" } ]
}
```

A missing or extra top-level key is corruption, not something to repair
(`work_store.py:532-537`).

### The document is validated in full on every read

`read_document` (`work_store.py:621-654`) is the entry point for every read
*and* the first step of every write.  It checks the exact top-level and item key
sets, every field's type and length, item revisions, unique ids, timestamp
format and the ordering of the stamps the store owns, and the whole parent graph
— missing, archived, self-referential, cyclic, or too-deep parents.

Nothing stored is normalized, defaulted, repaired, or dropped.  Any violation is
raised as `WorkCorruptionError`, `items.json` is left byte-for-byte unchanged,
and because validation happens before the lock does any work, a mutation that
fails this way appends nothing to the history either.  The operator repairs or
removes the file by hand.

Reading is dispatched on the stored `version`, and each version validates
against the enum *that version could have written*, so widening the enum later
can never retroactively bless a file that was already invalid when it was
written (`work_store.py:526-531`).  A file declaring `version: 1` while
containing a `todo` item is corruption.

| version | statuses |
|---|---|
| 1 | `backlog`, `ready`, `in_progress`, `waiting`, `blocked`, `done`, `cancelled` |
| 2 | version 1 plus `todo` |

`_migrate_v1_to_v2` (`work_store.py:569-579`) is a total, lossless widening:
every v1 status keeps its meaning and `todo` is new, so no item is relabelled or
dropped.  It is written out rather than assumed so that the widening is stated
somewhere.  Reading migrates **in memory only** and reports `migrated_from`; the
file itself is rewritten by the next ordinary write, and the backoffice says an
upgrade is pending until then.

### Writes are atomic; reads are not locked

A writer takes an exclusive `flock` on `items.lock` for the whole
read-modify-write cycle (`work_store.py:1234-1261`), waiting up to 10 seconds
before raising `WorkLockTimeout`.  The lock is held per open file description,
so it must never be nested; every public mutation takes it exactly once and
calls the `_within` helpers underneath (`work_store.py:1236-1240`).

The document is replaced through a temporary file in the same directory
(`_atomic_private_write`, `admin_store.py:61-78`), so a reader sees either the
old file or the new one and never a partial one.  That is why reads take no
lock.  The containing directory is not `fsync`ed after the rename, so the rename
itself is not crash-durable on every filesystem; the file contents are.

## What an item is, and how it moves

An item has 19 fields, fixed exactly by `_blank_item()`
(`work_store.py:365-386`).  Fourteen of them a caller may write; five —
`id`, `created_at`, `updated_at`, `revision`, `archived_at` — belong to the
store, and a caller that tries to set one is rejected before anything else
happens (`work_store.py:333-335`).  Three are required to create anything at
all: `title`, `requested_by`, `assigned_to`.

`status` is the board's primary classification, and it is two things wearing one
field.  Four **queue stages** say where the work is — `in_progress` (진행 중),
`ready` (다음 할 일), `todo` (해야 할 일), `backlog` (백로그).  Four **supporting
statuses** say why it is not moving or that it has left the queue — `waiting`
(대기), `blocked` (막힘), `done` (완료), `cancelled` (취소).  Collapsing the two
kinds into one list would lose that difference (`work_store.py:1302-1305`), so
`describe_roles` reports each item's `stage.kind` as `queue` or `condition`.

The board layout lives in the schema rather than in the page
(`work_store.py:65-69`), because the board must be a **total partition** of the
status set: `BOARD_COLUMNS` gives every status exactly one column,
`_validate_board_columns()` enforces that at import time — so adding a status
without giving it a column fails the process on startup instead of being noticed
later by an operator wondering where an item went — and any status a client's
own column list fails to claim goes to an explicit `미분류` residue column.  An
item can never fall between two columns and disappear.  `GET
/api/v1/admin/work/meta` serves the labels and the columns so the page derives
its layout from the schema, and `list_items` returns `total`, `status_counts`
and `withheld` for the whole live set beside the filtered slice, so a filtered
view can never be mistaken for the complete one.

Three timestamps move on their own (`_apply_status_timestamps`,
`work_store.py:1499-1512`): `started_at` is stamped on the first move to
`in_progress`, `completed_at` on the first move to `done`/`cancelled`, and
`completed_at` is cleared when an item leaves a terminal status without the
caller naming `completed_at` in the same change.

`parent_id` must reference an existing, unarchived item.  Self-parenting,
cycles, and chains deeper than eight levels are rejected on write and reported
as corruption on read.

**Archiving is a soft delete.**  `archived_at` is stamped, the item stays in the
document, and its live children must be archived first — which is also why a
live child of an archived parent is treated as corruption on read: the write
path cannot produce it, so its presence means the file was written by something
else (`work_store.py:503-506`).

## Concurrent writers

Two agents can edit at the same time without silently overwriting each other:

- The `flock` serializes the read-modify-write cycles.
- Each item carries a `revision`.  Pass `expected_revision` (preferred) or
  `expected_updated_at` on an update or archive and the store refuses with a
  conflict when the item has moved on.
- `upsert` against an existing match **requires** one of those expectations.
  Without one it fails as a validation error rather than overwriting; a caller
  that intends last-write-wins passes `force_overwrite` / `--force-overwrite`.
  An expectation supplied alongside force is still checked — force only waives
  the requirement to supply one.

## Who may write what

Two doors reach the same store, and only one of them has locks on it.

### The web API

Reads are open to everyone who works here: `require_board_session`
(`admin_web.py:123-136`) admits a super-admin session or any agent session, and
the reason is written into the guard — several wrong calls were made by someone
who could not see a screen and reasoned from the clock instead, so withholding a
view costs more than it protects.  A `company_user` session is not a board
session and gets 403.

What stays shut is the small set of things that cannot be undone, expressed as
three separate checks on top of the session:

- **`_require_may_write`** (`work_web.py:91-107`), applied on PATCH.  An agent
  may write an item only when it is the item's `assigned_to`.  Rule 8 says an
  executor does not hand work to another executor; this is that sentence as a
  permission.
- **`_require_may_name_only_self`** (`work_web.py:109-141`), applied on POST and
  PATCH.  An agent may put its own name in `assigned_to` or `requested_by` and
  no other name.  `_require_may_write` asks who owns the row *now*, which is the
  wrong question at two moments: at creation there is no owner yet, and on
  update the field being changed may be the ownership itself.  Both holes were
  measured open before this guard existed — creating an item assigned to someone
  else returned 201, and moving one's own item onto another agent by PATCHing
  `assigned_to` returned 200.  `requested_by` is covered for the same reason: it
  is the field that says who directed the work, so accepting it from the request
  body let an agent sign a direction with a judgement role's name.
- **`require_super_admin_session`** on archive (`work_web.py:260`).  It is the
  only work route an agent cannot reach at all, because archiving is the one
  board action that cannot be undone by its author.

Every mutation also carries the session CSRF token in `x-csrf-token`
(`admin_web.py:148-151`) and appends a record to the admin `audit.jsonl` — the
fact of the change only: `item_id`, `revision`, `status`, `assigned_to`, and
never the item's free text (`work_web.py:159-170`).

Agent identity comes from a signed session token minted against a closed roster
of five names — `noa`, `boa`, `doa`, `roa`, `soa` (`admin_store.py:222`) — so a
typo cannot quietly mint a sixth identity.  Revocation increments a per-agent
generation counter, which is what kills a token that is still correctly signed
and still unexpired (`admin_store.py:253-271`, `admin_store.py:329-331`).

Both guards contain an escape hatch for the directing parties `hk`, `ari` and
`mori`.  Since the roster restricts issuable tokens to the five executor names,
**that branch is unreachable for any token the system can currently issue**;
judgement roles reach the board through a super-admin or emergency session,
where `agent_name` returns `None` and the first clause returns anyway.

### The CLI has no authorization at all — unresolved

`worklog work` reaches the same store with the same *validation* and no
authorization whatsoever.  There is no session, no role, no CSRF, no ownership
check, and no audit entry.  The actor is a free-text string:

```python
def _actor(args): return args.actor or os.environ.get("WORKLOG_ACTOR") or "local-cli"
```
(`work_cli.py:214-215`)

The only thing that inspects it is `_normalize_actor`
(`work_store.py:192-200`), which checks the *shape* of an identifier, not
anyone's authority to use it.  The store layer holds none of the rules:
`create_item`, `update_item`, `upsert_item` and `archive_item`
(`work_store.py:896`, `913`, `936`, `991`) take `actor` as an ordinary keyword
argument.  Every role rule in this subsystem lives in `work_web.py`.

Concretely, anyone who can run the process as the owner of `APP_CONFIG_ROOT`
can:

- archive any item (`work_cli.py:520-528`) — the one action the web reserves for
  a super-admin;
- write any value into `requested_by` and `assigned_to` (`work_cli.py:494-519`,
  via `FIELD_FLAGS` at `work_cli.py:47-62`), so a CLI edit can be signed `hk`,
  `ari` or `mori` — the exact forgery `_require_may_name_only_self` exists to
  stop on the web;
- **mint a board session token for any agent on the roster** —
  `worklog work agent-token <name>` calls `admin.issue_agent_session(args.name,
  actor=_actor(args))` at `work_cli.py:457` and prints the token, and
  `agent-revoke` (`work_cli.py:470-475`) ends any agent's sessions;
- redirect the whole store elsewhere with `--config-root`
  (`work_cli.py:168-173`, `work_cli.py:208-211`).

Everything that returns 403 on the web passes on the CLI.

The posture that makes this coherent today is filesystem-level rather than
application-level: root and `work/` at `0700`, the three files at `0600`, and a
loopback-only server, so anyone with shell access as that user is already inside
the boundary.  The suite states the situation in its own words at
`tests/test_work_api.py:670-673`: *"every assignment would have to go through
the CLI - the door that does no checking at all."*

**The unresolved part is the record, not the reach.**  Both doors append to the
same `history.jsonl` with no marker of origin, so a reader cannot tell an
authenticated `hk` edit from a `--actor hk` CLI edit.  The `actor` field is
authoritative only for web-originated edits, and nothing in the file says which
those are.  This is a live gap; it is not documented here as a design.

### Agents cannot verify the collection dashboard — unresolved

`require_board_session` admits agents; `require_super_admin_session`
(`admin_web.py:139-145`) does not, and raises 403 `"super administrator access
required"` at `admin_web.py:143-144` for any role other than `super_admin`.

Every route under the `/api/v1/admin/collection` prefix
(`collection_web.py:27`) uses the second guard:

| Method | Path | Guard |
| --- | --- | --- |
| `GET` | `/api/v1/admin/collection/rules` | `collection_web.py:79` |
| `GET` | `/api/v1/admin/collection/overview` | `collection_web.py:89` |
| `GET` | `/api/v1/admin/collection/runs` | `collection_web.py:102` |
| `POST` | `/api/v1/admin/collection/refresh` | `collection_web.py:121` |
| `GET` | `/api/v1/admin/collection/coverage` | `collection_web.py:138` |

So an agent session gets 403 on all five, and the 수집 현황 page an agent can
open in the backoffice shell has no data behind it.  An agent asked to check
whether a collection run happened cannot answer from the dashboard and has to
read the archive on disk instead.  The module docstring
(`collection_web.py:1-13`) says these routes require a super-admin session "like
the delegated-work API" — which was true when it was written and is no longer:
the work API moved to `require_board_session` and these did not.  Whether that
is an intended boundary or an omission has not been decided.

## The outbox: a queue for parties that cannot hold the lock

Writing to the board means taking `items.lock`, which means being a process on
this machine with access to `APP_CONFIG_ROOT`.  A requester who is neither — a
job on another host, a mail-driven flow, anything that can drop a file but
cannot run the store — writes a JSON file into an outbox directory instead, and
`worklog work apply-outbox --outbox <DIR>` drains it under the lock on their
behalf.

The queue is therefore a *request*, not a command, and the design follows from
that:

- **The file is data, never a command.**  A malformed one is refused, not
  interpreted and not executed (`work_cli.py:349-354`).
- **Nothing fails quietly.**  Every file ends up in `applied/` or `rejected/`
  beside a `<name>.json.reason.json` receipt, because the worst state is the one
  where someone drops a file and nothing happens anywhere
  (`work_cli.py:404-408`).  The receipt is written before the file is moved.
- **An edit may set only `next_action` and `detail`.**  The other fields are the
  executor's own account of the work; a queue that could set `status` or
  `progress_summary` would let the requester write the report as well as the
  request, and the board would no longer say who observed what
  (`work_cli.py:258-262`).
- **A create may set the request but not the account of it**, and its `status`
  only among `backlog`, `todo`, `ready` — the stages that mean nobody has
  started this.  A queue that could file an item straight to `in_progress` or
  `done` would let a requester close work no one did (`work_cli.py:264-283`).
- **The draining actor is the requester, by construction.**  A file naming
  someone else in `requested_by` is refused rather than quietly corrected,
  because a board that misattributes who asked for the work is worse than a
  rejected file (`work_cli.py:310-321`).
- **A revision conflict is never merged.**  Merging here would silently
  overwrite a change the requester never saw, and that decision is theirs
  (`work_cli.py:387-392`).

Fields the allowlist does not cover are reported back in the receipt's
`ignored` list rather than dropped in silence.  `--outbox` is required and takes
an arbitrary directory; no canonical location is defined anywhere in the repo.

There is no HTTP equivalent of the outbox.

## The history guarantee

`work/history.jsonl` is append-only, one JSON object per line, and it records
what each changed field **was and what it became** — not merely which field
names changed.

Two ordering decisions carry the guarantee.

**The history line is written before the document is committed**
(`work_store.py:1107-1161`).  The two files have no transaction across them, so
one has to go first, and which one goes first decides what a crash destroys.
Committing first destroys the record: the item lands, the append fails, and the
change exists with nothing saying who made it or what it replaced — while the
caller sees a traceback and reasonably tries again.  That is not hypothetical;
on 2026-09-04 the window was open from 08:35:36Z to 08:36:06Z, two `work create`
calls came through it, both raised, both had already written their item, and one
of the resulting duplicates has to be archived by hand.  History-first inverts
it: if the append fails nothing is committed and the traceback means what it
says.  The mirror risk — the entry lands and the commit fails — is
over-recording rather than loss, and the reader is told, because a
`work.commit_failed` entry follows naming the action that did not land.  A
history that admits an extra line is repairable; a change with no history is
not.

**A field that was absent and a field that held an empty string are different
facts** (`work_store.py:1082-1084`), so `before` is captured from the live item
ahead of the update and an absent field is recorded as `{"present": false}`
rather than as null.

Values are kept verbatim up to `MAX_HISTORY_VALUE`, which is *derived* from the
board's own limits rather than chosen: `max(maximum for _, maximum in
TEXT_FIELDS.values())`, currently 8,000, the largest value the board can hold
(`work_store.py:217-235`).  It was previously a flat 2,000 while `detail`
accepts 8,000, and a 3,615-character edit on 2026-09-04 reached the stream
clipped.  The test asserts the *relation* rather than the number, so raising a
field's cap past the history's fails the test instead of the history.  A value
that is clipped anyway says so, and carries its original length and SHA-256.

Entries written before the timeline fields existed are reported as
`record_schema: "legacy"` with their missing fields named, rather than
back-filled with a guess (`work_store.py:744-746`).

Two consequences worth stating:

- `history.jsonl` now holds a full-fidelity copy of every text edit ever made to
  the board.  It has **no rotation, no size cap and no compaction** anywhere in
  the subsystem.  Its protection is the file mode, `0600`.
- API mutations additionally append to the admin `audit.jsonl`; CLI mutations do
  not.

## Backoffice page

`업무 현황` in the backoffice sidebar renders the columns the server describes,
falling back to a hard-coded copy of the same total partition only if the `meta`
request fails (`static/admin.html:729-747`).  Each card carries the assignee,
priority, progress, next action, blocker, and last update time.  Items can be
added, edited, moved between statuses, and archived without a page reload.

Read-only data is polled every 15 seconds (`static/admin.html:748`), and a poll
never re-renders the board while an edit form is open or a write is in flight.
The page also surfaces `withheld` — how many archived items are hidden, and how
many of those were still unfinished — because archiving hides an item from every
view at once whatever state it was in (`work_store.py:700-713`).

Priority labels are the one thing the page hard-codes and never fetches
(`static/admin.html:737`); the server publishes no priority label map.

No sample or default items are seeded anywhere; the board starts empty.

## Known inconsistencies

Real contradictions between the code and its own comments or docstrings, found
by audit and left in place rather than fixed as part of this document.

- **`work_store.py:1188-1191`** — `_append_history`'s first paragraph says the
  item's free text "is still never written here".  The next paragraph reverses
  it and the code implements the reversal: `values` carries `detail`,
  `progress_summary`, `blocker` and `next_action` verbatim up to 8,000
  characters.  The stale paragraph should go.
- **`work_store.py:266-267`** — `MAX_CONTEXT_SUMMARY`'s comment asserts the same
  now-false claim: "`detail` and `progress_summary` still never reach the
  history stream".  They do.
- **`work_web.py:1-7`** — the module docstring says the work API requires a
  super-administrator session.  Only the archive route does; everything else
  moved to `require_board_session`.
- **`collection_web.py:1-13`** — says the collection routes require a super-admin
  session "exactly like … the delegated-work APIs".  Half of that comparison is
  no longer true (see above).
- **`work_web.py:207` vs `work_store.py:689`** — `GET /items` slices `items` to
  `limit` after the store has already computed `count` as the unsliced length,
  so a client with more matches than `limit` sees `count > len(items)` with no
  flag saying so.  That is the class of silence the `withheld` block exists to
  prevent.
- **`work_web.py:133-134` vs `work_store.py:195`** —
  `_require_may_name_only_self` compares actor names case-sensitively while
  `_normalize_actor` lowercases.  Fail-closed today (`NOA` from `noa`'s session
  is refused), but the two normalizations should be one.
- **`cowork.py:282` vs `work_store.py:120`** — `cowork._WORK_ID` is
  `^wi_[0-9a-f]{8,}$`, looser than the store's `^wi_[0-9a-f]{16}$`.  A directive
  naming `wi_abcdef01` passes the directive format check and is refused later
  only because the item cannot be read.
- **The timeline context mechanism is unreachable.**  `_normalize_context`,
  `PHASES`, `directed_by`, `session_id`, `summary` and `receipt` are fully
  implemented and tested, but no shipped caller passes `context=` — not
  `work_web.py:235`, `253`, `266`, nor any `work_cli.py` call site.  `GET
  /meta` advertises `phases` to clients that cannot set one.
- **`work_cli.py:420`** — `apply-outbox` uses `queued[: max(1, args.limit)]`, so
  `--limit 0` still processes one file, and the command exits 0 however many
  files were rejected.
