# Daily incremental collection

One command captures a day of Slack, Google Calendar and Notion over their
official read-only APIs, projects the capture into the standard v1 ledger, and
optionally loads it into the service database.

```
worklog daily-collect
```

## What one run does

Per source, in three separately reported stages:

| Stage | Input | Output | On failure |
|---|---|---|---|
| `capture` | official read-only API | immutable raw archive + run manifest | the source is `failed`; other sources still run |
| `ledger` | that run manifest | validated standard v1 JSONL | the source is `degraded`; the raw capture stays usable |
| `load` | that JSONL | PostgreSQL | the source is `degraded`; re-runnable from the ledger |

The raw archive is the system of record. A capture that succeeded stays
successful and re-projectable even when everything after it fails, because
both later stages read only from files that are already on disk.

Sources always run in the order **slack → google-calendar → notion**,
regardless of the order `--source` is given: Slack and Calendar discover Notion
URLs, and Notion drains that queue in the same run.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | every requested source completed every stage it reached |
| 1 | at least one **capture** failed |
| 2 | every capture succeeded, but a ledger or database stage failed |
| 3 | another run holds the lock; nothing ran |

## Credentials

Credentials come from the admin-managed config root, which is
`--config-root`, else `APP_CONFIG_ROOT`, else `~/.config/hk-work-assistant`:

```
<config root>/credentials/slack-token
<config root>/credentials/notion-token
<config root>/credentials/google-token.json
<config root>/settings.json          # slack_expected_team_id, ...
```

A file wins over the matching environment variable (`SLACK_USER_TOKEN`,
`NOTION_TOKEN`, `GOOGLE_TOKEN_PATH`): the backoffice is where a token is
rotated, and a stale shell export must not override it. A missing credential
fails that one source and is reported in `credentials_available`; the other
sources still run. No token, OAuth client, database URL or callback code is
ever printed or written to a manifest.

## Paths

| What | Flag | Default |
|---|---|---|
| raw archive | `--archive-root` | `RAW_ARCHIVE_ROOT`, else `/data/rlwrld-worklog` |
| ledger JSONL | `--ledger-root` | `LEDGER_ROOT`, else `<archive root>/staging/ledger` |
| lock | `--lock-path` | `<archive root>/locks/daily-collect-<environment>.lock` |
| database | `--database-url` | `DATABASE_URL` |

Layout under the archive root:

```
raw/<source>/<env>/YYYY/MM/DD/<run id>/NNNNNN-<kind>-<hash>.json.gz
manifests/<source>/<env>/<run id>.json          one manifest per run
manifests/<source>/<env>/checkpoint.json        current position
manifests/<source>/<env>/checkpoints/<run>.json every previous position
manifests/notion/<env>/link-queue.json          Notion URLs still to resolve
```

Nothing in `raw/` is ever rewritten. A run writes into its own directory, each
page filename carries a content hash, and a write to a path that already
exists is refused rather than replacing what is there.

## Dry runs and smoke tests

```bash
worklog daily-collect --smoke     # strictly bounded API work; implies --dry-run
worklog daily-collect --dry-run   # full window, but nothing is committed
```

Both leave **every** piece of persistent production state exactly where it
was, and run the database stage as a transaction that rolls back. That means:

* no checkpoint advances;
* nothing is written to `link-queue.json` — it is read so the run can report
  what is pending, but no URL is created, updated or marked `fetched`,
  `failed` or `unresolved`, and no file is created if none existed. A dry run
  therefore does **not** remember this run's Slack/Calendar discoveries; the
  next real run re-reads every still-pending URL and re-discovers them. This
  is declared in the Notion manifest as
  `notion.link_queue_not_persisted_in_dry_run`, and the capture summary
  reports `notion_links_queued` (what it would have written) alongside
  `notion_links_persisted: false`.

`--smoke` additionally bounds the API work: at most 2 Slack channels and 25
messages with the workspace-wide searches skipped, 5 Notion objects with no
re-check sweep and a 20-request comment budget, and 2 calendars.

Raw and ledger files are still written by a dry run. Both are immutable,
deterministic, genuinely observed data, and having them on disk is what makes
a smoke test inspectable.

## Scheduling

The command takes a non-blocking exclusive lock, so a slow run can never be
overlapped by the next one; the second run exits 3 without touching anything.

```
# crontab, 03:10 Asia/Seoul
10 3 * * * cd /srv/worklog && /usr/local/bin/worklog daily-collect >> /var/log/worklog/daily.log 2>&1
```

