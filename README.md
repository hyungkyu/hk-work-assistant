# hk-work-assistant

Local, read-only tooling that collects work context from the services a person
already works in — Slack, Notion, Google Calendar, GitHub, Slurm — keeps the
captured bytes unchanged on disk, and turns them into a queryable timeline plus
a small backoffice for tracking delegated work.

Everything runs on one machine. Nothing here sends collected data anywhere.

## Repository boundary

This is a source-code repository. It may contain application code, deployment
configuration, schemas, tests with synthetic inputs, and Markdown files
explicitly reviewed by the repository owner.

It must not contain collected or derived company data, including messages,
calendar events, reactions, user attribution, run metadata, manifests, exports,
database contents, search indexes, logs, or attachments. Public availability
does not make data eligible for upload. Credentials and local environment files
are also prohibited.

Counts produced by a collection run are derived data and do not belong here
either — not in code, not in comments, not in documentation.

A `pre-commit` hook enforces most of this. It is committed but **not active
until you enable it**:

```bash
git config core.hooksPath .githooks
```

Any future proposal to store data in Git requires a separate discussion and an
explicit policy change before files are staged. See
[docs/data-policy.md](docs/data-policy.md).

## What exists today

**Collection.** Daily incremental, read-only capture with an immutable raw
archive, per-run manifests, and resumable per-source checkpoints. Five sources
are implemented; `worklog daily-collect` runs three of them (Slack, Google
Calendar, Notion), and GitHub and Slurm are separate commands whose output must
be converted and loaded by hand. See
[docs/daily-collection.md](docs/daily-collection.md).

**Ledger.** A standard v1 record format that both live captures and legacy
exports convert into, loaded into PostgreSQL and projected onto a timeline. See
[docs/ledger.md](docs/ledger.md).

**Collection rules.** An append-only registry of the rules a capture was taken
under, with a frozen content digest stamped into every manifest, so a coverage
claim can name the rules that produced it. See
[docs/collection-rules.md](docs/collection-rules.md).

**Coverage dashboard.** Per-source, per-KST-day verdicts derived from manifests
— read-only and rebuildable. See
[docs/collection-status.md](docs/collection-status.md).

**업무 보드 (work board).** Delegated-work tracking shared by the backoffice
page and the `worklog work` CLI, stored as a single JSON file with an
append-only history. See [docs/delegated-work.md](docs/delegated-work.md) and
[docs/work-board-reference.md](docs/work-board-reference.md).

**Board audit.** A half-hourly batch that measures the ways the board has
drifted from the work and writes the count where it can be seen. See
[docs/board-audit.md](docs/board-audit.md).

**Agent coordination.** A directive validator and liveness marks used by the
sessions that do work against the board. Parts of the written protocol are not
implemented; that document says which. See
[docs/cowork-mailbox.md](docs/cowork-mailbox.md).

Search indexing and response drafting are planned, not built. OpenSearch is
declared in `compose.yaml` and read by no code yet.

## Getting started

Requires Python 3.12 or later.

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m pip install pytest        # not a declared dependency
git config core.hooksPath .githooks

.venv/bin/python -m pytest -q
```

Two tests are skipped unless `WORKLOG_TEST_DATABASE_URL` points at a throwaway
PostgreSQL; no other test touches a network or a database. Every collector test
drives a scripted fake API client against a temporary directory.

Running the services, wiring credentials, and applying migrations are covered in
[docs/setup.md](docs/setup.md). Every environment variable the code reads is
listed in [docs/environment.md](docs/environment.md).

## Repository map

```
src/rlwrld_worklog/
  cli.py                 the worklog command and every subcommand
  daily.py               daily-collect: capture -> ledger -> load, per source
  *_collector.py         one per source; each writes through archive.py
  archive.py             the immutable raw archive and the run manifest
  collection_rules.py    the append-only rule registry and its digests
  collection_status.py   coverage verdicts derived from manifests
  ledger/                standard v1 schema, converters, verify, load
  work_store.py          the work board: file, lock, history, validation
  work_cli.py            worklog work ... and the outbox applier
  work_web.py            the board's HTTP API
  admin_store.py         settings, sessions, the agent roster
  admin_web.py           login, session guards, admin API
  cowork.py              directive validation and agent liveness
  web.py                 the app entry point
scripts/                 operational shell scripts (docs/scripts.md)
deploy/systemd/          user timers for the dev -> prod carrier
sql/                     baseline schema and migrations 0001-0004
docs/                    everything above, in detail
```

## Contributing

Read [CONTRIBUTING.md](CONTRIBUTING.md). The short version: the suite must be
green before anything is pushed, no collected data enters this repository, and a
change to a collector's behaviour needs a new entry in the collection-rule
registry rather than an edit to a published one.

Development happens in a cloud container and production runs on one Linux
machine; patches cross between them on a timer. See
[docs/dev-prod-split.md](docs/dev-prod-split.md).
