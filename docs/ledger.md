# Standard v1 ledger

The ledger is the system of record for **historical observations** of Slack,
Notion, Google Calendar, GitHub and Slurm objects. Live API collection writes
the **current head**; converted legacy files never overwrite it.

`SOURCES` in `src/rlwrld_worklog/ledger/schema.py:76` is the authoritative
list: `("slack", "notion", "google_calendar", "github", "slurm")`. The JSON
Schema constrains `source` to exactly those five.

## Pipeline

Three inputs, one ledger, one loader:

```
official API      ──capture──▶ raw archive ──convert──▶ ledger JSONL ──load──▶ PostgreSQL
git mirrors + gh  ──capture──▶ (immutable)              (staging disk)        ledger_records
sacct dump        ──capture──▶                                                + service projection
legacy daily_raw ────────────────convert──────────────▶
   (read-only)
```

Each stage is independently re-runnable and independently verifiable. The live
path is `worklog daily-collect` for Slack, Calendar and Notion, and
`worklog github-collect` / `worklog slurm-collect` followed by a manual
`ledger-live-convert` and `ledger-load` for the other two — see
[daily-collection.md](daily-collection.md). This file covers the ledger format
that every path writes.

Not every input is an HTTP API. GitHub commits are read with `git log` from
local bare mirrors, and Slurm rows come from a gzipped pipe-separated sacct
dump. Both are archived and hashed exactly like an API response.

## Entity types

`ACTIVITY_ENTITY_TYPES` and `DIMENSION_ENTITY_TYPES`
(`ledger/schema.py:43-75`) hold twelve activity types and six dimension types.

| Source | Activity | Dimension |
|---|---|---|
| Slack | `message` | `user`, `usergroup`, `conversation` |
| Notion | `page`, `block`, `comment` | `data_source`, `user` |
| Google Calendar | `event` | `calendar` |
| GitHub | `commit`, `pull_request`, `review`, `review_comment`, `issue`, `issue_comment` | `repository` |
| Slurm | `job` | — |

**Being an activity type is not the same as being projected onto the
timeline.** The loader projects four types and no others:

```python
PROJECTED_ENTITY_TYPES = {"message", "page", "comment", "event"}
```

`src/rlwrld_worklog/ledger/load.py:42`; the filter is applied at `load.py:466`.
So `block` and every GitHub and Slurm activity type land in `ledger_records`
but never in `timeline_events`. `EVENT_TYPE_BY_ENTITY` (`load.py:44`) maps only
those same four.

The comment on `ledger_records.entity_type` written by
`sql/migrations/0004_github_slurm_sources.sql:69-75` says the GitHub and Slurm
activity types "are projected onto `timeline_events`". That comment does not
match `PROJECTED_ENTITY_TYPES`. The loader is what runs; treat the comment as
stale.

Dimension entities exist because rule 2 requires the service database to be
rebuildable from ledger data, and a message row without its channel and its
author is not rebuildable. They stay out of `timeline_events` because a channel
is not an activity.

Slurm step rows (`.batch`, `.extern`) have **no** entity type at all. They hold
the real resource usage and the raw archive keeps all 117 columns of them, but
projecting them would turn one job into several timeline events; they need a
third category alongside activity and dimension, which has not been decided.
The converter counts what it skipped as
`unhandled_kinds["slurm_step_rows_not_converted"]` (`ledger/live.py:995-996`).

## Field set

The JSON Schema (`ledger/schema.py:189-349`) defines **25 properties** and
marks **22 of them required**:

```
ledger_id, schema_version, capture_profile, source, tenant, scope,
entity_type, source_entity_id, source_entity_key, source_created_at,
source_updated_at_status, deleted_state, raw_payload, content_hash,
relations, provenance, coverage, observation_window, capture_completeness,
supplement_provenance, visibility_routing, denormalized_label_snapshot
```

The three optional properties are `source_revision_id`, `source_updated_at`
and `collected_at`. The top level is `additionalProperties: false`, so a
converter cannot add a field without changing the schema.

`provenance` has required members of its own: `source_file`,
`source_file_sha256`, `record_pointer`, `legacy_layout_version`,
`converter_version`.

