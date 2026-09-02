# Standard v1 ledger

The ledger is the system of record for **historical observations** of Slack,
Notion, and Google Calendar objects. Live API collection writes the
**current head**; converted legacy files never overwrite it.

## Pipeline

Two inputs, one ledger, one loader:

```
official API ──capture──▶ raw archive ──convert──▶ ledger JSONL ──load──▶ PostgreSQL
                          (immutable)              (staging disk)        ledger_records
legacy daily_raw ────────────convert──────────────▶                      + service projection
   (read-only)
```

Each stage is independently re-runnable and independently verifiable. The live
path is `worklog daily-collect`, documented in
[daily-collection.md](daily-collection.md); this file covers the ledger format
that both paths write.

## Entity types

| Kind | Types | Projected onto the timeline |
|---|---|---|
| activity | `message`, `page`, `block`, `comment`, `event` | yes, except `block` |
| dimension | `user`, `usergroup`, `conversation`, `calendar`, `data_source` | no |

Dimension entities were added for the live path
(`sql/migrations/0003_live_capture.sql`, a widened CHECK that still accepts
every earlier type). Rule 2 requires the service database to be rebuildable
from ledger data, and a message row without its channel and its author is not
rebuildable. They stay out of `timeline_events` because a channel is not an
activity.

## Field set

Codex's standard v1 (17 fields) plus five approved ledger extensions:
`observation_window`, `capture_completeness`, `supplement_provenance`,
`visibility_routing`, `denormalized_label_snapshot`.

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
   thread-store supplement at 10. A supplement fills gaps without replacing a
   primary capture, and a legacy re-run can never demote a live head.
8. Every row carries `source_file` + `source_file_sha256` + `record_pointer`,
   all `NOT NULL`, so any value is traceable back to an exact byte range of an
   exact legacy file.

## Identity and duplicates

`ledger_id = uuid5(source, entity_type, tenant, scope_key, entity_id,
observation_window.start, content_hash)`.

* The **observation window** is part of identity because legacy files are day
  slices. The same object on two days is two observations, not one row.
* `content_hash` collapses byte-identical copies automatically.
* `scope_key` participates only when it is part of the entity's natural key:
  a Slack message belongs to a channel, a Notion block belongs to a page. A
  Notion **page** is identified by its page id alone — which database query
  surfaced it is scope, not identity.

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

```bash
export PYTHONPATH="$PWD/.python-packages:$PWD/src"
LEGACY=/data/rlwrld-worklog/legacy/claude/weekly
LEDGER=/data/rlwrld-worklog/staging/ledger

# 1. schema
python3 -m rlwrld_worklog ledger-schema --output /tmp/ledger-v1.schema.json

# 2. convert (dry-run first; writes nothing)
python3 -m rlwrld_worklog ledger-convert slack  --legacy-root $LEGACY --out-root $LEDGER --dry-run
python3 -m rlwrld_worklog ledger-convert slack  --legacy-root $LEGACY --out-root $LEDGER
python3 -m rlwrld_worklog ledger-convert notion --legacy-root $LEGACY --out-root $LEDGER --salvage-comments

# 3. migrate (dry-run by default; --apply to commit)
python3 -m rlwrld_worklog ledger-migrate --database-url "$DATABASE_URL"
python3 -m rlwrld_worklog ledger-migrate --database-url "$DATABASE_URL" --apply

# 4. load (dry-run by default; runs the real transaction then rolls back)
python3 -m rlwrld_worklog ledger-load slack --ledger-root $LEDGER --database-url "$DATABASE_URL"
python3 -m rlwrld_worklog ledger-load slack --ledger-root $LEDGER --database-url "$DATABASE_URL" --apply

# 5. verify
python3 -m rlwrld_worklog ledger-verify slack --ledger-root $LEDGER --legacy-root $LEGACY \
    --database-url "$DATABASE_URL" --report $LEDGER/_verify/slack.json
```

Optional flags worth knowing:

* `--root hk_private` adds the owner-only tier, which is **not** included by
  default. It holds 18,502 further Slack thread messages.
* `--salvage-comments` (Notion) lifts comment objects out of attribution
  files. Notion comments exist nowhere else. Every salvaged row is marked
  `source_file_kind="attribution"` and carries its own capture profile, so the
  decision is reversible with a single provenance filter.

## Re-running

Conversion is deterministic: the same legacy input produces byte-identical
JSONL, so re-running and diffing is meaningful. Loading is idempotent through
`ledger_id` plus `ON CONFLICT DO UPDATE`, and skips files whose sha256 already
appears in `ledger_batches` unless `--reload-unchanged` is given.

## Migrations

`sql/migrations/0002_ledger_v1.sql` assumes the baseline `sql/schema.sql` has
been applied: it takes foreign keys on `people` and fixes the baseline
`timeline_events` source CHECK, which omits `notion`.

## Live capture runs

`worklog daily-collect` runs the conversion itself; `ledger-live-convert`
re-runs it for one already-archived run without touching an API:

```bash
python3 -m rlwrld_worklog ledger-live-convert slack \
    --archive-root $ARCHIVE \
    --manifest $ARCHIVE/manifests/slack/production/<run id>.json \
    --out-root $LEDGER
```

Output is `ledger/<source>/live-<run id>.jsonl`, one file per capture run
rather than one per observation day. The loader takes the observation date
from the rows when the filename is not a date, so `ledger_batches` still gets
a real `observation_date`.

The observation window of a live record is the **capture day**, not the age of
the object: re-observing an unchanged message tomorrow is a second
observation, exactly as it is for a legacy day slice.

Every archived page is verified against its manifest SHA-256 before it is
parsed. Conversion aborts on a mismatch rather than emitting records or
replacing an existing ledger file. Only pages the converter actually reads are
verified: a Notion `/search` listing or paginated `property-*` page is
archived for the record but contributes no ledger row, so its bytes are not
re-hashed here.

Coverage the collector could not obtain is carried into every record rather
than dropped: `coverage.permission_gap` counts the run's skips by kind,
`capture_completeness.truncated` and `rate_limit_hits` come from the run
manifest, and `capture_completeness.lossy_fields` marks Slack files and
Calendar attachments as metadata-and-links only.
