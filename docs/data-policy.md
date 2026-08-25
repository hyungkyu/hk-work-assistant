# Repository data policy

`RLWRLD/hk-works` contains source code and non-sensitive operational metadata only.

## Allowed

- Application source code, schemas, migrations, tests, and deployment definitions
- Synthetic fixtures using invented identities and content
- Documentation and public API specifications
- Non-sensitive metadata such as schema versions, aggregate counts, and source coverage dates
- Public datasets only after an explicit, file-by-file review

## Prohibited

- Slack messages, threads, DMs, reactions, user attribution exports, or downloaded files
- Google Calendar event bodies, attendee lists, meeting notes, or attachments
- Raw API responses, normalized timeline exports, database dumps, and search indexes
- OAuth clients, access tokens, refresh tokens, passwords, private keys, or `.env` files
- Logs or manifests containing message bodies, personal identifiers, private URLs, or tokens

Collected data remains under `/data/rlwrld-worklog`, outside the Git working tree. Public data is not automatically safe to commit; it must be deliberately selected and reviewed first.

The committed `.githooks/pre-commit` hook blocks common data paths, credential files, credential-shaped strings, and files larger than 1 MiB. Enable it with:

```bash
git config core.hooksPath .githooks
```
