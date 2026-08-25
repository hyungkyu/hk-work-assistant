# Repository data policy

`RLWRLD/hk-work-assistant` is a source-only repository. Collected or derived data is never committed, regardless of whether it is public, private, or metadata-only.

## Allowed

- Application source code, schemas, migrations, tests, and deployment definitions
- Synthetic fixtures using invented identities and content
- Documentation and public API specifications
- Documentation about formats and behavior that does not contain collected records

## Prohibited

- Slack messages, threads, DMs, reactions, user attribution exports, or downloaded files
- Google Calendar event bodies, attendee lists, meeting notes, or attachments
- Raw API responses, normalized timeline exports, database dumps, and search indexes
- OAuth clients, access tokens, refresh tokens, passwords, private keys, or `.env` files
- Logs or manifests containing message bodies, personal identifiers, private URLs, or tokens
- Public datasets, aggregate exports, collection coverage reports, and operational metadata produced by a run

Collected data remains under `/data/rlwrld-worklog`, outside the Git working tree. Any future proposal to place data in Git requires an explicit policy change agreed with the repository owner before files are staged or uploaded.

The committed `.githooks/pre-commit` hook blocks common data paths, credential files, credential-shaped strings, and files larger than 1 MiB. Enable it with:

```bash
git config core.hooksPath .githooks
```
