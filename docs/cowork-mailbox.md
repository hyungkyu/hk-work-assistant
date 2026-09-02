# Ari–Moa Markdown mailbox protocol (v1)

This protocol lets Ari and Moa exchange long instructions and handoffs without
copying them through a chat window. The backoffice work item remains the source
of truth for status, ownership, and optimistic revision. Mailbox files carry
messages and evidence; they do not replace the work item.

## Identities

- `hk`: decision maker and requester
- `ari`: primary operator for requirements, rules, review criteria, and handoff verification
- `moa`: Co-work operator that continues registered work when explicitly triggered

## Paths

The mailbox root is:

```text
APP_CONFIG_ROOT/cowork/mailbox/
  to-ari/
  to-moa/
```

Change receipts and detailed task handoffs remain separate:

```text
APP_CONFIG_ROOT/cowork/
  events.jsonl
  handoffs/
  latest.json
```

Directories use mode `0700`; files use mode `0600`. No mailbox reader may
follow a symlink or read outside `APP_CONFIG_ROOT/cowork`.

## One immutable file per message

The sender creates one new Markdown file in the receiver's directory. Existing
messages are never edited, renamed, moved, or deleted.

Filename:

```text
<YYYYMMDDTHHMMSSZ>--<message-id>--<work-id-or-none>.md
```

Use UTC. `message-id` is `msg_` followed by at least 16 lowercase hexadecimal
characters. A writer creates a temporary regular file in the same directory,
sets mode `0600`, flushes and fsyncs it, then atomically renames it to the final
`.md` name. Readers ignore temporary files.

## Required front matter

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

Allowed message types are `ASSIGN`, `ACK`, `PROGRESS`, `QUESTION`,
`REVIEW_REQUEST`, and `HANDOFF`.

- `ASSIGN`, `PROGRESS`, `REVIEW_REQUEST`, and `HANDOFF` require a work ID.
- A reply sets `reply_to` to the original `message_id`.
- `expected_revision` is the revision the sender observed. The receiver must
  compare it with the backoffice before acting.
- A mismatch produces a `QUESTION` response; it never causes a force overwrite.

## Body

Keep the body concise and link to local evidence rather than copying large
output. Use the relevant sections:

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

## Reading and acknowledgement

The receiver scans only its own directory for safe regular `.md` files, orders
them by `created_at` and filename, and validates the front matter. It does not
mark a message by editing it. Acknowledgement is a new `ACK` file in the other
party's directory with `reply_to` set to the original message ID.

Before action, the receiver must:

1. read the referenced backoffice work item;
2. confirm `expected_revision` and assignment;
3. record planned scope in the work item or change-receipt event;
4. respond with `ACK`;
5. act only after the trigger and permissions required by the work item exist.

Silence is not a trigger. Moa starts work only after an explicit HK request, a
`ready` item assigned to `moa`, or an approved PC-scheduler event naming the
work ID.

## Handoff to Ari

Every completion, pause, or failure creates a `HANDOFF` message to Ari and a
corresponding append-only handoff receipt. It includes the final work-item
revision, changed paths and before/after hashes, tests, build and deployment
results, running process/session identifiers, blockers, next action, and local
evidence paths. It never includes private source content.

When Ari returns, Ari compares the work item, handoff receipt, actual Git state,
current file hashes, and test/deployment evidence before accepting the handoff.

## Initial handshake

Moa must first read this protocol without changing product code or operational
data. If feasible, Moa creates only the mailbox directories and one `ACK`
message to Ari with `work_id: wi_6168b397db24d070`. The ACK reports feasibility,
conflicts, the observed work-item revision, and any permission still required.
