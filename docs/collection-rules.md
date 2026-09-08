# Collection rules

`src/rlwrld_worklog/collection_rules.py` is an append-only registry of the
rules a collection run was captured under. Every run manifest carries a
three-field stamp naming the rule it followed, so a later reader never has to
guess what "collected" meant on the day the run happened.

## Why the registry exists

A manifest says *what* a run fetched. It does not say *what the run was
supposed to fetch*, and that changes over time: the legacy dumps and the
current official-API archive have different scopes, different densities and
different blind spots. Without a versioned rule, a coverage dashboard silently
compares two incomparable things.

Nothing in this module is imported from the collectors at runtime. A rule has
to keep describing what a *past* run did even after the collector changes, so
every statement in it is a literal fact written down at publication time, with
its evidence named.

## What a rule version is

A `CollectionRule` (`collection_rules.py:158`) is one frozen statement covering
every source collected at that time:

| Field | Meaning |
|---|---|
| `version` | `V0` … `V9`. The string written into manifests. |
| `title`, `summary` | What changed and why, in prose. |
| `status` | `pending`, `active`, or `superseded`. |
| `effective` | `EffectivePeriod(start, end, basis)`. `end` is **always** `None` in the registry. |
| `manifest_schema_version`, `ledger_schema_version`, `source_schema_version` | The schema versions a run under this rule writes. Literal, not imported. |
| `capture_profiles` | The capture profiles a run under this rule stamps. |
| `sources` | One `SourceRule` per source. |
| `storage_layout`, `unknowns`, `supersedes` | Layout facts, facts the rule cannot assert, and its predecessor. |

Each `SourceRule` (`collection_rules.py:106`) carries `scope`, `density`, a
machine-readable `density_kind` (`day_slice`, `incremental_continuous`,
`incremental_or_date_slice`, `full`, `unknown`), `includes`, `excludes`,
`known_limitations`, `evidence` (a path, a file or a code location that proves
the statements) and `unknowns`.

A version's **end** is never stored. It is derived from its successor's start
by `effective_window` (`collection_rules.py:1410`), because writing an end into
a rule would mean editing published, digest-frozen content after the fact.
`_validate_registry` rejects any rule that stores one.

## The frozen digest

`CollectionRule.digest` (`collection_rules.py:202`) is
`sha256` over the rule's canonical JSON. `PUBLISHED_DIGESTS`
(`collection_rules.py:1459`) pins the digest of every already-published
version, and `_validate_registry` runs at import: editing a published rule
changes its digest, trips the check and raises `RuleRegistryError` before
anything can run.

Two details of what the digest covers:

* **`status` is deliberately outside the digest** (`collection_rules.py:178`).
  Retiring a version — which any registry with more than one version must do —
  would otherwise change its digest and make a required transition look like
  tampering. The frozen thing is what the rule says about collection, not where
  it sits in the lifecycle.
* **`HISTORICAL_DIGESTS`** (`collection_rules.py:1404`) keeps the values `V0`
  and `V1` carried under registry schema 1, when `status` was still hashed.
  Manifests written then recorded those values and must keep verifying;
  `digest_is_recognised` (`:1446`) accepts either. The rule content did not
  change, only the digest definition did.

## The pending lifecycle

`RULE_STATUSES` is `("pending", "active", "superseded")`.

`pending` exists because publishing a rule and running under it are two
different days. A repair to a collector cannot land while the active rule
describes the behaviour being repaired — the rule would keep asserting what the
code no longer does — but activating the new rule first makes every run in
between stamp a rule it does not follow.

A pending version is published, readable and digest-checkable, and names the
coverage notes the repair will record, so the repair has something to land
against. It stamps nothing, and it does not close its predecessor's window.

