# Contributing

This document is what a new contributor needs before touching anything. It
assumes no prior context about the project.

## Before your first commit

```bash
git clone <this repo> && cd hk-work-assistant
python3.12 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m pip install pytest
git config core.hooksPath .githooks        # not optional, see below
.venv/bin/python -m pytest -q
```

Python 3.12 is a hard floor (`pyproject.toml`, `requires-python = ">=3.12"`).
`pytest` is deliberately not a declared dependency, so it has to be installed
separately. The package is installed editable, which is why the tests find
`rlwrld_worklog` without any `PYTHONPATH` juggling — if you see instructions
anywhere setting `PYTHONPATH` to `.python-packages`, they are stale.

Everything else — services, credentials, migrations — is in
[docs/setup.md](docs/setup.md), and is not needed to run the suite.

## The one rule that is not negotiable

**No collected data, and nothing derived from it, enters this repository.**

Not messages, not calendar events, not manifests, not exports, not logs, not
database dumps, and not counts produced by a run. "It's already public" is not
an exemption. "It's just a number in a comment" is not an exemption — a coverage
figure in a document is derived data, and one had to be removed from
`docs/ledger.md` for exactly that reason.

`.githooks/pre-commit` blocks most of it: path segments like `secrets/`, `data/`,
`raw/`, `manifests/`, `logs/`; the data-shaped extensions `.jsonl`, `.ndjson`,
`.json.gz`, `.parquet`, `.sqlite`, `.db`, `.dump`, `.sql.gz`, `.log`;
credential-shaped strings (Slack, GitHub and PEM key patterns, and JSON keys
named `refresh_token` / `private_key` / `client_secret`); and any file over
1 MiB. A test that must contain a credential-shaped literal can opt out by
putting `hook-allow: synthetic-credentials` in its first five lines — and only
under `tests/`.

The hook does nothing until you run the `git config` line above. It is not
installed for you, so treat enabling it as part of setup rather than as a nicety.

Full policy: [docs/data-policy.md](docs/data-policy.md).

## How the code is laid out

Read [README.md](README.md#repository-map) for the map. Three things about the
shape are worth knowing before you start editing:

**Collectors never write files directly.** Every source goes through
`archive.py`, which owns the raw archive layout, the manifest, and the rule
stamp. That is what makes a capture describable after the fact. A collector that
writes its own file bypasses the stamp and the coverage dashboard stops being
able to say what the file was captured under.

**Behaviour changes to a collector need a new rule version.** `collection_rules.py`
is an append-only registry with frozen content digests. If your change alters
what gets captured, `tests/test_collection_rules.py` will fail — that failure is
the signal to append a new version, never to edit a published one. See
[docs/collection-rules.md](docs/collection-rules.md).

**The work board is a file, not a database.** `work_store.py` holds one JSON
document under a lock, with an append-only `history.jsonl` written *before* the
change it records. Its validation is layered on purpose and is documented in
[docs/work-board-reference.md](docs/work-board-reference.md). Adding a field
means touching the allowlist, the shape check, the whole-file integrity check
and the history — not just the dataclass.

## Tests

```bash
.venv/bin/python -m pytest -q
```

No test reaches the network. Collector tests drive scripted fake API clients
against `tmp_path`. Two tests skip unless `WORKLOG_TEST_DATABASE_URL` points at
a throwaway PostgreSQL; `compose.ledger-test.yaml` exists to provide one:

```bash
docker compose -f compose.ledger-test.yaml up -d
WORKLOG_TEST_DATABASE_URL=postgresql://worklog_test:disposable-only@127.0.0.1:55432/worklog_test \
  .venv/bin/python -m pytest -q
```

A test is expected to fail for a reason you can name. When you fix a defect,
add the test that goes red without the fix, and say in the commit message which
mutation it catches. Several tests in this repository exist because a claim was
made that turned out not to be true; that is the standard to hold.

## Commit messages

Look at `git log`. The subject line says what the change makes possible or
prevents, in the imperative, as a sentence rather than a category:

```
Write the history before the change it records
Stop a date slice from voting on the days it never looked at
Have the board say what it is not showing
```

Not `fix: history bug`, not `refactor work_store`. The body explains *why* —
what was wrong before, what evidence showed it, what the change closes. If the
change came out of an incident, name the incident and the date; several commits
here do, and that is why the reasoning survives.

Wrap the body at 72 columns. No trailing issue-tracker noise.

## What needs a human decision

Some changes are not a matter of taste and should not be made unilaterally:

- Anything that would put data in this repository.
- Editing a published collection-rule version, rather than appending one.
- Widening what the outbox queue may write to the board. The current limits
  exist so the party that requests work cannot also write the report of it.
- Granting an unattended session a broader tool allowlist.
- `git push`, `sudo`, deletion, and changes to policy or security controls.
  `cowork.py` names these as never-autonomous, and there is a live conflict
  between that rule and what the wake timer currently permits — see
  [docs/cowork-mailbox.md](docs/cowork-mailbox.md).

## Development and production are different machines

Development happens in a cloud container with a small amount of test data.
Production is one Linux machine holding the real archive, and it is the only
side allowed to push. Commits cross as patch files applied by a timer, which
runs the suite and pushes only if it is green.

The full mechanism, including every state the carrier can report and what each
one leaves behind, is in [docs/dev-prod-split.md](docs/dev-prod-split.md).

## Known gaps

These are open, documented, and not traps for you to rediscover:

- The CLI path has no authorization. Everything that returns 403 over HTTP
  passes on the command line, including issuing another agent's board session.
  See [docs/work-board-reference.md](docs/work-board-reference.md).
- `/collection/*` returns 403 to agent sessions, so an agent cannot verify the
  coverage dashboard it is asked to fix.
- A plain `--dry-run` collection currently paints a day as `collected` on the
  dashboard although it committed nothing. See
  [docs/collection-status.md](docs/collection-status.md).
- Parts of the cowork mailbox protocol are specified but not implemented. The
  document labels which.
