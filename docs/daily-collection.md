# Incremental collection

Five sources are collected over their official read-only APIs into an immutable
raw archive, projected into the standard v1 ledger, and loaded into the service
database.

All five are driven by one command, and two of them also have a command of
their own for collecting an explicit historical window.

| Source | Daily capture, ledger and load | Window capture by hand |
|---|---|---|
| Slack | `worklog daily-collect` | `worklog collect slack` |
| Google Calendar | `worklog daily-collect` | `worklog collect google-calendar` |
| GitHub | `worklog daily-collect` | `worklog github-collect` |
| Slurm | `worklog daily-collect` | `worklog slurm-collect` |
| Notion | `worklog daily-collect` | `worklog collect notion` |

`daily-collect` runs every source in `SOURCE_ORDER`
(`src/rlwrld_worklog/daily.py:51`), and `--source` accepts exactly that list
(`src/rlwrld_worklog/cli.py:227-232`). All three stages — capture, ledger, load —
run for each of the five.

`github-collect` and `slurm-collect` capture into the same archive under the
same manifest and checkpoint conventions, but they stop after the capture
stage: a window collected with either has to be converted and loaded by hand
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
`degraded` (`src/rlwrld_worklog/notion_collector.py:839`).

A source can also be reported `degraded` while every stage says `ok`: a Notion
capture that finished with unresolved objects sets a degraded reason, and the
source status follows it (`src/rlwrld_worklog/daily.py:781`) so a run summary
can never look cleaner than the manifest behind it.

Sources always run in the order **slack → google-calendar → github → slurm →
notion**, regardless of the order `--source` is given (`daily.py:822`): Slack
and Calendar discover Notion URLs, and Notion drains that queue in the same
run. GitHub and Slurm discover no Notion URL today and are ordered before
Notion anyway, so the rule stays "Notion runs last" rather than a list of which
sources happen to feed it.

### Two kinds of window

Slack, Calendar and Notion are given the `--since` instant directly. GitHub and
Slurm cannot take an instant: they collect a closed interval of **KST calendar
days**, because a KST date is the key their records are filed under and every
window decision they make is a comparison against that day's boundaries
(`github_collector.py:175-219`). `_kst_window` (`daily.py:324-365`) makes the
conversion, and it is the only place in the codebase that makes it — it builds
the same `Window` `github-collect --since/--until` builds, from the daily run's
instant rather than from the command line.

Both edges round outwards:

* the start is the whole KST day that *contains* the since instant, because a
  day is the smallest unit these two sources can express and the alternative to
  re-reading that day's earlier hours is never reading them. Re-reading costs
  nothing: a commit sha and a job id are stable identities, the ledger is keyed
  by them, and the archive refuses to rewrite a page it already holds;
* the end is today's KST date, not the moment the run started, because a day
  still in progress is still the day its records are filed under. Tomorrow's
  run re-reads it and picks up what arrived after this one.

With the default `--since 26h`, that is a two-day window on most days and a
three-day one when the run starts before 02:00 KST.

### A window too wide for one run

