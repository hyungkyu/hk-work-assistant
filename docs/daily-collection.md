# Incremental collection

Five sources are collected over their official read-only APIs into an immutable
raw archive, projected into the standard v1 ledger, and loaded into the service
database.

They are **not** all driven by one command.

| Source | Captured by | Converts and loads by itself |
|---|---|---|
| Slack | `worklog daily-collect` | yes |
| Google Calendar | `worklog daily-collect` | yes |
| Notion | `worklog daily-collect` | yes |
| GitHub | `worklog github-collect` | **no** |
| Slurm | `worklog slurm-collect` | **no** |

`daily-collect` runs three sources and nothing else: `SOURCE_ORDER` in
`src/rlwrld_worklog/daily.py:35` is `("slack", "google-calendar", "notion")`,
and `--source` accepts only those three (`src/rlwrld_worklog/cli.py:214`).

`github-collect` and `slurm-collect` capture into the same archive under the
same manifest and checkpoint conventions, but they stop after the capture
stage. Their ledger projection and database load must be run by hand
afterwards — see [After a GitHub or Slurm capture](#after-a-github-or-slurm-capture).

The ledger format both paths write is documented in [ledger.md](ledger.md).
The rule version stamped into every manifest is documented in
[collection-rules.md](collection-rules.md). The dashboard that reads these
manifests is documented in [collection-status.md](collection-status.md).

## What one `daily-collect` run does

Per source, in three separately reported stages:

| Stage | Input | Output | On failure |
|---|---|---|---|
| `capture` | official read-only API | immutable raw archive + run manifest | the source is `failed`; other sources still run |
| `ledger` | that run manifest | validated standard v1 JSONL | the source is `degraded`; the raw capture stays on disk |
| `load` | that JSONL | PostgreSQL | the source is `degraded`; re-runnable from the ledger |

A capture that succeeded stays successful and re-projectable when the ledger
projection or the database load afterwards fails, because both later stages
read only from files that are already on disk.

One exception: a capture whose manifest status is `degraded` is **not**
re-projectable. `_load_manifest` accepts only `success` and
`success_with_skips` (`src/rlwrld_worklog/ledger/live.py:81-85`), so a degraded
run's raw pages are preserved but produce no ledger record, from
`daily-collect` or from `ledger-live-convert`. Only the Notion collector emits
`degraded` (`src/rlwrld_worklog/notion_collector.py:748`).

A source can also be reported `degraded` while every stage says `ok`: a Notion
capture that finished with unresolved objects sets a degraded reason, and the
source status follows it (`src/rlwrld_worklog/daily.py:552`) so a run summary
can never look cleaner than the manifest behind it.

Sources always run in the order **slack → google-calendar → notion**,
regardless of the order `--source` is given (`daily.py:570`): Slack and
Calendar discover Notion URLs, and Notion drains that queue in the same run.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | every requested source completed every stage it reached |
| 1 | at least one source is `failed` |
| 2 | no source failed, but at least one is `degraded` |
| 3 | another run holds the lock; nothing ran |

`github-collect` and `slurm-collect` return 3 on a lock conflict and 0 on
success; a capture failure propagates the exception after writing a
`status: failed` manifest (`cli.py:444`, `cli.py:521`).

## Credentials

Credentials come from the admin-managed config root, which is
`--config-root`, else `APP_CONFIG_ROOT`, else `~/.config/hk-work-assistant`:

```
<config root>/credentials/slack-token
<config root>/credentials/notion-token
<config root>/credentials/google-token.json
<config root>/credentials/github-token
<config root>/settings.json          # slack_expected_team_id, ...
```

A file wins over the matching environment variable (`SLACK_USER_TOKEN`,
`NOTION_TOKEN`, `GOOGLE_TOKEN_PATH`, `GITHUB_TOKEN`): the backoffice is where a
token is rotated, and a stale shell export must not override it. The GitHub
token is read by `read_github_token`
(`src/rlwrld_worklog/github_client.py:61-70`) and passed to `gh` through the
child process environment.

The Google token has one extra fallback the others do not
(`daily.py:117-121`): if `<config root>/credentials/google-token.json` is not a
file, `GOOGLE_TOKEN_PATH` is tried, and then the literal relative path
`secrets/google-token.json`.

Slurm needs no credential. `GET /api/download/jobs-raw-<cloud>` is an
unauthenticated request on the tailnet; the presigned S3 URL it redirects to is
treated as a credential and is never logged, returned or written to a manifest
(`src/rlwrld_worklog/slurm_client.py:9-16`).

A missing credential fails that one source and is reported in
`credentials_available`; the other sources still run. `credentials_available`
covers the three `daily-collect` sources only (`daily.py:88-93`). No token,
OAuth client, database URL or callback code is ever printed or written to a
manifest.

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
manifests/<source>/<env>/<run id>.<n>.json      a second finish() in one run
manifests/<source>/<env>/checkpoint.json        current position
manifests/<source>/<env>/checkpoints/<run>.json every previous position
manifests/notion/<env>/link-queue.json          Notion URLs still to resolve
locks/daily-collect-<env>.lock                  Slack + Calendar + Notion
locks/github-collect-<env>.lock                 GitHub only
locks/slurm-collect-<env>.lock                  Slurm only
staging/slurm/<run id>-<cloud>.psv.gz           transient; unlinked after use
staging/ledger/ledger/<source>/live-<run id>.jsonl
```

`<source>` here is the collector directory name, so Calendar is
`google-calendar`. The ledger uses `google_calendar` instead
(`src/rlwrld_worklog/collection_rules.py:66-75`).

The ledger root is `<archive root>/staging/ledger` by default, and
`convert_live_run` writes under a further `ledger/<source>/` inside it
(`src/rlwrld_worklog/ledger/live.py:1399`) — hence the doubled path segment
above.

One directory sits outside the archive. A run in flight publishes a derived
progress snapshot to

```
$APP_CONFIG_ROOT/collection-status/progress/<source>/<env>/<run id>.json
```

so a crashed capture that never wrote a manifest is still visible. It is
best-effort, never fatal, and written only when a config root is configured
(`src/rlwrld_worklog/collection_progress.py:71-83`, `:132`). It is described in
[collection-status.md](collection-status.md).

Nothing in `raw/` is ever rewritten. A run writes into its own directory, each
page filename carries a content hash, and a write to a path that already
exists is refused rather than replacing what is there
(`src/rlwrld_worklog/archive.py:166`).

## Dry runs and smoke tests

```bash
worklog daily-collect --smoke     # strictly bounded API work; implies --dry-run
worklog daily-collect --dry-run   # full window, but nothing is committed
```

Both leave **every** piece of persistent production state exactly where it
was, and run the database stage as a transaction that rolls back. That means:

* no checkpoint advances — `write_checkpoint` raises during a dry run
  (`archive.py:285`);
* nothing is written to `link-queue.json` — it is read so the run can report
  what is pending, but no URL is created, updated or marked `fetched`,
  `failed` or `unresolved`, and no file is created if none existed. A dry run
  therefore does **not** remember this run's Slack/Calendar discoveries; the
  next real run re-reads every still-pending URL and re-discovers them. This
  is declared in the Notion manifest as
  `notion.link_queue_not_persisted_in_dry_run`, and the capture summary
  reports `notion_links_queued` (what it would have written) alongside
  `notion_links_persisted: false`.

`--smoke` additionally bounds the API work (`daily.py:42-50`): at most 2 Slack
channels and 25 messages with the workspace-wide searches skipped, 5 Notion
objects with no re-check sweep and a 20-request comment budget, and 2
calendars. A full run uses a Notion re-check limit of 100 and no comment budget
at all (`daily.py:334-339`).

Raw and ledger files are still written by a dry run. Both are immutable,
deterministic, genuinely observed data, and having them on disk is what makes
a smoke test inspectable.

Each of those smoke bounds calls `note_truncation`, so a smoke run's manifest
is `truncated` and the coverage dashboard reads its date as `partial`. A plain
`--dry-run` sets no truncation, and the dashboard currently reads its date as
`collected` — see the known defect in
[collection-status.md](collection-status.md#known-defect-a-plain---dry-run-day-reads-as-collected).

`github-collect` and `slurm-collect` take `--dry-run` with the same meaning: no
checkpoint moves (`github_collector.py:509`, `slurm_collector.py:319`). Neither
has a `--smoke` mode.

## Scheduling

Every collection command takes a non-blocking exclusive lock, so a slow run can
never be overlapped by the next one; the second run exits 3 without touching
anything (`daily.py:632-645`).

The three locks are deliberately separate files. A GitHub backfill taking the
daily lock would make the nightly `daily-collect` exit 3 and be read the next
morning as a failed collection (`cli.py:371-376`).

The batch catalogue in `src/rlwrld_worklog/schedules.py:86-127` declares one
batch, `daily-collect`, and declares its runner to be a systemd timer driving a
oneshot service — not a cron entry and not a manual run:

```
timer unit:    worklog-daily.timer
service unit:  worklog-daily.service
cadence:       once a day, at the backoffice-configured hour and timezone
concurrency:   non-blocking flock on <RAW_ARCHIVE_ROOT>/locks/daily-collect-<environment>.lock
logs:          journalctl -u worklog-daily.service
```

systemd, not the backoffice setting, decides when the timer actually fires.
`schedules.py` reports the configured hour and the systemd answer side by side
and never merges them (`schedules.py:274-283`).

Two gaps a contributor should know about:

* **The unit files are not in this repository.** `worklog-daily.timer` and
  `worklog-daily.service` are named in the catalogue (`schedules.py:53-54`) but
  do not exist under `deploy/systemd/`, which currently holds only the
  `hkwa-wake` and `hkwa-incoming` units. They have to be installed on the host
  by hand.
* **GitHub and Slurm have no catalogue entry.** `CATALOGUE` contains exactly
  one `Batch` (`schedules.py:86`), so however `github-collect` and
  `slurm-collect` are scheduled on a host, the schedule page cannot see them.

Output is line-oriented JSON: one `daily_collect_config=` line before the run,
one `daily_collect_source=` line as each source finishes, and a final
`daily_collect=` summary (`cli.py:823-828`), so a log can be parsed without
re-reading manifests. The config line never contains the database URL
(`daily.py:660`).

## What a manifest tells you

Every run writes one, whether it succeeded or failed. The full field set is
written by `RawArchive.finish` (`archive.py:222-251`) and is identical for all
five sources:

```
schema_version          2
source, environment, run_id
capture_profile, capture_density, dry_run
started_at, finished_at
requested_window, checkpoint_in, checkpoint_out, checkpoint_advanced
api_coverage            per endpoint: pages and items fetched
coverage_notes          the API limits that apply to this run
pages_archived
rate_limit_hits, truncated, truncation
skips, errors           what was not collected, and why
files                   every archived file with its sha256 and its request
status                  success | success_with_skips | degraded | failed
<per-source details>    see below
collection_rule_version
collection_rule_digest
collection_rule_schema_version
```

The last three are the **rule stamp**. It is the archive's own record of how
the run was supposed to collect, applied after the caller's details so a
collector cannot overwrite or omit it, and it is present on success, dry-run
and failure manifests alike (`archive.py:14-19`, `:248-250`). See
[collection-rules.md](collection-rules.md).

Each entry in `files` carries `path`, `sha256`, `compressed_bytes`, `kind`,
`endpoint`, `request` (with credential-shaped keys redacted,
`archive.py:44-51`), `item_count` and `written_at`.

The per-source details each collector adds to its own manifest:

| Source | Adds |
|---|---|
| Slack | `team_id`, `self_user_id`, `since`, `channels_seen`, `channels_attempted`, `channels_collected`, `events`, `skipped_channels`, `high_watermarks`, `counters` |
| Google Calendar | `since`, `calendars_seen`, `calendars_attempted`, `calendars_collected`, `skipped_calendars`, `events_archived`, `cancelled_events`, `notion_urls`, `counters` |
| Notion | `counters`, including `coverage_complete`, `objects_failed_by_phase`, `objects_failed_unresolved`, `watermark_held`, `comment_request_budget`, `comment_requests_made` |
| GitHub | `organization`, `mode`, `repositories_listed`, `repositories_collected`, `skipped_repositories`, `mirrors_behind_remote`, `mirrors_with_unknown_coverage`, `repositories_mirror_only`, `repositories_api_only`, `commits`, `commit_rows_archived`, `rest_counts`, `counters` |
| Slurm | `mode`, `clouds_attempted`, `clouds_collected`, `jobs`, `parent_rows_archived`, `step_rows`, `counters` |

`degraded` is stronger than `success_with_skips`: it means at least one object
failed **unresolved** — the API never gave a final answer for it, because the
client used up its transient retries. A `success_with_skips` run knows what it
missed and why; a `degraded` run does not. Only Notion emits `degraded`. Slurm
emits `failed` from inside the collector when no cloud answered at all
(`slurm_collector.py:314`).

## Checkpoints

Each collector owns its own checkpoint file under its own manifest directory,
so one source's failure cannot touch another's. The previous position is kept
under `checkpoints/` before the current one is replaced (`archive.py:286-295`).

| Source | Checkpoint holds | Advances when |
|---|---|---|
| Slack | per-channel `high_watermarks`, `thread_watch` (pruned to the 30-day lookback), `skipped_channels` | not a dry run **and not truncated** (`slack_collector.py:576`) |
| Notion | `last_edited_watermark`, `known_objects` | not a dry run **and not truncated** (`notion_collector.py:753`) |
| GitHub | `collected_through` (KST date), `organization`, repository count | not a dry run, **not truncated**, and not `--backfill` (`github_collector.py:509`) |
| Slurm | `collected_through` (KST date), `clouds` | not a dry run, **not truncated**, not `--backfill`, **and every requested cloud answered** (`slurm_collector.py:319-325`) |
| Google Calendar | per-calendar `sync_tokens`, `skipped_calendars`, `reset_calendars` | **not a dry run — that is the only guard** (`calendar_collector.py:254`) |

Four of the five collectors withhold the checkpoint from a truncated run. The
Google Calendar collector does not: `if advance_checkpoint and not
archive.dry_run:` has no truncation term. Calendar's only truncation source is
`max_calendars` (`calendar_collector.py:157-159`), which today is set only by
`--smoke`, and a smoke run is a dry run — so the gap does not currently lose
data. It is a divergence from every other collector, and a future caller that
bounds calendars outside a dry run would advance a sync token past calendars it
never read.

Notion's watermark has a further rule of its own: it advances only across the
contiguous prefix of successfully fetched objects and never steps over a failed
one, and it does not move at all when an object failed unresolved without a
`last_edited_time` (`counters.watermark_held`).

## Date-slice capture

Slack and Notion can capture one bounded historical window instead of resuming
from the checkpoint. `collect()` takes an exclusive upper bound `until`
(`slack_collector.py:198`, `notion_collector.py:464`); passing it changes three
things:

* the checkpoint watermark is ignored, because it records how far the
  *incremental* front has reached and would empty a historical slice;
* `advance_checkpoint` is forced to `False`, because a watermark moved to the
  slice's end would assert that everything before it had been read;
* the run declares `requested_window.until` and a `date_slice` mode, which is
  what lets the coverage dashboard stop attributing the run at the window edge
  instead of at the wall clock (`collection_status.py:450-480`).

Slack additionally skips the watched-thread re-poll and bounds its
workspace-wide searches with `before:` (`slack_collector.py:427`, `:464-468`).
Notion additionally skips the link queue and the re-check sweep, because both
target the live head (`notion_collector.py:521`).

This mode is what makes a month-by-month backfill terminate. **No CLI flag
exposes it**; it is reachable only from Python, or from a caller that
constructs the collector itself. Its known limitation is recorded in the rule
registry: a slice does not recover replies whose thread parent predates the
window, which is the defect rule `V7` is published as pending to describe (see
[collection-rules.md](collection-rules.md)).

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
  of threads carried in the checkpoint (30-day lookback by default,
  `slack_collector.py:52`) and the `search.messages` queries. The re-poll is
  skipped entirely for a bounded or date-slice run
  (`slack_collector.py:427`).
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
  The raw pages already written for it are kept immutably — a partial capture
  is evidence, not garbage. `counters.objects_failed_by_phase` says where each
  one died, and `coverage_complete` is the single field that says whether the
  run saw everything it set out to.
* The API client retries what has no answer yet — read timeouts, dropped
  connections, DNS and protocol faults, HTTP 408/425/429 and 5xx — honouring
  `Retry-After` (seconds or HTTP-date, clamped) and otherwise backing off
  exponentially within a bound. A definitive 4xx is never retried. The counts
  land in `counters.transient_retries`, `transient_retries_by_class` and
  `requests_exhausted`. No error message ever carries the token or a query
  string.
* If an object fails unresolved and carries no `last_edited_time` — a
  link-queue or re-check candidate — there is no position to stop the
  watermark below, so the watermark does not move at all
  (`counters.watermark_held`). A *permanent* answer (400/401/403/404) does not
  hold it: a deleted page 404s on every future run, and holding for it would
  freeze the watermark forever.
* A `/search` walk that dies part-way keeps the objects it did list, marks the
  run truncated (`search_incomplete`) and withholds the checkpoint, because
  objects inside the window may never have been listed at all.
* Data-source rows are pages and arrive through `/search`; the collector does
  not re-query every data source daily.
* A run that ends `degraded` cannot be converted to ledger records at all —
  see [What one `daily-collect` run does](#what-one-daily-collect-run-does).

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
* The checkpoint has no truncation guard — see [Checkpoints](#checkpoints).

**GitHub.** Commits come from local bare mirrors under
`<mirror root>/<repo>.git`, read with `git log`, so a month-long backfill needs
no API budget and has no window limit. Pull requests, reviews, review comments,
issue comments and issues come from REST, one page per call.

* `REST_KINDS` holds **five** kinds — `pull_request`, `review`,
  `review_comment`, `issue_comment`, `issue`
  (`src/rlwrld_worklog/github_collector.py:69`). The `--kinds` help text says
  "Defaults to all six" (`cli.py:69`) and the module comment says "the six
  object kinds" (`github_collector.py:66`); both are counting commits, which
  are not a REST kind. Passing `--kinds none` means commits only.
* Releases, deployments, Actions runs and permission changes are **not**
  collected. They are out of scope for this collector
  (`github_collector.py:66-68`).
* Merge commits are kept, with `parent_count`. Archived repositories are kept,
  with archived state as a field rather than a filter. Repository listing is
  fully paginated.
* The pulls endpoint cannot be filtered by time, so it is paginated
  newest-updated-first and stopped at the window edge. A pull request whose
  last update predates the window is not re-observed even if it was open.
* Whether a mirror holds everything the remote has is answered only by
  `--verify-mirror-refs`, which costs one network round trip per repository.
  Without it, a repository not pushed during the window is covered and every
  other mirror's coverage is reported as unknown rather than guessed.
* A repository the API lists with no mirror has its commits read over REST
  instead, carrying the REST capture profile and no local diff statistics. A
  mirror whose repository the API no longer lists is preserved: its history
  exists nowhere else.
* No blob, file body or source tree is fetched.

**Slurm.** The full 117-column sacct dump for each cloud (`kakao`, `aws`,
`naver`), fetched once per cloud and projected onto KST days locally, because
the endpoint offers neither a time window nor pagination.

* Day keys come from the `End` column, never `Submit`: the naver cluster
  reports an empty `Submit` on every job.
* Finished state is decided by a blacklist of not-finished states. An unknown
  state is kept and counted, never discarded.
* All 117 columns are preserved as a header plus rows. No efficiency or
  classification value is derived at capture time.
* Step rows (`.batch`, `.extern`) hold the only real resource usage and are
  archived, but they are sub-resources of a job rather than activities and have
  no ledger entity type — see [ledger.md](ledger.md).
* Retention differs per cloud and is declared as a coverage note. Work older
  than a cloud's floor is outside what this API can answer.
* The downloaded dump is staged under `staging/slurm/` and unlinked after the
  projection: what is preserved is the archived projection, not the dump
  (`slurm_collector.py:294`).

## Ledger output

`convert_live_run` turns one run manifest into
`<ledger root>/ledger/<source>/live-<run id>.jsonl`, validated against the
standard v1 JSON Schema (`ledger/live.py:1399`).

Before any archived page is decompressed or parsed, its SHA-256 is recomputed
over the file bytes and compared with the manifest entry: the manifest is an
index, and only the bytes on disk are the authority. A missing, malformed or
mismatched hash aborts the whole conversion before anything is written, so a
corrupted or edited raw page can neither become a ledger record nor replace a
ledger file that was built from good bytes.

Records are either **activity** or **dimension**. The full type list per source
is in [ledger.md](ledger.md); it covers twelve activity types and six dimension
types across the five sources, and only four of the activity types are
projected onto `timeline_events`.

`sql/migrations/0003_live_capture.sql` widened the `ledger_records.entity_type`
CHECK for the dimension types; `sql/migrations/0004_github_slurm_sources.sql`
widened it again for the GitHub and Slurm types and widened the `source` CHECK
on six tables. Every type the earlier loader could write is still accepted.

Live records carry a `live-` capture profile and therefore load at
`origin_priority` 100, above every legacy profile (20, or 10 for the Slack
thread-store supplement). A legacy re-run can never demote a live head.

Conversion is deterministic: the same manifest produces byte-identical JSONL,
and loading is idempotent through `ledger_id`.

The load stage offers the whole ledger root for that source, not only the file
this run produced. Files already loaded are skipped by sha256, so a file whose
load failed yesterday is picked up today rather than being stranded.

## After a GitHub or Slurm capture

Neither `github-collect` nor `slurm-collect` runs the ledger or load stages.
Their CLI handlers stop after printing the capture result (`cli.py:453`,
`cli.py:530`). Two commands have to follow:

```bash
ARCHIVE=/data/rlwrld-worklog
LEDGER=$ARCHIVE/staging/ledger

worklog github-collect --since 2026-09-01 --until 2026-09-04 \
    --environment production --archive-root $ARCHIVE

worklog ledger-live-convert github \
    --archive-root $ARCHIVE \
    --manifest $ARCHIVE/manifests/github/production/<run id>.json \
    --out-root $LEDGER

worklog ledger-load github --ledger-root $LEDGER --apply
```

The same three steps apply to Slurm, with `slurm-collect` and `slurm`. Both
sources are accepted by `ledger-live-convert` and `ledger-load` (`cli.py:199`,
`cli.py:171`) but **not** by `ledger-verify` (`cli.py:182`), which still takes
only `slack`, `notion` and `google-calendar`.

Historical windows need `--backfill` on the capture command. It makes the run
independent of the checkpoint in both directions: it neither starts from the
stored watermark nor moves it (`github_collector.py:332-337`).

## Re-running a stage by hand

The package installs a `worklog` console script (`pyproject.toml:20`);
`python3 -m rlwrld_worklog` is equivalent.

```bash
ARCHIVE=/data/rlwrld-worklog
LEDGER=$ARCHIVE/staging/ledger

# re-project one archived run; no API call
worklog ledger-live-convert slack \
    --archive-root $ARCHIVE \
    --manifest $ARCHIVE/manifests/slack/production/<run id>.json \
    --out-root $LEDGER

# load it (dry-run by default; the real transaction, then rollback)
worklog ledger-load slack --ledger-root $LEDGER
worklog ledger-load slack --ledger-root $LEDGER --apply
```

A manifest whose status is `degraded` or `failed` is refused by
`ledger-live-convert`; only `success` and `success_with_skips` convert.