```ini
# /etc/systemd/system/worklog-daily.service
[Service]
Type=oneshot
Environment=RAW_ARCHIVE_ROOT=/data/rlwrld-worklog
Environment=APP_CONFIG_ROOT=/var/lib/hk-work-assistant/config
EnvironmentFile=/etc/worklog/database.env
ExecStart=/usr/local/bin/worklog daily-collect
```

Output is line-oriented JSON, one `daily_collect_source=` line as each source
finishes plus a final `daily_collect=` summary, so a log can be parsed without
re-reading manifests.

## What a manifest tells you

Every run writes one, whether it succeeded or failed:

```
source, run_id, capture_profile, capture_density, dry_run
started_at, finished_at, requested_window, checkpoint_in, checkpoint_out
api_coverage           per endpoint: pages and items fetched
pages_archived, files  every archived file with its sha256 and its request
rate_limit_hits, truncated, truncation
skips, errors          what was not collected, and why
coverage_notes         the API limits that apply to this run
status                 success | success_with_skips | degraded | failed
```

`degraded` is stronger than `success_with_skips`: it means at least one object
failed **unresolved** -- the API never gave a final answer for it, because the
client used up its transient retries. A `success_with_skips` run knows what it
missed and why; a `degraded` run does not.

A checkpoint only advances after a complete, untruncated run, and the previous
position is kept under `checkpoints/`.

## Per-source coverage, and its limits

These are declared in every run manifest under `coverage_notes` and are not
worked around silently.

**Slack.** Every visible conversation (public, private, MPIM, DM) including
archived ones; users, usergroups, messages with their edits and reactions,
threads, and channel metadata. Each channel resumes from its own high
watermark.

* The Web API has **no deleted-message feed**. `conversations.history` simply
  stops returning a deleted message. Tombstones are preserved only where Slack
  exposes them (`subtype: tombstone`); otherwise a deleted message keeps its
  last observation. Closing this needs the Events API, which is not a
  read-only pull.
* `conversations.history` omits thread replies, so a reply to an older thread
  cannot be found through history. Two independent paths cover it: a re-poll
  of threads carried in the checkpoint (30-day lookback by default) and the
  `search.messages` queries.
* Mentions are covered by six searches plus one per usergroup the user belongs
  to. Matches the context filter drops, and matches older than the window, are
  counted in the manifest (`search_matches_context_filtered`,
  `search_matches_before_window`) so a dropped mention is never silent. Every
  search response is archived in full before any filtering.
* `search.messages` is an index and can lag, so a same-minute mention may
  first appear on the next run. The 26-hour default window overlaps for this.
* File attachments are reduced to id, name, title, mimetype, filetype, size
  and links. No file body is ever fetched.

**Notion.** `/search` with no object filter (pages, databases, data sources),
full page and data-source properties, every descendant block recursively,
comments per block, users, and the archived/in-trash flags.

* `/search` is **not a change feed**: it omits archived and trashed objects and
  anything not shared with the integration, and it can lag an edit. Deletion,
  trashing and a lost share become observable only through the bounded
  re-check of previously seen objects (100 per run by default), which reports
  a 404 as a skip rather than as a clean day.
* Comments live on the block they were left on, so `/comments` is queried per
  block. A full-density run is **exhaustive and uncapped**: the bound is the
  API client's rate-limit handling. A finite default would make any large
  workspace mark itself truncated, withhold its checkpoint, and then redo the
  identical window on every run. An operator can still pass a finite budget;
  when one is set it is reported in `comment_request_budget` /
  `comment_requests_made`, adds the `notion.comment_requests_capped` coverage
  note, and reaching it marks the run truncated and withholds the checkpoint.
  Smoke runs use a small explicit budget for exactly that reason.
* The `last_edited_time` watermark advances only across the contiguous prefix
  of successfully fetched objects: it never steps over a failed fetch. A
  stalled checkpoint widens the next window rather than leaving a hole.
* **One object is the unit of completeness.** A failure in an object's
  retrieval, block walk, comment sweep, properties or normalization fails that
  object alone: it emits no event, is not marked fetched in the link queue, is
  not remembered as successfully checked, and every other candidate still runs.
  The raw pages already written for it are kept immutably -- a partial capture
  is evidence, not garbage. `counters.objects_failed_by_phase` says where each
  one died, and `coverage_complete` is the single field that says whether the
  run saw everything it set out to.