An **unbounded** run whose window is wider than `MAX_WINDOW_HOURS` (48 hours,
`daily.py:71-80`) is refused while the config is built — before the lock and
before the first API call — and the refusal names
[`scripts/backfill-days.sh`](scripts.md#backfill-dayssh).

```
$ worklog daily-collect --since 5d
--since 5d asks for a window 5.0 days wide in one run, and anything past 48
hours has to be sliced: one run banks nothing until it finishes, so a failure
part way through loses every day it had already read and moves no checkpoint.
Run it one KST day at a time with scripts/backfill-days.sh <first KST date>
<last KST date>, which banks each day as it finishes and resumes at the first
day that failed. Pass --allow-wide-window to run it as one run anyway.
```

The reason is the one in the message. A `daily-collect` run advances no
checkpoint until it ends, so a five-day catch-up holds all five days inside one
process: hour three failing loses every day already read, and the next run
starts exactly where the dead one did. Day-sized slices bank each finished day
instead — which is how August was backfilled, and what the five-day run started
on 2026-09-05 did not do; two hours in it was still going, with four finished
days unbanked. A KST day is an expensive unit: one measured day of Notion cost
6,053 `blocks/{id}/children` requests, which is why the width of the window is
the thing worth checking rather than the number of sources.

Two windows are deliberately outside the rule:

* **26 hours.** The line is 48 rather than 24 so the nightly incremental
  window, and the three KST days it rounds out to before 02:00 KST, keep
  working untouched.
* **Any run carrying `--until`.** That run is already one named slice, and the
  day runner produces nothing else.

`--allow-wide-window` runs it as one run anyway. The flag's help says what that
costs, because anyone reaching for it is reaching past a refusal.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | every requested source completed every stage it reached |
| 1 | at least one source is `failed` |
| 2 | no source failed, but at least one is `degraded` |
| 3 | another run holds the lock; nothing ran |

`github-collect` and `slurm-collect` return 3 on a lock conflict and 0 on
success; a capture failure propagates the exception after writing a
`status: failed` manifest (`cli.py:498`, `cli.py:575`).

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
(`src/rlwrld_worklog/github_client.py:72-81`) and passed to `gh` through the
child process environment.

The Google token has one extra fallback the others do not
(`daily.py:171-176`): if `<config root>/credentials/google-token.json` is not a
file, `GOOGLE_TOKEN_PATH` is tried, and then the literal relative path
`secrets/google-token.json`.

Slurm needs no credential. `GET /api/download/jobs-raw-<cloud>` is an
unauthenticated request on the tailnet; the presigned S3 URL it redirects to is
treated as a credential and is never logged, returned or written to a manifest
(`src/rlwrld_worklog/slurm_client.py:9-16`).

A missing credential fails that one source and is reported in
`credentials_available`; the other sources still run. `credentials_available`
covers all five sources (`daily.py:142-152`), and Slurm is always `true` there
because it has no credential that can be missing — a four-entry map for five
sources would read as one source with a lost token. No token, OAuth client,
database URL or callback code is ever printed or written to a manifest.

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
locks/daily-collect-<env>.lock                  every source daily-collect runs
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

`--smoke` additionally bounds the API work (`daily.py:58-80`): at most 2 Slack
channels and 25 messages with the workspace-wide searches skipped, 5 Notion
objects with no re-check sweep and a 20-request comment budget, 2 calendars, 2
GitHub repositories with **no REST kind at all**, and 1 of the 3 Slurm clouds.
A full run uses a Notion re-check limit of 100 and no comment budget at all
(`daily.py:641-652`).

The GitHub and Slurm bounds are shaped by what each source can be asked for a
little of at all. GitHub's commits come from the local mirrors, so dropping every REST
kind still exercises listing, mirror read, archive and manifest for the single
API call the repository listing costs. Slurm's dump endpoint has neither a time
query nor pagination, so the smallest thing it can fetch is one cloud's entire
export; `clouds_attempted` records which one was asked for, so the bound is
never mistaken for two quiet clouds.

Raw and ledger files are still written by a dry run. Both are immutable,
deterministic, genuinely observed data, and having them on disk is what makes
a smoke test inspectable.

Most of those smoke bounds call `note_truncation`, so a smoke run's manifest is
`truncated` and the coverage dashboard reads its date as `partial`. Asking
Slurm for one cloud is the exception: it is a narrower request, not a truncated
answer, and the manifest records it as `clouds_attempted`. A plain `--dry-run`
sets no truncation, and the dashboard currently reads its date as `collected` —
see the known defect in
[collection-status.md](collection-status.md#known-defect-a-plain---dry-run-day-reads-as-collected).

`github-collect` and `slurm-collect` take `--dry-run` with the same meaning: no
checkpoint moves (`github_collector.py:509`, `slurm_collector.py:319`). Neither
has a `--smoke` mode of its own; `daily-collect --smoke` is the bounded run for
both.

## Scheduling

Every collection command takes a non-blocking exclusive lock, so a slow run can
never be overlapped by the next one; the second run exits 3 without touching
anything (`daily.py:886-899`).

The three locks are deliberately separate files. A GitHub backfill taking the
daily lock would make the nightly `daily-collect` exit 3 and be read the next
morning as a failed collection (`cli.py:415-421`).

The batch catalogue in `src/rlwrld_worklog/schedules.py:86-127` declares one
batch, `daily-collect`, and declares its runner to be a systemd timer driving a
oneshot service — not a cron entry and not a manual run:

```
timer unit:    hkwa-collect.timer
service unit:  hkwa-collect.service
cadence:       once a day, at the backoffice-configured hour and timezone
concurrency:   non-blocking flock on <RAW_ARCHIVE_ROOT>/locks/daily-collect-<environment>.lock
logs:          <RAW_ARCHIVE_ROOT>/logs/daily-collect/  (latest.log, last.json)
```

systemd, not the backoffice setting, decides when the timer actually fires.
`schedules.py` reports the configured hour and the systemd answer side by side
and never merges them (`schedules.py:274-283`).

The unit files live in `deploy/systemd/` and `scripts/install-incoming-timer.sh`
installs them into the user's systemd. `OnCalendar=*-*-* 01:00:00 Asia/Seoul`
names the timezone rather than assuming the host's, and the unit carries **no
`--source` flags**, so what runs is the default: all five sources.

That last point is the whole reason this unit exists. The batch it replaced
(`worklog-daily.timer`) lived in the system unit directory, out of reach of the
development side, and named two sources explicitly. When the code grew to five,
the installed batch stayed at two, and three sources went uncollected for four
days before anyone could see it. A batch nobody can read is a batch that
drifts. It was disabled on 2026-09-05.

GitHub and Slurm no longer need a catalogue entry of their own: they run inside
the daily collection. `github-collect` and `slurm-collect` remain as hand-run
commands for an explicit window, and anything captured that way still needs
`ledger-live-convert` and `ledger-load` run by hand.

Output is line-oriented JSON: one `daily_collect_config=` line before the run,
one `daily_collect_source=` line as each source finishes, and a final
`daily_collect=` summary (`cli.py:823-828`), so a log can be parsed without
re-reading manifests. The config line never contains the database URL
(`daily.py:914`).

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
| Notion | `counters`, including `coverage_complete`, `objects_failed_by_phase`, `objects_failed_unresolved`, `watermark_held`, `comment_strategy`, `comment_blocks_unswept`, `comment_request_budget`, `comment_requests_made` |
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
| Slack | per-channel `high_watermarks`, `thread_watch` (pruned to the 30-day lookback), `skipped_channels` | not a dry run **and not truncated** (`slack_collector.py:606`) |
| Notion | `last_edited_watermark`, `known_objects` | not a dry run **and not truncated** (`notion_collector.py:845`) |
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
(`slack_collector.py:228`, `notion_collector.py:529`); passing it changes three
things:

* the checkpoint watermark is ignored, because it records how far the
  *incremental* front has reached and would empty a historical slice;
* `advance_checkpoint` is forced to `False`, because a watermark moved to the
  slice's end would assert that everything before it had been read;
* the run declares `requested_window.until` and a `date_slice` mode, which is
  what lets the coverage dashboard stop attributing the run at the window edge
  instead of at the wall clock (`collection_status.py:450-480`).

Slack additionally skips the watched-thread re-poll and bounds its
workspace-wide searches with `before:` (`slack_collector.py:457`, `:498`).
Notion additionally skips the link queue and the re-check sweep, because both
target the live head (`notion_collector.py:603`).

This mode is what makes a month-by-month backfill terminate, and `--until`
exposes it on both `worklog collect` and `worklog daily-collect`. A range of
days is run through [`scripts/backfill-days.sh`](scripts.md#backfill-dayssh),
which produces exactly these slices, one per KST day, and banks each one as it
finishes:

```bash
# a range of days, one bounded run each, resumable
scripts/backfill-days.sh 2026-08-01 2026-08-31

# one slice by hand: four sources, ledger and load included
worklog daily-collect --environment production \
    --source slack --source notion --source github --source slurm \
    --since 2026-08-01 --until 2026-09-01

# or one source at a time, capture only
worklog collect slack --since 2026-08-01 --until 2026-09-01
```

The middle command is a month in one run. It is legal — a run carrying
`--until` is a named slice and is never refused for its width — but it banks
nothing until it finishes, so a range worth more than a day or two belongs in
the day runner.

The bound is **exclusive**, and a bare `YYYY-MM-DD` means midnight *KST* that
day (`slack_collector.py:94-121`) — so `--until 2026-09-01` covers exactly
August. A date is not an instant, and KST is the only calendar these sources
file records under; reading a bare date as UTC would put every slice boundary
nine hours out. An ISO 8601 instant is accepted too and keeps the offset it
carries, as `--since` does. A duration is refused: "everything before 26 hours
ago" is a window nobody means to ask for.

Note the collision of names. `github-collect --until` and `slurm-collect
--until` predate this flag and name the **last day, inclusive**; the flag on
`collect` and `daily-collect` is an exclusive bound. Both help strings say so.

Four of the five sources take it. Google Calendar is refused, before the lock
and before any API call: its incremental read is a per-calendar sync token, so
an upper bound cannot be expressed at all, only ignored — and a run that
ignored it would collect the live head and file it under the requested window's
name, which afterwards is indistinguishable from a window that was genuinely
empty. Naming the sources explicitly with `--source` is how a `daily-collect`
slice is run.

GitHub and Slurm take the bound as their window's end — the KST day holding the
last instant the bound admits — together with `backfill=True`, which is their
own name for ignoring the checkpoint in both directions.

No run carrying `--until` advances a checkpoint. Each collector enforces that
for itself, and `_advance_checkpoint` (`daily.py:368-380`) enforces it again
for all four, because a guarantee that depends on four collectors each
remembering it is not a guarantee.

The mode's known limitation is recorded in the rule registry: a slice does not
recover replies whose thread parent predates the window, which is the defect
rule `V8` is published as pending to describe (see
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
  `slack_collector.py:55`) and the `search.messages` queries. The re-poll is
  skipped entirely for a bounded or date-slice run
  (`slack_collector.py:457`).
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
comments under one of two named sweep strategies, users, and the
archived/in-trash flags.

* `/search` is **not a change feed**: it omits archived and trashed objects and
  anything not shared with the integration, and it can lag an edit. Deletion,
  trashing and a lost share become observable only through the bounded
  re-check of previously seen objects (100 per run by default), which reports
  a 404 as a skip rather than as a clean day.
* Comments live on the block they were left on, so an exhaustive sweep costs
  one `/comments` request per block. One completed day measured what that is
  worth: 176 pages, 6,053 block requests, 1,500 comment requests — and one
  comment. So the sweep has two named strategies
  (`notion_collector.COMMENT_STRATEGIES`), and every manifest records which one
  ran in `requested_window.comment_strategy` and `counters.comment_strategy`:
  * `page_first` (**the default**) asks each object for its own comments and
    walks its blocks only when that answered with at least one. A page with no
    discussion costs one request instead of dozens.
  * `every_block` is the old exhaustive sweep, kept for a run that wants it.

  The default is a **narrowing of coverage, not a free saving**: an inline
  comment on a block of a page that carries no page-level comment is not
  fetched, and its absence from a run is not evidence it does not exist. That
  is stated in the `notion.comments_page_first` coverage note on every run that
  uses it, and `counters.comment_blocks_unswept` counts the blocks never asked
  about. The run is *not* marked truncated for it — the narrowing is the rule
  the run followed, not a bound it hit — so the checkpoint still advances.
* Neither strategy caps itself by default: the bound is the API client's
  rate-limit handling. A finite default budget would make any large workspace
  mark itself truncated, withhold its checkpoint, and then redo the identical
  window on every run. An operator can still pass a finite budget; when one is
  set it is reported in `comment_request_budget` / `comment_requests_made`,
  adds the `notion.comment_requests_capped` coverage note, and reaching it does
  mark the run truncated and withhold the checkpoint. Smoke runs use a small
  explicit budget for exactly that reason.
* **Mentions are extracted, and cost nothing extra.** The user mentions inside
  the `rich_text` of blocks and comments the run already fetched are pulled out
  and carried both on the timeline event (`mentions`, as `MentionKind.DIRECT`)
  and on every ledger record (`relations.mentioned_user_ids`, alongside
  `relations.mentions_extracted: true`). Who *edited* a page was already free —
  `created_by` and `last_edited_by` ride on the page object. Page, database,
  date and link_preview mentions are deliberately not turned into a `Mention`:
  a mention carries a direction, and one document naming another has no
  direction to state. Those stay in the raw block JSON, which the ledger keeps
  verbatim.
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

## After a GitHub or Slurm capture *by hand*

The daily batch converts and loads both sources itself. `github-collect` and
`slurm-collect` do not: their CLI handlers stop after printing the capture
result (`cli.py:507`, `cli.py:584`), so a window collected with either needs
two more commands to follow it.

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