A pending version is always the **tip** of the registry: it supersedes the
version in force and never sits beneath one. That is the ordering, not an
accident of who committed first — the version describing what the collector
*does* is the version being stamped, and a change that has already landed never
queues behind an unlanded one for nothing but a version number. See step 6 of
[the workflow](#when-teststest_collection_rulespy-fails) for what to do when
both kinds of change are in flight at once.

`_validate_registry` enforces two things about a pending rule
(`collection_rules.py:1484-1498`):

* it must not store an `effective.start` — a version starts on the day a run
  first follows it, which is not knowable until the collector does;
* it must not appear in `PUBLISHED_DIGESTS` — a rule is frozen when it takes
  effect, not before.

Activating a pending rule is therefore one change that does three things
together: flip `status` to `active`, write in `effective.start`, and pin the
digest.

## The stamp in every manifest

`active_rule_stamp()` (`collection_rules.py:1578`) returns the three fields
`RawArchive.finish` writes into every manifest:

```
collection_rule_version          e.g. "V8"
collection_rule_digest           e.g. "sha256:09b45864..."
collection_rule_schema_version   RULE_REGISTRY_SCHEMA_VERSION, currently 2
```

The archive applies them **after** the caller's details
(`src/rlwrld_worklog/archive.py:248-250`), so a collector cannot overwrite or
omit them, and they are identical on success, dry-run and failure manifests.

A run stamps the rule that was active when it *started*. That is the honest
record even when the rule says nothing about the source being captured — the
August GitHub runs stamped `V2`, and `V2` does not name GitHub. Such a manifest
is not wrong and must never be rewritten; the dashboard reports
`declares_source: false` instead (`collection_status.py:355-360`).

## Current state

```
RULES              = (V0, V1, V2, V3, V4, V5, V6, V7, V8, V9)
ACTIVE_RULE_VERSION = "V8"
```

`V8` is **active**, effective from 2026-09-08. It is `V7` with the Notion
document set stated rather than inherited from whatever the block walk reached.
A day's documents are what `/search` listed inside the window, plus the rows
each data source in that window reports as edited, plus the operator's seed
pages that changed; the block walk stops at `child_page` and `child_database`
instead of descending through them.

The descent it removes was not coverage. Measured on two collected days, the
blocks it reached under pages `/search` had not listed were 0% and 2.2%
in-window while costing a third to two thirds of the run's block requests, and
what it did find in-window sat under pages search *had* listed and the run
walks anyway. Every run now records `counters.child_object_refs_unlisted` —
child objects it declined to enter that no discovery pass had named — so a
search that starts leaking is visible from inside a run rather than inferred
from a gap months later.

`V7` is superseded, and its window closes at `V8`'s start.

`V9` is **pending**, not active. It is a Slack slice that recovers replies whose
thread parent predates the window, by reading backwards from the window's start
and by re-polling watched threads inside the window instead of skipping the
re-poll. `V6` described those skips as deliberate; measurement showed they are
why a month-by-month backfill silently misses replies to older threads. That
collector change has **not** landed — see [the date-slice section of
daily-collection.md](daily-collection.md#date-slice-capture) for what the
collector does today.

Because `V9` is pending, it has no `effective.start` and no pinned digest, and
it does not close `V8`'s window.

`V9` has been renumbered twice. It was published as `V7` while it was the only
unlanded change in flight, moved to `V8` when the Notion mention and comment
repair landed, and to `V9` when the Notion document-set repair landed. A
pending version is the tip of the registry, never a rule a landed change queues
behind: a stamp that does not describe the run is worth less than no stamp. The
renumbering costs nothing recoverable, because a pending version stamps no
manifest and cannot pin a digest (`_validate_registry` forbids it), so no run
and no `PUBLISHED_DIGESTS` entry ever carried an earlier number for that rule.
`V9`'s own summary records the moves, so the number is not made to look as if
it were always this one.

## Invariants `_validate_registry` enforces

Checked at import (`collection_rules.py:1471-1550`), so a broken registry fails
the moment the module loads:

1. No version is declared twice. The registry is append-only.
2. Every `status` is one of `RULE_STATUSES`.
3. Exactly one rule is `active`, and it is `ACTIVE_RULE_VERSION`.
4. A `pending` rule stores no `effective.start` and has no pinned digest.
5. No rule stores an `effective.end`.
6. Every source a rule names is in `SOURCES`, and no rule defines a source
   twice or defines no source at all.
7. **The active rule must cover every source in `SOURCES`.** A retired version
   defines only the sources its collector knew about — demanding otherwise
   would back-date a source into rules written before that collector existed —
   but the version in force must account for everything collected now.
8. No published digest has drifted from the rule's current content.

Invariant 7 is two-sided with the source list: `SOURCES` and the active rule
move together, in one commit. Widening one without the other leaves the
registry invalid in between, which is deliberate — a source the list claims but
no live rule describes would be a source the dashboard reports with no
statement of what was collected.

## When `tests/test_collection_rules.py` fails

That test asserts the current collectors have not grown a limitation the
registry does not name. **A failure is the signal to append a new version, not
to edit a published one.**

The workflow:

1. Decide whether the change is already described by the active rule. If the
   collector's scope, density, includes, excludes or known limitations moved,
   it is not.
2. Append a new `CollectionRule` at the end of the version list, built from the
   previous one — `V6` reuses `V5.capture_profiles`, `V5.storage_layout` and
   `V5.unknowns`, and reuses unchanged `SourceRule` values via
   `V5.source_rule("slurm")`. Set `supersedes` to the predecessor.
3. Add it to `RULES` (`collection_rules.py:1394`).
4. If the collector change has already landed, set `status="active"`, write the
   `effective.start`, move `ACTIVE_RULE_VERSION`, and pin the new digest in
   `PUBLISHED_DIGESTS`. Flip the predecessor to `superseded`.
5. If the collector change has **not** landed yet, publish it `pending`: no
   `effective.start`, no pinned digest, `ACTIVE_RULE_VERSION` unchanged.
   Activate it in the same change that lands the collector repair.
6. **A landed change gets an active version; a pending version sits on top of
   it as the tip.** These two orderings are not interchangeable, and the rule
   is not "whichever version was written first keeps its number":
   * The version that describes what the collector *does* is the one in force.
     A stamp that does not describe the run is worth less than no stamp at all,
     because it is believed — the whole registry exists so a later reader never
     has to guess what "collected" meant on the day.
   * A landed change must never wait on an unlanded one for nothing but version
     numbering. If a pending version already sits at the tip and it describes
     different work, the landed change takes the next active number and the
     pending version is **renumbered above it**, keeping `supersedes` pointed
     at the new active version.
   * Renumbering a pending version destroys no record — it has stamped no
     manifest and, by invariant 4, has pinned no digest — but check anything
     keyed by the version string before doing it, and carry over any
     `PUBLISHED_DIGESTS` / `HISTORICAL_DIGESTS` entry rather than dropping it.
     If the version was ever published under its old number, say so in its own
     text instead of letting the new number look original.
7. Adding a new source means adding it to `SOURCES`, `COLLECTOR_SOURCES`,
   `SOURCE_TO_COLLECTOR` and `SOURCE_LABELS`, and giving the active rule a
   `SourceRule` for it, all in one commit.

Never edit a published rule's content to make a test pass. `PUBLISHED_DIGESTS`
exists precisely to turn that into a loud import-time failure rather than a
quiet re-labelling of historical runs.

To find the digest of a rule you just wrote, read it off the
`RuleRegistryError` the import raises, or print
`rule_for_version("V9").digest`.

## Reading a rule back

`registry_as_dict()` (`collection_rules.py:1588`) returns the whole registry
for the backoffice, attaching each rule's derived `effective_window` outside
its frozen body — a published rule's own prose was written before it had a
successor and can still say it is current; the derived window is what the
registry knows now.

`stamp_from_manifest()` (`:1474`) reads a manifest's stamp back, and
`classify_rule()` in `collection_status.py:327` decides how a run is attributed
to a rule. See
[collection-status.md](collection-status.md#rule-attribution).