The module docstring at `ledger/schema.py:3` describes the field set as
"Codex's standard v1 (17 fields) with the five approved ledger extensions".
That count is wrong — the `LedgerRecord` dataclass at `ledger/schema.py:119`
has 25 fields, twenty base plus the five extensions. The schema is the
authority; the docstring has not been corrected.

The five extensions are `observation_window`, `capture_completeness`,
`supplement_provenance`, `visibility_routing`, `denormalized_label_snapshot`.

Deliberately **not** ledger fields:

| Item | Where it lives | Why |
|---|---|---|
| `extracted_text` | `ledger_extracted_text` | Collector-derived text the live API cannot return again. Preserved verbatim as its own artifact. |
| `derived_attribution` | `derived_attribution` | Mixes observation with inference and duplicates per person. |
| `roster_identity_link` | `roster_identity_link` | Each source matched people on a different key. |
| `computed_metrics` | `computed_metrics` | Derived values must not sit beside observations. |
| `legacy_layout_version` | `provenance.legacy_layout_version` | Parser provenance, not a ledger field. |

## Rules the code enforces

1. Legacy inputs are opened read-only. Nothing writes, moves, or deletes them.
2. `.rsync-partial` is excluded by **path**, not by parse error — some of those
   files parse cleanly and would otherwise be ingested.
3. Attribution buckets are never converted. They are skipped by container name
   so a rename cannot silently pull them in.
4. Gemini notes and Notion `_blocks_text` are preserved byte-for-byte.
5. Unresolvable values are recorded as `unknown`, never inferred. A null
   `source_updated_at` with status `unknown` does **not** mean "never edited".
6. Slack identity is `(workspace_id, channel_id, ts)`.
7. Origin priority is derived per record from `capture_profile`: a `live-`
   profile loads at 100, a primary legacy capture at 20, and the Slack
   thread-store supplement at 10 (`load.py:35-37`, `:222-227`). A supplement
   fills gaps without replacing a primary capture, and a legacy re-run can
   never demote a live head.
8. Every row carries `source_file` + `source_file_sha256` + `record_pointer`,
   all `NOT NULL`, so any value is traceable back to an exact byte range of an
   exact source file.

## Identity and duplicates

`ledger_id = uuid5(source, entity_type, tenant, scope_key, entity_id,
observation_window.start, content_hash)` (`ledger/schema.py:105-116`).

* The **observation window** is part of identity because legacy files are day
  slices. The same object on two days is two observations, not one row.
* `content_hash` collapses byte-identical copies automatically.
* `scope_key` participates only when it is part of the entity's natural key:
  a Slack message belongs to a channel, a Notion block belongs to a page, a
  GitHub commit belongs to a repository, a Slurm job belongs to a cluster. A
  Notion **page** and a Google **calendar** are identified by their own id
  alone — which query surfaced them is scope, not identity — so callers pass
  an empty `scope_key` there.

Each converter also keeps an in-run `seen` set of `ledger_id`s and drops a
repeat rather than emitting it twice (`ledger/live.py:258`, `:1153`, `:1281`).
That is what collapses one commit arriving under two mirrored names when a
repository was renamed; the raw pages keep both observations.

Three different things get counted separately, because they mean different
things:

| Signal | Meaning | Verdict |
|---|---|---|
| `identical_content_rows` | byte-identical rows for one entity and window | must be 0; a failure |
| `entities_with_multiple_observations_in_window` | two capture paths saw the same object the same day and their copies differ | expected; kept |
| `entities_seen_in_multiple_windows` | the same object observed on several days | expected; separate observations |

The middle one matters: for Slack, only the `search.messages` copy carries
`edited`, so collapsing the pair would silently destroy edit metadata.

## Commands

The package installs a `worklog` console script (`pyproject.toml:20`);
`python3 -m rlwrld_worklog` is equivalent.

