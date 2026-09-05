# Collection status and coverage

`src/rlwrld_worklog/collection_status.py` is the read-only derived view behind
the 수집 현황 backoffice page. It answers one question per cell: **for this KST
date and this source, what does the evidence actually say?**

Everything it reads — raw run directories, run manifests, checkpoints, ledger
JSONL, legacy `daily_raw` directories, progress snapshots — is external
evidence and immutable. Nothing here writes, moves or deletes any of it. The
whole view is derived and can be thrown away and rebuilt at any time.

Four rules the reader follows (`collection_status.py:9-25`):

* **No path comes from a caller.** Sources, environments and run ids are
  matched against the names the filesystem actually offers; a name that is not
  a single safe path segment is dropped before it is ever joined. Every
  resolved path is re-checked to be inside `RAW_ARCHIVE_ROOT` or
  `APP_CONFIG_ROOT`.
* **No raw content is exposed.** Counts, kinds, endpoint names, timestamps,
  local identifiers and paths only. A skip or an error is reported by its
  `kind` and its count, never by its details.
* **Absent evidence is never collected.** A date with nothing behind it is
  `not_collected`; a date whose evidence cannot be read is `unknown`. Neither
  is ever rounded up to "collected".
* **Expensive work is bounded and cached.** A completed run is summarized from
  its manifest, never by walking its raw directory. Only a run with no manifest
  is walked, with a hard entry cap and a short TTL cache.

All five sources are in scope: `SOURCE_TO_COLLECTOR`
(`collection_rules.py:68`) covers `slack`, `notion`, `google_calendar`,
`github` and `slurm`. The dashboard uses the ledger source names; the archive
directories use the collector names, so Calendar is `google-calendar` on disk.

## Where the evidence comes from

Three independent kinds of evidence feed a cell.

1. **Run manifests** — `manifests/<source>/<env>/<run id>.json`. The strongest
   evidence: a finished, recorded observation. A repeat manifest
   (`<run id>.<n>.json`) collapses into the same run row.
2. **Progress snapshots** — `$APP_CONFIG_ROOT/collection-status/progress/<source>/<env>/<run id>.json`,
   written by `collection_progress.RunProgress` while a run is in flight. A
   capture that crashed leaves no manifest, so without this it would be
   invisible. Each snapshot carries `updated_at`, `pid` and `host`;
   `snapshot_liveness` (`collection_progress.py:302`) grades an unfinished one
   as `running`, `stale` or `unknown`. A snapshot whose process is gone on this
   host is stale immediately; otherwise one that has not advanced for
   `DEFAULT_STALE_AFTER_SECONDS` (1800) is stale. A finished snapshot never
   overrides the manifest.
3. **Legacy `daily_raw` directories** — the V0 dumps. A directory name is the
   only identity they carry.

Snapshots are written only when a config root is configured
(`collection_progress.py:71-83`); a collector run from a bare shell publishes
none, and the dashboard then sees only manifests.

## Verdict vocabulary

Nine values, defined at `collection_status.py:1195-1216`. They are deliberately
distinct; none is a synonym for another.

| Verdict | Meaning |
|---|---|
| `collected` | Every run touching this date finished cleanly, with no skips, no truncation and no failure. |
| `collected_with_skips` | At least one run finished `success_with_skips` and none was incomplete. The run named everything it could not reach. Honest reporting is not a defect, so it is kept apart from `partial`. |
| `partial` | Something is incomplete: a `degraded` or `failed` run alongside others, a `truncated` run, a stale run beside a settled one, or a malformed manifest beside a readable one. |
| `running` | A run touching this date is in flight. |
| `failed` | **Every** run touching this date failed. |
| `not_collected` | No evidence of any kind exists, and the legacy inventory is complete enough to say so. |
| `unknown` | Evidence exists but does not say whether the date was collected: every manifest was malformed, or the only evidence is a crashed run that wrote raw pages and no manifest. Also used when the legacy inventory itself is incomplete, so absence is not evidence. |
| `unverified` | A V0 legacy date. A directory exists, and that is all it proves: the legacy dumps carry no run identity and their `meta.json` `status` is hardcoded, so nothing about them can assert completeness. |
| `unexamined` | A V0 legacy date whose `meta.json` was never opened, because the requested range was wider than `MAX_LEGACY_META_PROBE_DAYS` (62 days). Distinct from `unverified`: that one means the record was read and proves nothing; this one means nobody looked. Collapsing the two would let a query's own width change a date's verdict — the same date would read `partial` in a 30-day window and `unverified` in a 276-day one. |

## Three axes, not one

A cell carries three separate judgements. Merging them would let a badge claim
more than the evidence supports.

**`coverage`** — one of the nine values above. Observation quality.

**`completeness`** — whether the date is finished, gated by the time axis:

| Value | Set when |
|---|---|
| `complete` | `coverage` is `collected` **and** `time_coverage` is `complete`. |
| `complete_with_known_gaps` | `coverage` is `collected_with_skips` and time coverage is complete. |
| `in_progress` | The KST date has not ended yet. |
| `incomplete` | `coverage` is `partial` or `failed`, or no run observed past the date's end. |
| `unknown` | Anything else, including every legacy cell without a truncation warning. |

