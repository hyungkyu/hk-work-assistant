# Security and data handling

## Source-only policy

Do not commit collected or derived data. This includes Slack content, Google Calendar content, user or reaction attribution, raw API responses, manifests, checkpoints, exports, logs, database dumps, search indexes, and operational metadata. The rule applies even when the underlying data is public.

Only synthetic test inputs may be committed. They must use invented identities, identifiers, URLs, and content.

## Credentials

Do not commit OAuth clients, access or refresh tokens, passwords, private keys, cookies, `.env` files, or credential-bearing logs. Local credentials must use the minimum required read-only scopes and remain outside the working tree.

If a credential may have entered Git history, stop distribution and revoke or rotate it before rewriting history.

## Markdown review

Markdown files are included only after explicit review by the repository owner. A documentation change must be reviewed with the same care as source code because it can expose internal paths, identifiers, operating details, or collected metadata.