```bash
LEGACY=/data/rlwrld-worklog/legacy/claude/weekly
LEDGER=/data/rlwrld-worklog/staging/ledger

# 1. schema
worklog ledger-schema --output /tmp/ledger-v1.schema.json

# 2. convert legacy (dry-run first; writes nothing)
worklog ledger-convert slack  --legacy-root $LEGACY --out-root $LEDGER --dry-run
worklog ledger-convert slack  --legacy-root $LEGACY --out-root $LEDGER
worklog ledger-convert notion --legacy-root $LEGACY --out-root $LEDGER --salvage-comments

# 3. migrate (dry-run by default; --apply to commit)
worklog ledger-migrate --database-url "$DATABASE_URL"
worklog ledger-migrate --database-url "$DATABASE_URL" --apply

# 4. load (dry-run by default; runs the real transaction then rolls back)
worklog ledger-load slack --ledger-root $LEDGER --database-url "$DATABASE_URL"
worklog ledger-load slack --ledger-root $LEDGER --database-url "$DATABASE_URL" --apply

# 5. verify
worklog ledger-verify slack --ledger-root $LEDGER --legacy-root $LEGACY \
    --database-url "$DATABASE_URL" --report $LEDGER/_verify/slack.json
```

Which sources each subcommand accepts differs, and the difference matters:

| Subcommand | Accepted sources | Defined at |
|---|---|---|
| `ledger-convert` (legacy) | `slack`, `notion`, `google-calendar` | `cli.py:141` |
| `ledger-live-convert` | `slack`, `notion`, `google-calendar`, `github`, `slurm` | `cli.py:199` |
| `ledger-load` | `slack`, `notion`, `google-calendar`, `github`, `slurm` | `cli.py:171` |
| `ledger-verify` | `slack`, `notion`, `google-calendar` | `cli.py:182` |

**GitHub and Slurm records can be converted and loaded but not verified.**
There is no `ledger-verify github`. The duplicate, provenance and
database-parity checks below are unavailable for those two sources until the
subcommand's `choices` list is widened.

Optional flags worth knowing:

* `--root hk_private` adds the owner-only visibility tier, which is **not**
  included by default. It holds Slack thread messages that the shared and
  personal tiers do not carry.
* `--salvage-comments` (Notion) lifts comment objects out of attribution
  files. Notion comments exist nowhere else. Every salvaged row is marked
  `source_file_kind="attribution"` and carries its own capture profile, so the
  decision is reversible with a single provenance filter.

## What `ledger-verify` checks

`verify_ledger` (`ledger/verify.py:74`) walks
`<ledger root>/ledger/<source>/*.jsonl` and reports:

* file and record counts, `by_entity_type`, `by_capture_profile`;
* per-record JSON Schema validation;
* `unknown_counts` — how many rows carry an unknown tenant, an unknown
  `source_updated_at_status`, an unknown deleted state, a `not_recorded` or
  `unknown` capture completeness, an unknown calendar scope, or a visibility
  routing anomaly;
* duplicates, split into the three signals above, plus how many entities appear
  under both a company-wide and a restricted container in one window;
* the observation-date range and how many rows have no usable date;
* provenance: a deterministic evenly-spaced sample of the referenced source
  files (200 by default, `--provenance-sample`) is re-hashed against
  `--legacy-root`;
* with `--database-url`: row counts in `ledger_records`,
  `ledger_extracted_text` and `timeline_events`, heads by origin, and rows
  whose `source_file` or `source_file_sha256` is empty.

The report is `ok` only when `failures` is empty. A **failure** is: any
`identical_content_rows`; any schema-invalid record; any record with no source
file or hash; any provenance hash mismatch or missing file; database rows
without provenance; or a database-versus-files count mismatch for ledger
records or extracted text. A cross-visibility container pair is a **warning**,
not a failure (`verify.py:218-222`).

`--legacy-root` is optional. Without it the provenance check reports
`skipped_no_legacy_root: true` and re-hashes nothing.

## Re-running

Conversion is deterministic: the same input produces byte-identical JSONL, so
re-running and diffing is meaningful. Loading is idempotent through
`ledger_id` plus `ON CONFLICT DO UPDATE`, and skips files whose sha256 already
appears in `ledger_batches` unless `--reload-unchanged` is given
(`load.py:332-336`).

## Migrations

`sql/migrations/` holds four files, applied in order by `ledger-migrate`, which
records each one in `schema_migrations` with a checksum.

