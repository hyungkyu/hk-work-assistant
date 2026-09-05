# Ari–Mori–Moa Markdown mailbox protocol (v1)

This document is in two parts, and the split matters more than anything else in
it.

**Part 1 is behaviour.** Everything in it is implemented in
`src/rlwrld_worklog/cowork.py` and its callers, and is covered by tests. Every
claim carries the `file:line` that makes it true.

**Part 2 is specification.** It is the protocol as designed. None of it is
implemented. It is kept here because it is the agreed design and because
deleting it would hide the gap rather than close it, but a reader must not read
Part 2 as a description of what the system does. Each item says what the code
does instead.

The one thing to carry away before reading further: **no code in this
repository writes, reads, lists, or parses a mailbox message file.** There is no
mailbox writer, no mailbox reader, no directory scanner, no front-matter parser.
`cowork.py` is a library of checks that a mailbox reader would call if one
existed. The strings `to-ari`, `to-mori` and `to-moa` appear nowhere in `src/`
or `scripts/`. The only mailbox I/O that runs is `stat()` on two hidden mark
files (§1.5).

The backoffice work item remains the source of truth for status, ownership, and
optimistic revision.

---

# Part 1 — What the code implements

## 1.1 Identities and authority

- `hk`: decision maker and requester
- `ari`: primary operator for requirements, rules, review criteria, and handoff
  verification
- `mori`: Cowork deputy. Plans, manages work items, and may direct Moa when Ari
  is away. Cannot run commands on the host, which is why Moa exists.
- `moa`: Claude Code executor. Runs shell, code, tests, builds and deploys.
  Never creates its own work and never widens a directive's scope.

Defined at `cowork.py:39-51`. Authority when directives conflict is `hk` >
`ari` > `mori` — `AUTHORITY_ORDER` at `cowork.py:54`, resolved by
`authority_rank()` (`cowork.py:60-65`) and `resolve_conflict()`
(`cowork.py:68-70`). `moa` is deliberately absent from that tuple, so
`authority_rank("moa")` ranks last and `resolve_conflict(["moa", ...])` returns
`None`. `DIRECTING_PARTIES` (`cowork.py:57`) is the frozenset of the three
parties whose messages `moa` will act on at all.

These names are published to clients through `registry_as_dict()`
(`cowork.py:442-455`), served at `GET /api/v1/admin/work/meta` under the
`cowork` key (`work_web.py:188`).

**Read §1.7 before using any of this.** The four parties above are not the
roster the running system issues sessions to.

## 1.2 The directive validator

`validate_directive()` (`cowork.py:328-434`) is a fail-closed check of one
message's front matter against one work item read from the backoffice. It
returns a `DirectiveVerdict` (`cowork.py:286-311`) and never executes anything.

It has **no caller in `src/`**. Its only callers are `tests/test_cowork.py`.
Nothing in the running system validates a directive, because nothing in the
running system reads a directive.

The work item is passed in by the caller, from the work store, and is not taken
from the message: "The message states what the sender believed; the item is what
is true now, and a disagreement refuses rather than overwrites"
(`cowork.py:338-339`).

### Front-matter fields the validator reads