* The API client retries what has no answer yet -- read timeouts, dropped
  connections, DNS and protocol faults, HTTP 408/425/429 and 5xx -- honouring
  `Retry-After` (seconds or HTTP-date, clamped) and otherwise backing off
  exponentially within a bound. A definitive 4xx is never retried. The counts
  land in `counters.transient_retries`, `transient_retries_by_class` and
  `requests_exhausted`. No error message ever carries the token or a query
  string.
* If an object fails unresolved and carries no `last_edited_time` -- a
  link-queue or re-check candidate -- there is no position to stop the
  watermark below, so the watermark does not move at all
  (`counters.watermark_held`). A *permanent* answer (400/401/403/404) does not
  hold it: a deleted page 404s on every future run, and holding for it would
  freeze the watermark forever.
* A `/search` walk that dies part-way keeps the objects it did list, marks the
  run truncated (`search_incomplete`) and withholds the checkpoint, because
  objects inside the window may never have been listed at all.
* Data-source rows are pages and arrive through `/search`; the collector does
  not re-query every data source daily.

**Google Calendar.** Every calendar in `calendarList` with `showDeleted` and
`showHidden`, re-listed in full each run, plus per-calendar `nextSyncToken`
incrementals with `showDeleted=true` and `singleEvents=false` — so recurrence
masters, `recurringEventId`, `originalStartTime`, attendees and their response
states, organizers, creators, conference data, reminders, attachment metadata,
`updated`, `etag` and `status` all arrive verbatim.

* A first sync, and a sync recovering from an expired token (HTTP 410), reads
  from `timeMin` forward. Events older than that window keep their previous raw
  observation and are not re-observed. Nothing is deleted by a resync.
* Each calendar's token advances independently and only on that calendar's own
  success, so one inaccessible calendar cannot disturb another. A token Google
  has already rejected is dropped rather than replayed.
* Deletions arrive as `status: cancelled`; a calendar removed from
  `calendarList` is preserved with `deleted: true`.
* Attachments are the `fileId`/`fileUrl`/`title` metadata Google returns. No
  file body is fetched.

## Ledger output

`convert_live_run` turns one run manifest into
`<ledger root>/ledger/<source>/live-<run id>.jsonl`, validated against the
standard v1 JSON Schema.

Before any archived page is decompressed or parsed, its SHA-256 is recomputed
over the file bytes and compared with the manifest entry: the manifest is an
index, and only the bytes on disk are the authority. A missing, malformed or
mismatched hash aborts the whole conversion before anything is written, so a
corrupted or edited raw page can neither become a ledger record nor replace a
ledger file that was built from good bytes.

Two kinds of record come out:

* **activity** — `message`, `page`, `block`, `comment`, `event`; projected onto
  `timeline_events` by the loader;
* **dimension** — `user`, `usergroup`, `conversation`, `calendar`,
  `data_source`; the containers and actors those activities point at. They
  exist so the service database is rebuildable from ledger data alone, and are
  deliberately **not** projected onto the timeline.

`sql/migrations/0003_live_capture.sql` widens the `ledger_records.entity_type`
CHECK for the dimension types; every type the earlier loader could write is
still accepted.

Live records carry a `live-` capture profile and therefore load at
`origin_priority` 100, above every legacy profile (20, or 10 for the Slack
thread-store supplement). A legacy re-run can never demote a live head.

Conversion is deterministic: the same manifest produces byte-identical JSONL,
and loading is idempotent through `ledger_id`.

The load stage offers the whole ledger root for that source, not only the file
this run produced. Files already loaded are skipped by sha256, so a file whose
load failed yesterday is picked up today rather than being stranded.

## Re-running a stage by hand

```bash
export PYTHONPATH="$PWD/.python-packages:$PWD/src"
ARCHIVE=/data/rlwrld-worklog
LEDGER=$ARCHIVE/staging/ledger

# re-project one archived run; no API call
python3 -m rlwrld_worklog ledger-live-convert slack \
    --archive-root $ARCHIVE \
    --manifest $ARCHIVE/manifests/slack/production/<run id>.json \
    --out-root $LEDGER

# load it (dry-run by default; the real transaction, then rollback)
python3 -m rlwrld_worklog ledger-load slack --ledger-root $LEDGER
python3 -m rlwrld_worklog ledger-load slack --ledger-root $LEDGER --apply
```
