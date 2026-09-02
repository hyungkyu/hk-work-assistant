# hk-work-assistant

Local, read-only tooling for collecting and organizing work context from services such as Slack and Google Calendar.

## Repository boundary

This is a source-code repository. It may contain application code, deployment configuration, schemas, tests with synthetic inputs, and Markdown files explicitly reviewed by the repository owner.

It must not contain collected or derived company data, including messages, calendar events, reactions, user attribution, run metadata, manifests, exports, database contents, search indexes, logs, or attachments. Public availability does not make data eligible for upload. Credentials and local environment files are also prohibited.

Any future proposal to store data in Git requires a separate discussion and an explicit policy change before files are staged.

## Current scope

- Deterministic timeline normalization
- Daily incremental read-only collection from Slack, Notion, and Google Calendar, with an
  immutable raw archive, per-run manifests, and resumable per-source checkpoints
  (`worklog daily-collect`, see [docs/daily-collection.md](docs/daily-collection.md))
- Standard v1 ledger conversion and loading, for both live captures and legacy files
  (see [docs/ledger.md](docs/ledger.md))
- Delegated-work tracking shared by the backoffice `업무 현황` page and the local
  `worklog work` CLI, stored under `APP_CONFIG_ROOT`
  (see [docs/delegated-work.md](docs/delegated-work.md))
- Collection status in the backoffice `수집 현황` page: running, completed and failed
  runs per source, KST date and weekday coverage, and an append-only registry of the
  collection rules (`V0` legacy dumps, `V1` official-API raw ledger) with a stable
  content digest per version. Every new manifest records the rule version and digest
  it was captured under; the dashboard is derived, read-only, and rebuildable
- Read-only mirroring of legacy Slack and Google Calendar JSON to local storage
- PostgreSQL and OpenSearch development services through Docker Compose
- Local web API

Search indexing and response-drafting features are planned work.

## Development

Requires Python 3.12 or later.

```bash
PYTHONPATH="$PWD/.python-packages:$PWD/src" python3 -m pytest -q
docker compose config
```

No test reaches the network: every collector test drives a scripted fake API client
against a temporary directory.

Runtime credentials are stored outside Git. Collected data is stored outside the working tree under `/data/rlwrld-worklog`.