**`time_coverage`** (`collection_status.py:1231-1233`, `_time_coverage` at
`:1246`) — whether the whole of a KST date has been observed yet:

* `in_progress` — `now` is before the date's end. A date still in progress can
  never be complete, however clean the runs that touched it are: the hours that
  have not happened cannot have been collected.
* `complete` — some run's observation window ends at or after the date's end.
* `partial` — the date is over, but no run observed past its end.

A date is only settled once a run has read past its end, because
`search.messages` is an index and lags the live channel; the 26-hour default
window is the overlap that covers it. Legacy V0 cells have no observation
window at all, so `time_coverage` is left `null` rather than invented.

**`evidence_class`** (`collection_status.py:1221-1223`, `_run_evidence_class`
at `:1294`) — which grade of evidence backs the cell:

* `manifest` — every run behind it wrote a manifest.
* `directory_only` — no run behind it wrote one; a directory being written or
  abandoned proves only that something ran.
* `mixed` — both, or a V1 run-backed date that a V0 legacy dump also covers.

## How one day-source cell is computed

`coverage()` (`collection_status.py:1548`) walks each KST date in the requested
range and, per source:

1. Selects every run whose observation window intersects that date's KST bounds
   (`_run_intersects_day`, `:1263`).
2. If any run matched, builds the cell from those runs (`_cell_from_runs`,
   `:1308`) and applies the time axis (`_apply_time_coverage`, `:1420`).
3. If none matched but a legacy directory exists, builds a legacy cell
   (`_legacy_cell`, `:1479`).
4. If neither, the cell is `not_collected` — or `unknown` when the legacy
   inventory is itself incomplete, with a note saying absence is not evidence.

### What "the run's window" means

`_window()` (`collection_status.py:450`) takes the window from the manifest's
`requested_window`, not from when the process ran:

* start = `since_effective` or `since`, falling back to `started_at`;
* end = `until`, falling back to `finished_at`, then `started_at`;
* `end_is_declared` is true only when the manifest declared `until`.

That flag changes the intersection test. A declared end is the collector's
**exclusive** upper bound: a date slice with `until` at 8/20 00:00 KST
collected nothing at that instant, so it has nothing to say about 8/20. An
undeclared end is just when the process stopped, and that instant was inside
the observation. Taking `end` from `finished_at` for a bounded run is what once
made a single 8/19 slice count toward every date from 8/20 to today, letting a
run that never looked at a date decide that date's verdict.

A run whose window is longer than `MAX_COVERAGE_DAYS_PER_RUN` (400 days) is
clamped to the last 400 days before the test.

### Verdict precedence

`_cell_from_runs` evaluates in this order (`:1323-1342`). The first match wins:

```
any state == running                                  -> running
all states == malformed                               -> unknown
all states == failed                                  -> failed
stale present and no settled run                      -> unknown
incomplete, or stale, or malformed/unknown present    -> partial
success_with_skips present and nothing incomplete     -> collected_with_skips
otherwise                                             -> collected
```

where `incomplete` is `any state in {"degraded", "failed"}` **or** any run
`truncated` (`:1319-1320`), and a "settled" run is one whose state is `success`
or `success_with_skips`.

`degraded` is a stronger failure signal than `success_with_skips`: a degraded
run does not know what it missed, while a run with skips named every one of
them. Only `degraded`, `failed` and truncation make a date incomplete.

### A whole-day re-read supersedes what came before it

Runs on a date are first split by `_effective_runs()` into the ones that still
speak for the date and the ones a later re-read has answered. The verdict is
computed from the first group only, through the same `any()` / `all()` over
states as before.

A run supersedes earlier runs only if all three conditions in
`_reread_whole_day()` hold:

| Condition | Why it is there |
| --- | --- |
| state is `success` or `success_with_skips` | a failure says nothing about what is there |
| not `truncated` | a truncated run is precisely one that knows it stopped early |
| window spans the whole KST date | a run that re-read two hours cannot speak for the other twenty-two |

So a date with a failed 03:10 run and a clean full-day 09:00 re-run reads
`collected`. A date whose re-run covered only the afternoon, or was truncated,
or itself failed, still reads `partial`. Supersession runs forwards only: a
failure *after* a clean re-read is not cleared by it.

The date's history is not erased. `runs` still counts every run that touched
the date, `runs_superseded` says how many the re-read answered, and a note on
the cell says so in words.

Before this, every run intersecting a date voted forever, so a repaired gap
could not be shown as repaired on the screen that reported it.

Ordering is otherwise used for exactly two fields: `last_status` and
`last_run_id` come from the run with the greatest `last_activity_at`, falling
back to `started_at`, with the run id breaking exact ties (`_run_order_key()`).

### `truncated` forces incomplete