| Field | Rule | On violation | Line |
|---|---|---|---|
| `protocol_version` | must be the integer `1` | **refuse** | `cowork.py:371-372` |
| `message_id` | must be present and a string | **refuse** if missing | `cowork.py:373-374` |
| `message_id` | should match `msg_` + at least 16 lowercase hex | **warning, not refusal** | `cowork.py:375-380` |
| `message_id` | must not be in `already_processed` | **refuse** | `cowork.py:381-382` |
| `from` | must be present | **refuse** | `cowork.py:384-385` |
| `from` | must be in `DIRECTING_PARTIES` (`hk`/`ari`/`mori`) | **refuse** | `cowork.py:386-389` |
| `to` | must equal the recipient (default `moa`) | **refuse** | `cowork.py:390-392` |
| `type` | must be present | **refuse** | `cowork.py:394-395` |
| `type` | must be one of `MESSAGE_TYPES` | **refuse** | `cowork.py:396-397` |
| `type` | must be in `TYPES_CARRYING_AUTHORITY` — only `ASSIGN` | **refuse** | `cowork.py:398-402` |
| `work_id` | required for `ASSIGN`/`PROGRESS`/`REVIEW_REQUEST`/`HANDOFF` | **refuse** | `cowork.py:404-405` |
| `work_id` | must match `^wi_[0-9a-f]{8,}$` | **refuse** | `cowork.py:406-407` |
| `expected_revision` | must be a real `int` (a `bool` is rejected) | **refuse** | `cowork.py:355-356`, `426-427` |
| `expected_revision` | must equal the item's current `revision` | **refuse** | `cowork.py:428-432` |
| `reply_to` | must be present, even as `null` | **warning, not refusal** | `cowork.py:409-410` |
| `allow_write`, `allow_commit`, `allow_build`, `allow_deploy` | must be a boolean | **warning**, and treated as not granted | `cowork.py:412-414` |

Two of these are stated as requirements in Part 2 and enforced as warnings here.
That is deliberate, and it is pinned by tests, so do not "fix" it without
reading them:

- **`message_id` format is a warning.** `cowork.py:376-377` — `# Not fatal: the
  id still addresses the message. Recorded so a malformed id cannot pass
  unnoticed.` Pinned by `tests/test_cowork.py:212`
  `test_a_malformed_message_id_warns_but_does_not_block`, whose docstring reads
  *"Ari's own assignment used a non-conforming id; it must still be
  actionable."*
- **`reply_to` presence is a warning.** The warning text at `cowork.py:410`
  calls the field required while not refusing on it. Pinned by
  `tests/test_cowork.py:219`
  `test_a_missing_reply_to_warns_but_does_not_block`.

A verdict carrying warnings can still be `accepted`. Warnings are returned in
`DirectiveVerdict.warnings` for the caller to record.

### Fields the protocol requires that the validator never reads

`created_at`, `requires_ack`, and `subject` are listed as required front matter
in Part 2, item D, and no code reads them. A message missing all three is
accepted.

### Cross-check against the work item

When `work_id` is present, four further checks run (`cowork.py:416-432`):

1. the item could not be read at all → refuse (`:418`);
2. `item["id"]` differs from the directive's `work_id` → refuse (`:421`);
3. `item["assigned_to"]` is not the recipient → refuse (`:424`);
4. `expected_revision` missing → refuse (`:427`); mismatched → refuse, with the
   reason ending *"refusing rather than overwriting"* (`:428-432`).

### Message types

`MESSAGE_TYPES = ("ASSIGN", "ACK", "PROGRESS", "QUESTION", "REVIEW_REQUEST",
"HANDOFF")` (`cowork.py:271`). `TYPES_REQUIRING_WORK_ID` covers `ASSIGN`,
`PROGRESS`, `REVIEW_REQUEST` and `HANDOFF` (`cowork.py:275`).
`TYPES_CARRYING_AUTHORITY = ("ASSIGN",)` (`cowork.py:279`) — an `ACK` or a
`PROGRESS` is a report, not an instruction, and validating one returns
`accepted=False` with the reason *"a … carries no authority to act"*. Pinned by
`tests/test_cowork.py:156-160`.

`QUESTION` is a member of that tuple and nothing more: no code emits a message
of any type. See Part 2, item E.

### Replay protection

`already_processed` is a **parameter of the validator** (`cowork.py:333`), not a
file and not a stored set:

```python
if message_id is not None and message_id in set(already_processed):
    reasons.append("this message has already been processed; refusing to run it twice")
```
`cowork.py:381-382`

No code in this repository populates it. The only exercise of the path is
`tests/test_cowork.py:185`
`test_an_already_processed_message_is_never_run_twice`.

Note the name collision with the liveness marks in §1.5: `.processed-<agent>` is
a *mark file whose mtime is read*, and it has no relationship to the
`already_processed` set beyond the shared word. Neither is a cursor. There is no
cursor mechanism in this repository, and no record of one having been replaced —
`git log -S".cursor-"` and `git log -S".processed-"` both land on the single
commit `5800e86`, which introduced both names together in one tuple.

