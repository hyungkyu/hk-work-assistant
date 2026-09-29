# Slack V9 one-day comparison

Window: 2026-09-03 00:00–2026-09-04 00:00 Asia/Seoul. Both captures were
isolated under `/tmp`, used the read-only Slack API, and did not load a
database. No message text, channel name, user name, or other private source
content is included here.

## Decision

V9 has a material coverage benefit for this day and should replace the V8
slice behavior. It retained every baseline message and recovered 170 additional
in-window messages: 905 versus 735 unique message records, a 23.16% gain.

The gain costs about twice the API calls (1,624 versus 832) and 3.22 times the
wall time (1,374.7 versus 427.5 seconds). This is appropriate for historical
backfill, but the pre-window pass should not be added to ordinary incremental
runs; the implementation runs it only when an exclusive `until` is present.

## Bounded reach

- Maximum parent lookback: 90 days before the slice.
- Maximum discovery pages: 1 per channel.
- Channels enumerated: 697; discovery pages read: 696.
- Oldest parent-discovery observation: 2026-06-04T19:14:54.268159Z.
- Candidate parents considered: 103.
- One inaccessible DM was skipped in both captures; V9 records the same DM a
  second time for the discovery phase. No schema error or truncation occurred.

Slack returns an old thread parent as the first row of
`conversations.replies` even when `oldest` is newer. The first candidate run
made that visible. The collector now excludes any such row outside the slice,
and the ledger converter independently enforces the requested window. The
reported 170-message gain is after that filter; the old parent rows are not
counted.

## Evidence

- V8-behavior baseline run (discovery page budget 0):
  `20260909T065121Z-cfe63484fd`
- V9 candidate run (90 days, 1 page/channel):
  `20260909T065840Z-7e7288c0fb`
- Baseline ledger sha256:
  `33d68f1a4962599410410afcb2c8e42526935a40b0958b5b1a7fe00e1a2d0861`
- Candidate ledger sha256:
  `3688c6011da958d7d35c744f504375950e591d8baa7ca3ba3eea2bf87ace7c2b`
- Raw integrity: 831 baseline and 1,622 candidate files checked; no missing,
  mismatched, or unsafe path.
- Regression suite: 1,067 passed, 9 skipped; `git diff --check` and compileall
  passed.

The baseline manifest carries a V9 registry stamp because the comparison was
run after V9 activation, but its parent-discovery page budget was explicitly
zero. Its behavior is therefore the V8 date-slice path; this distinction is
recorded here rather than inferred from the stamp.