`truncated` is read straight from the manifest and OR-ed across every run on
the date (`:1319`). It feeds `incomplete` directly, so a truncated run makes
the date `partial` **regardless of that run's own `status`**. A manifest can
say `success` and still produce a `partial` cell, because a truncated run
by definition did not read everything its window covered.

This is also why a `--smoke` run shows as `partial`: each smoke bound calls
`note_truncation`.

### Legacy V0 cells

A date with only legacy directories gets `unverified` when its `meta.json` was
read and `unexamined` when the requested span exceeded 62 days. If a probed
`meta.json` records truncation warnings, the cell becomes `partial` with
`completeness: incomplete`. `runs` is `null` and `runs_known` is `false`: V0
dumps carry no run identity, so the number of runs behind the date cannot be
counted.

When a date has both V1 runs and V0 directories, **the V1 verdict stands**. The
V0 evidence is recorded alongside it — an extra `rule_versions` entry and an
`evidence_class` of `mixed` — rather than blended into the verdict
(`:1613-1619`).

## Dry runs are set aside, not counted

A dry run reads the source and keeps nothing: no checkpoint advances, the link
queue is a read-only view, and the database transaction is rolled back. The
date is exactly as uncollected afterwards as before.

`_cell_from_runs` therefore removes dry runs from the evidence before computing
the verdict. They are evidence of neither collection nor failure, so setting
them aside is not the same as treating them as a failure:

| The date's runs | Verdict |
| --- | --- |
| one real success, one failed dry run | `collected` — the dry run was never going to keep anything |
| dry runs only | `not_collected`, `completeness: incomplete` |
| a failure, then a clean full-day dry run | `failed` — a dry run cannot supersede |

`runs` still counts every run that touched the date, and a note names how many
were dry runs.

This was a defect until 2026-09-05: `_cell_from_runs` did not consult
`dry_run`, a `--smoke` run happened to be caught because every smoke bound
calls `note_truncation`, and a plain `worklog daily-collect --dry-run` produced
an ordinary `success` manifest that painted the date `collected`.

## Rule attribution

Every run row carries a `rule` block from `classify_rule()`
(`collection_status.py:327`), which separates a recorded fact from a
reconstruction:

| `attribution` | Meaning |
|---|---|
| `declared` | The manifest carries `collection_rule_version` itself. |
| `inferred` | The manifest predates the stamp, but its capture profile, schema version, location, run-id format and start time identify it as a run of the current collector. |
| `legacy` | The evidence is a legacy `daily_raw` directory, which is V0. |
| `unknown` | The evidence does not identify a rule. Nothing is assumed. |

A `declared` block also reports `declares_source` — whether the stamped rule
actually names the source being captured. A run stamps the rule that was active
when it started, which for the August GitHub runs was `V2`, and `V2` does not
name GitHub. That manifest is not wrong and is never rewritten; the view says
`declares_source: false` instead of failing or quietly filling the gap with the
current rule.

`digest_matches_registry` is `null` rather than `false` when the digest cannot
be judged — an unknown version, or no digest at all. A manifest written before
the digest definition changed carries the earlier value, which
`digest_is_recognised` still accepts; reporting it as a mismatch would flag real
runs as tampered with. See [collection-rules.md](collection-rules.md).

Each cell's `density` comes from the rule's `SourceRule.density_kind` for that
source, so a day-sliced legacy dump and a continuously resumed capture are
never compared on the same scale.

## Bounds

Every limit exists so one HTTP request cannot turn into an unbounded walk of a
300 GB archive (`collection_status.py:79-87`):

| Constant | Value | Effect |
|---|---|---|
| `MAX_RAW_ENTRIES_SCANNED` | 250,000 | Cap on walking a manifest-less run directory. The cell says it was capped. |
| `MAX_ACTIVE_DAY_DIRS` | 14 | How many day directories an active-run scan looks at. |
| `MAX_COVERAGE_DAYS` | 400 | A wider range is clamped to the last 400 days and `range_truncated` is set. |
| `MAX_COVERAGE_DAYS_PER_RUN` | 400 | A longer run window is clamped before the intersection test. |
| `MAX_LEDGER_COUNT_BYTES` | 512 MiB | Cap on counting ledger records from JSONL. |
| `MAX_LEGACY_META_PROBE_DAYS` | 62 | Beyond this span, legacy `meta.json` is not opened and cells read `unexamined`. |
| `LEGACY_INVENTORY_MAX_SECONDS` | 5.0 | The legacy directory index gives up rather than blocking, and reports itself incomplete. |
| `RETAINED_SNAPSHOTS` | 200 | Progress snapshots kept per source/environment; older ones are pruned. |

The response reports `legacy_meta_probe_limit_days` and `requested_span_days`
so a reader can see why a probe was skipped and how far to narrow the range to
get a verdict instead of an `unexamined`.

Caches are TTL-bounded per kind — manifests and ledger counts for an hour, raw
scans for 15 seconds, the legacy inventory and meta for 5 minutes — and can be
cleared by group with `clear_caches()` (`:153`).