## 1.3 What an unattended session may do

`AUTONOMOUS_BASELINE = ("read", "investigate", "report", "test")`
(`cowork.py:255`) is what a directive grants with no flags at all. Each further
action is a separate opt-in that the `ASSIGN` must state as a literal `true`:

| Front-matter flag | Grants |
|---|---|
| `allow_write: true`  | editing files in the working tree |
| `allow_commit: true` | creating local commits |
| `allow_build: true`  | building images |
| `allow_deploy: true` | restarting or deploying services |

`GRANT_FLAGS` at `cowork.py:258`, `GRANTED_ACTIONS` at `cowork.py:260-265`,
applied by `granted_actions()` (`cowork.py:314-325`). Only `is True` grants: a
missing flag, the string `"true"`, `1`, `[]` and `{}` all leave the action
ungranted (`cowork.py:323`), pinned by `tests/test_cowork.py:129-132`. Grants
appear on a verdict only when it is accepted (`cowork.py:363`).

```python
NEVER_AUTONOMOUS = ("sudo", "delete", "policy_change", "security_control_change", "push")
```
`cowork.py:269`. There is deliberately no flag that turns these on: "These need
a human decision every time" (`cowork.py:267-268`). **`push` is on that list.**
See Part 3.

## 1.4 Identities in historical records

Names that appear in older records — `codex`, `claude-code`, `claude-cowork` —
predate the Ari/Mori/Moa naming and are **not** rewritten onto today's parties.
`resolve_actor()` (`cowork.py:201-249`) resolves a name through the append-only
`ACTOR_ALIASES` registry (`cowork.py:105-160`) and reports one of `declared`,
`inferred`, or `unresolved` together with the basis for that answer.

A bare party name in a record written before `NAMING_EFFECTIVE_FROM =
"2026-09-02"` (`cowork.py:166`) resolves as `inferred`, never as `declared`
(`cowork.py:221-233`). A naive timestamp is read as UTC, not as local time, so
the reader's own timezone cannot move a record across that boundary
(`cowork.py:173-198`, pinned by `tests/test_cowork.py:83-86`).

The registry is append-only: "Correcting an entry means adding a superseding one
with a new basis, never editing what a past reader already relied on"
(`cowork.py:103-104`).

Three rules that used to sit under this heading are about messages, not about
historical identity, and have been moved to where they belong:
`ASSIGN`/`PROGRESS`/`REVIEW_REQUEST`/`HANDOFF` requiring a work ID is in §1.2;
`reply_to` on a reply is in §1.2 and Part 2, item D; the `expected_revision`
comparison and the `QUESTION` response it is supposed to produce are in §1.2 and
Part 2, item E.

## 1.5 Agent liveness marks

`agent_activity()` (`cowork.py:484-534`) is the one function that touches the
mailbox directory. Its only caller is `work_web.work_agents()`
(`work_web.py:289-317`), which calls it as:

```python
rows = agent_activity(admin.root / "cowork" / "mailbox", admin.AGENT_NAMES)
```
`work_web.py:309`

For each agent it takes the later mtime of two files in the mailbox root:

```python
ACTIVITY_MARKS = (".processed-{agent}", ".cursor-{agent}")
```
`cowork.py:463`

Both names are treated as equals; whichever has the later mtime wins
(`cowork.py:499-510`), and the winning filename is reported as `evidence`.

Three verdicts, kept apart on purpose (`cowork.py:490-492`):

| Verdict | Condition | Line |
|---|---|---|
| `활동 있음` | a mark exists and is at most `QUIET_AFTER_SECONDS` old | `cowork.py:528` |
| `활동 없음` | a mark exists and is older than that | `cowork.py:528` |
| `판정 불가` | no mark could be read | `cowork.py:511-523` |