| File | What it does |
|---|---|
| `0001_schema_migrations.sql` | Creates the `schema_migrations` bookkeeping table. Applied before every other migration. |
| `0002_ledger_v1.sql` | The standard v1 ledger tables. Assumes the baseline `sql/schema.sql` has been applied: it takes foreign keys on `people` and fixes the baseline `timeline_events` source CHECK, which omits `notion`. |
| `0003_live_capture.sql` | Widens `ledger_records.entity_type` for the five dimension types the live path added. |
| `0004_github_slurm_sources.sql` | Widens the `source` CHECK on `ledger_batches`, `ledger_load_runs`, `ledger_records`, `ledger_extracted_text`, `sync_runs`, `raw_objects`, `identities`, `timeline_events` and `source_object_observations` to admit `github` and `slurm`, and widens `ledger_records.entity_type` again for the six GitHub activity types, `job`, and `repository`. |

Every widening is backward compatible: every source and entity type an earlier
loader could write is still accepted, and no existing row changes. `0004`
rewrites the `timeline_events` source CHECK that `0002` set, rather than
editing `0002`, so `0002`'s recorded checksum stays stable.

## Live capture runs

`worklog daily-collect` runs the conversion itself for Slack, Notion and Google
Calendar. GitHub and Slurm captures do **not** convert themselves;
`ledger-live-convert` has to be run against the archived manifest afterwards.
The same command re-runs a conversion for any already-archived run without
touching an API:

```bash
worklog ledger-live-convert slack \
    --archive-root $ARCHIVE \
    --manifest $ARCHIVE/manifests/slack/production/<run id>.json \
    --out-root $LEDGER
```

Output is `<out-root>/ledger/<source>/live-<run id>.jsonl`, one file per
capture run rather than one per observation day. The loader takes the
observation date from the rows when the filename is not a date, so
`ledger_batches` still gets a real `observation_date` (`load.py:230-237`,
`:341`).

Only a manifest whose `status` is `success` or `success_with_skips` can be
converted (`ledger/live.py:81-85`). A `degraded` Notion run and a `failed` run
of any source are archived but produce no ledger record.

### The observation window is not the same for every source

A live record's observation window is a capture-time fact, not the age of the
object: re-observing an unchanged message tomorrow is a second observation,
exactly as it is for a legacy day slice. But which day is used differs:

| Source | Window | Timezone | Set at |
|---|---|---|---|
| Slack, Notion, Google Calendar | the UTC date of the run's `finished_at` — the capture day, one window for the whole run | `UTC` | `ledger/live.py:131-134` |
| GitHub | the KST date of the record's own activity timestamp | `Asia/Seoul` | `ledger/live.py:938` |
| Slurm | the KST day the archived page was filed under, taken from the `End` column | `Asia/Seoul` | `ledger/live.py:1244-1249` |

All three use `granularity: "day"`. A GitHub or Slurm run therefore emits rows
in several observation windows at once, and a `--backfill` window emits one per
day it covers, while a Slack run emits everything under a single capture day.

### Hash verification

Every archived page a converter reads is verified against its manifest SHA-256
before it is parsed (`ledger/live.py:96-124`). Conversion aborts on a mismatch
rather than emitting records or replacing an existing ledger file.

The Notion converter is the only one that skips pages it will not use: a
`/search` listing or a paginated `property-*` page is archived for the record
but contributes no ledger row, and the converter `continue`s before reading it
(`ledger/live.py:604-610`). The Slack, Calendar and GitHub converters read and
hash **every** file the manifest lists, including listings that produce no row.

### Coverage carried into every record

Coverage the collector could not obtain is carried into the records rather than
dropped: `coverage.permission_gap` counts the run's skips by kind
(`live.py:185-193`), `capture_completeness.truncated`, `truncation_events` and
`rate_limit_hits` come from the run manifest (`live.py:163-183`), and
`capture_completeness.lossy_fields` names what was reduced —
`{"files": "metadata_and_links_only"}` for Slack attachments,
`{"attachments": "metadata_and_links_only"}` for Calendar,
`{"diffstat": ...}` for a GitHub commit whose diff statistics are unavailable,
and `{"submit_time": "absent for this cluster"}` for a Slurm job whose cluster
reports no `Submit`.