`판정 불가` carries a `basis` distinguishing `"표시 파일을 읽을 수 없다"` (an
`OSError` while reading) from `"표시 파일이 없다"` (no such file) —
`cowork.py:518-522`. `QUIET_AFTER_SECONDS = 60 * 60` (`cowork.py:477`), with its
calibration recorded at `cowork.py:465-470`.

**What a mark cannot prove.** The code says it (`cowork.py:472-476`):

> `# What it cannot mean is "dead". A mark moves when an agent processes a`
> `# message, so an agent with no mail leaves no trace while being perfectly`
> `# alive. On 2026-09-04 a four-hour silence was reported as work having stopped,`
> `# and it turned out four agents had been up for thirty-three hours and simply`
> `# had nothing to record.`

Three further limits are not stated there and hold anyway:

1. A mark proves that **something changed a file's mtime**. `touch` and real
   work are indistinguishable to this reader.
2. `path.stat()` (`cowork.py:503`) **follows symlinks**. There is no `lstat`, no
   `follow_symlinks=False`, and no containment check. A mark symlinked to any
   frequently-touched file reports liveness forever. This is the code's answer
   to the no-symlink rule in Part 2, item G.
3. **No code in this repository writes either mark.** On this codebase alone,
   every agent reports `판정 불가`. Whatever writes the marks is outside this
   repository, and the screen depends on it.

The endpoint joins in `open_items`, counted over non-terminal statuses
(`work_web.py:301-311`), and publishes `quiet_after_seconds` so the screen does
not hard-code the threshold (`work_web.py:314`).

Tests: `tests/test_work_api.py:530` (a fresh `.processed-noa` reads as
`활동 있음`; an agent with no mark reads as `판정 불가` with basis
`표시 파일이 없다`) and `tests/test_work_api.py:549` (*"A mark moves when mail is
processed, so silence is not proof of death."*).

## 1.6 Change receipts and handoffs

Two files under `APP_CONFIG_ROOT/cowork/` are read by the work-item timeline.
Both are **read-only, best-effort, and have no writer in this repository**:
"Read-only and best-effort: a missing or unreadable cowork tree yields no
entries rather than failing the timeline" (`work_store.py:827-828`).

The reader is `WorkStore._cowork_entries()` (`work_store.py:824-892`), reached
from `read_timeline()` (`work_store.py:794`) and served at
`GET /api/v1/admin/work/items/{item_id}/timeline` (`work_web.py:271`).

**`cowork/events.jsonl`** — one JSON object per line. Lines that are not JSON
are skipped (`work_store.py:842-844`); lines whose `work_id` does not match the
item are ignored (`work_store.py:845`). Fields consumed
(`work_store.py:847-858`): `at`, `event` (surfaced as both `action` and
`phase`), `revision`, `summary`, `handoff_id`.

The record's own actor is **not read**:

```python
"actor": resolve_actor("moa", at=at),
```
`work_store.py:856`

Every event is attributed to `moa` unconditionally, and because `at` is normally
on or after `2026-09-02`, that renders as `resolution: "declared"` (§1.4). An
event written by anyone else is reported as `moa`, with a declared basis.

**`cowork/handoffs/*.json`** — one file per handoff. Candidates are selected by
filename (`name.endswith(".json") and item_id in name`, `work_store.py:864-866`)
and then re-verified against the payload's own `work_id` (`work_store.py:874`).
Fields consumed (`work_store.py:876-889`): `handoff_id`, `created_at` → `at`,
`actor` (here it *is* read, `:888`), `outcome` → `phase`, `next_action` →
`summary`, `item.revision_after` → `revision`. The receipt is recorded as the
relative identifier `cowork/handoffs/<name>` (`:887`) — "A local identifier,
never an absolute path" (`:886`). Receipts supplied through the work API are
refused if absolute or traversing (`work_store.py:271-287`).

Shape pinned by `tests/test_work_timeline.py:215`; rendered at
`src/rlwrld_worklog/static/admin.html:1177,1225`.

**`cowork/logs/`** is created and written by `scripts/wake-local.sh:29,32,116` —
one session transcript per wake. It is the only path under `cowork/` that this
repository writes.

## 1.7 The roster is disjoint from the directing parties

This is the largest gap between this document and the running system, and it
changes how every section above should be read.

| Set | Value | Defined at |
|---|---|---|
| Cowork parties | `("hk", "ari", "mori", "moa")` | `cowork.py:44` |
| Directing parties | `frozenset({"hk", "ari", "mori"})` | `cowork.py:57` |
| Agent session roster | `("noa", "boa", "doa", "roa", "soa")` | `admin_store.py:222` |
| Wake-script executor | `"local"` by default (`WAKE_EXECUTOR`) | `scripts/wake-local.sh:30` |

The three sets do not intersect. Four consequences, all of them live:

1. **The liveness screen watches a different set of names than this protocol
   describes.** `work_web.py:309` passes `admin.AGENT_NAMES`, so the marks read
   are `.processed-noa`, `.cursor-noa`, `.processed-boa`, and so on. A mark
   named `.processed-moa` is never read by anything.

2. **An authority branch is unreachable.** Both write guards read:

   ```python
   name = agent_name(current)
   if name is None or name in DIRECTING_PARTIES:
       return
   ```
   `work_web.py:100-101` and `work_web.py:128-129`

   `agent_name()` (`admin_web.py:49-54`) returns the subject of an agent
   session, and a session can only be minted for a name in `AGENT_NAMES`:
   `issue_agent_session()` calls `agent_subject()` (`admin_store.py:275`), which
   raises `unknown agent` for anything outside the roster
   (`admin_store.py:249-250`). So for any real agent session,
   `name in DIRECTING_PARTIES` is always false, and only the `name is None` arm
   — a human session — can take that early return. As the roster currently
   stands, the `DIRECTING_PARTIES` half of both conditions is dead code. The
   guards still work; they work entirely through their `assigned_to` and
   declared-name checks (`work_web.py:102-105`, `:130-136`).

3. **Items assigned to the wake executor are invisible on the agents screen.**
   `scripts/wake-local.sh:30` defaults `executor` to `local`, which is in
   neither set. `work_web.work_agents()` emits one row per `AGENT_NAMES` entry
   (`work_web.py:309-311`), so open items assigned to `local` are counted into
   `open_counts` and then never rendered.

4. **The validator's recipient check cannot pass for a roster agent.** The check
   is `item["assigned_to"] != recipient`, with `recipient` defaulting to `moa`
   (`cowork.py:333`, `:424`). A board item assigned to `noa` would be refused
   unless a caller passed `recipient="noa"` — and there is no caller.

---

# Part 2 — Specified, not implemented

Everything below is the agreed design. **None of it is behaviour.** Each item
states what the code does instead.

### A. Mailbox directories

**Specification:**

```text
APP_CONFIG_ROOT/cowork/mailbox/
  to-ari/
  to-mori/
  to-moa/
```

**What the code does instead:** nothing creates or reads these directories. The
strings `to-ari`, `to-mori` and `to-moa` do not occur in `src/` or `scripts/`.
The only thing that opens `APP_CONFIG_ROOT/cowork/mailbox` is
`agent_activity()`, which `stat()`s `.processed-<agent>` and `.cursor-<agent>`
directly in the mailbox root — flat, not per-recipient (`cowork.py:501-503`).

What the code actually touches under `APP_CONFIG_ROOT/cowork/`:

| Path | Code | Access |
|---|---|---|
| `mailbox/.processed-<agent>`, `mailbox/.cursor-<agent>` | `cowork.py:501-503` via `work_web.py:309` | `stat()` only |
| `events.jsonl` | `work_store.py:833-859` | read |
| `handoffs/*.json` | `work_store.py:861-891` | read |
| `logs/` | `scripts/wake-local.sh:29,32,116` | created, written |

### B. `latest.json`

**Specification:** a `latest.json` beside `events.jsonl` and `handoffs/`.

**What the code does instead:** nothing. `latest.json` has zero references in
`src/`, `scripts/`, `tests/`, or any other document. It is not read, not
written, and no code would notice it.

### C. One immutable file per message, and its filename

**Specification:** the sender creates one new Markdown file in the receiver's
directory; existing messages are never edited, renamed, moved, or deleted. The
filename is `<YYYYMMDDTHHMMSSZ>--<message-id>--<work-id-or-none>.md`, in UTC,
where `message-id` is `msg_` followed by at least 16 lowercase hexadecimal
characters. A writer creates a temporary regular file in the same directory,
sets mode `0600`, flushes and fsyncs it, then atomically renames it to the final
`.md` name. Readers ignore temporary files.

**What the code does instead:** nothing writes a message file, so there is no
atomic-rename path, no fsync, and no temporary file to ignore. Nothing parses a
filename, so the naming scheme is unenforced. The nearest guard is
`is_safe_segment()` (`cowork.py:437-439`), which checks one path segment against
`_SAFE_SEGMENT = ^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$` (`cowork.py:283`) and
rejects `..`. Its docstring says *"Used before any mailbox join"*; there is no
mailbox join. `is_safe_segment` has no caller in `src/` and is exercised only by
`tests/test_cowork.py:230,234`.

The `message-id` shape is separately enforced by the validator as a **warning,
not a refusal** — §1.2.

### D. Required front matter

**Specification:**

```yaml
---
protocol_version: 1
message_id: msg_0123456789abcdef
from: ari
to: moa
type: ASSIGN
work_id: wi_0123456789abcdef
expected_revision: 3
created_at: 2026-09-02T05:30:00Z
reply_to: null
requires_ack: true
subject: Short human-readable subject
---
```

A reply sets `reply_to` to the original `message_id`. `expected_revision` is the
revision the sender observed, for the receiver to compare with the backoffice
before acting.

**What the code does instead:** no front matter is ever parsed from a file.
`validate_directive()` takes an already-parsed mapping from its caller. Of the
twelve fields above it reads nine; `created_at`, `requires_ack` and `subject`
are read by no code at all (§1.2). Of the nine it reads, `message_id` format and
`reply_to` presence produce warnings rather than refusals (§1.2).

### E. A revision mismatch produces a `QUESTION` response

**Specification:** the receiver compares `expected_revision` with the backoffice
before acting; a mismatch produces a `QUESTION` response, and never a force
overwrite.

**What the code does instead:** the comparison exists and refuses
(`cowork.py:428-432`), so the "never a force overwrite" half holds. The
`QUESTION` half does not: `validate_directive()` appends a reason and returns
`DirectiveVerdict(accepted=False)`. It emits nothing, writes nothing, and
notifies no one. `QUESTION` exists only as a string in `MESSAGE_TYPES`
(`cowork.py:271`); no code path constructs a message of any type.

The same applies to the rest of the reading-and-acknowledgement procedure the
protocol specifies — the receiver scans only its own directory for safe regular
`.md` files, orders them by `created_at` and filename, validates the front
matter, does not mark a message by editing it, and acknowledges with a new `ACK`
file in the sender's directory whose `reply_to` is the original message ID;
before acting it reads the referenced work item, confirms `expected_revision`
and assignment, records planned scope, responds with `ACK`, and acts only once
the required trigger and permissions exist. **No code performs any step of
that.** The nearest thing that runs is `agent_activity()`, which reads two mark
files that this procedure never mentions (§1.5).

### F. Directory mode `0700`, file mode `0600`

**Specification:** directories under `APP_CONFIG_ROOT/cowork` use mode `0700`
and files use mode `0600`.

**What the code does instead:** the only writer under `cowork/` is
`scripts/wake-local.sh`, and it sets neither. `wake-local.sh:32` is
`mkdir -p incoming "$log_dir"` — no mode argument, and no `umask` call anywhere
in the script, so `cowork/logs/` is created at the ambient umask. The session
transcript is created by shell redirection, `> "$transcript" 2>&1`
(`wake-local.sh:121`), which is `0644` under the usual `umask 022`. Those
transcripts contain whatever the session printed.

For contrast, the code that does enforce this rule is elsewhere: `AdminStore`
creates its root `0700` and re-chmods it (`admin_store.py:91-92`), and
`scripts/prepare-admin-config.sh:7-8` uses `umask 077` with `install -d -m 0700`.

### G. No mailbox reader may follow a symlink

**Specification:** no mailbox reader may follow a symlink or read outside
`APP_CONFIG_ROOT/cowork`.

**What the code does instead:** the only mailbox reader follows symlinks.
`cowork.py:503` is `datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)`,
and `Path.stat()` resolves symlinks by default. There is no `lstat()`, no
`follow_symlinks=False`, and no check that the resolved path is still inside
`APP_CONFIG_ROOT/cowork`. A `.processed-noa` symlinked at any frequently-written
file reports that agent as active indefinitely.

The containment half of the rule is enforced in one unrelated place:
`_normalize_receipt()` refuses absolute and traversing receipt paths supplied
through the work API (`work_store.py:271-287`), tested at
`tests/test_work_timeline.py:105-107`.

### H. Message body and content rules

**Specification:** keep the body concise and link to local evidence rather than
copying large output; use the relevant sections:

```markdown
## Request or result

## Allowed scope

## Prohibited actions

## Acceptance criteria

## Changed paths and receipts

## Tests, build, deploy, and UI verification

## Blocker, decision needed, and next action

## Local evidence
```

Do not include tokens, credentials, environment-variable values, private raw
content, full diffs, or long logs. Paths must be within the approved workspace,
`APP_CONFIG_ROOT`, or the explicitly approved archive root. Archive evidence is
read-only unless the work item explicitly authorizes a write.

**What the code does instead:** nothing reads a message body, so no section is
required, absent, or checked, and no content rule is enforced by code. The
comparable rule that is enforced lives in the work store: `detail` and
`progress_summary` never reach the history stream, "so a summary cannot become a
side channel for item content" (`work_store.py:265-268`), with
`MAX_CONTEXT_SUMMARY = 500` at `work_store.py:268`.

### I. Handoff to Ari

**Specification:** every completion, pause, or failure creates a `HANDOFF`
message to Ari and a corresponding append-only handoff receipt, carrying the
final work-item revision, changed paths and before/after hashes, tests, build
and deployment results, running process and session identifiers, blockers, next
action, and local evidence paths, and never private source content. When Ari
returns, Ari compares the work item, handoff receipt, actual Git state, current
file hashes, and test and deployment evidence before accepting the handoff.

**What the code does instead:** no code writes a `HANDOFF` message or a handoff
receipt. `cowork/handoffs/*.json` is read only (§1.6), and the reader consumes
six fields — `handoff_id`, `created_at`, `actor`, `outcome`, `next_action`, and
`item.revision_after` (`work_store.py:876-889`). Hashes, changed paths, test
results and session identifiers are not read even when present in the file.

### J. Silence is not a trigger

**Specification:** Moa starts work only after an explicit HK request, a `ready`
item assigned to `moa`, or an approved PC-scheduler event naming the work ID.
Being visible is not authority: a work item sitting in `ready`, or a message
merely present in the inbox, authorizes nothing. Only an `ASSIGN` from `hk`,
`ari` or `mori` that names the work item, matches its current `assigned_to`, and
carries the observed `expected_revision` may be acted on. When a conflict cannot
be resolved from the messages alone, Moa does not act: it moves the work item to
`waiting` and asks.

**What the code does instead:** `validate_directive()` implements the
`ASSIGN`-only rule exactly as specified, and has no caller (§1.2). The
unattended session that actually runs on the production host takes its trigger
from `status == "ready"` alone. That is not a gap in implementation but a direct
contradiction, and it is recorded in Part 3.

### K. Initial handshake

**Specification:** Moa must first read this protocol without changing product
code or operational data. If feasible, Moa creates only the mailbox directories
and one `ACK` message to Ari with `work_id: wi_6168b397db24d070`, reporting
feasibility, conflicts, the observed work-item revision, and any permission
still required.

**What the code does instead:** nothing. The handshake is referenced once in
code, as the evidence for when the naming took effect — `NAMING_BASIS` at
`cowork.py:167-170`: *"observed: the ari/mori/moa naming appears in the mailbox
handshake of 2026-09-02 (msg_4bb85f383554c17c0e and its ACK)."* That message is
not in this repository.

---

# Part 3 — Open conflict: `ready` and `push`

Two subsystems in this repository disagree about what may happen without a human
present. This is recorded, not resolved. Nothing has been changed to make one
side agree with the other, and neither side should be edited without a decision
from `hk` or `ari`.

## The two positions

**This protocol says a `ready` item authorizes nothing.**

> Being visible is not authority. A work item sitting in `ready`, or a message
> merely present in the inbox, authorizes nothing.

Enforced in code by `validate_directive()`, which requires a well-formed
`ASSIGN` from a directing party carrying a matching `expected_revision`
(`cowork.py:398-402`, `:404-405`, `:426-432`).

**This protocol says `push` is never autonomous.**

```python
NEVER_AUTONOMOUS = ("sudo", "delete", "policy_change", "security_control_change", "push")
```
`cowork.py:269`, with the reason at `cowork.py:267-268`: *"Never granted by a
message. These need a human decision every time, so there is deliberately no
flag that turns them on."* Pinned by `tests/test_cowork.py:118-119` and
`tests/test_cowork.py:244-245`.

**`scripts/wake-local.sh` selects on `status == "ready"` alone.**

```python
ready = [
    it for it in items
    if isinstance(it, dict)
    and it.get("status") == "ready"
    and (it.get("assigned_to") or "") == want
    and not it.get("archived_at")
]
```
`scripts/wake-local.sh:81-87`

There is no message, no sender, no `expected_revision`, and no call to
`validate_directive()` anywhere in that script or in anything it invokes. The
oldest such item is picked by `created_at` (`wake-local.sh:90-91`) and a headless
session is started on it (`wake-local.sh:118-121`).

**That session is granted more than the baseline.**

```
allowed='Read,Glob,Grep,Edit,Write,Bash(git:*),Bash(python3:*),Bash(pytest:*),Bash(docker:*),Bash(docker compose:*),Bash(work:*),Bash(systemctl:*),...'
```
`scripts/wake-local.sh:102`, passed with `--permission-mode acceptEdits`
(`wake-local.sh:119`). `AUTONOMOUS_BASELINE` is `("read", "investigate",
"report", "test")` (`cowork.py:255`); `Edit`, `Write`, `Bash(docker:*)` and
`Bash(systemctl:*)` are all beyond it, and no `ASSIGN` granted them, because no
`ASSIGN` was read.

**And the prompt instructs that session to push.**

`scripts/local-work-prompt.md:57-64` — *"**커밋과 푸시.** 코드를 고쳤으면
커밋한다. … 푸시 전에 반드시 `.venv/bin/python -m pytest -q`. 초록이 아니면
푸시하지 않는다."* `Bash(git:*)` in the allowlist includes `git push`, so the
capability is real and not merely requested.

The carrier does the same on its own timer, with no session and no human:
`scripts/incoming-tick.sh:103` is `if git push -q origin main; then`, run by
`hkwa-incoming.service` roughly every three minutes
(`deploy/systemd/hkwa-incoming.timer:6`).

## What this means as it stands

An item moved to `ready` and assigned to the wake executor will, within about
three minutes, cause an unattended session to start with write, docker, systemd
and `git push` capability, on the strength of the item's status alone. This
protocol says that status authorizes nothing, and that `push` requires a human
decision every time.

Both are current. `cowork.py`'s rules are not wired to anything that runs, and
the thing that runs does not consult them. Until this is decided, read
`cowork.py`'s guarantees as describing a validator rather than the host: they
constrain callers that do not exist, and they do not constrain
`hkwa-wake.service`.

Operational detail of the wake and carrier paths, including every state each can
report, is in `docs/dev-prod-split.md`. The scripts themselves are described one
by one in `docs/scripts.md`.
