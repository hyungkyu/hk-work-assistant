#!/usr/bin/env bash
# Read the roster, build yesterday's digests, render the org chart.
#
# Runs after the nightly collection rather than before it: a digest of a day
# whose collection has not finished is a digest that is wrong and that nothing
# will ever correct. The collection starts at 06:00 KST and has been taking
# around two and a half hours, so this fires at 09:30.
#
# Order matters and is not arbitrary:
#
#   1. roster sync   -- who exists, and which accounts are theirs. A digest
#                       built before this attributes yesterday's work to the
#                       org chart of the day before, and a person who joined
#                       yesterday has no rows at all.
#   2. digest        -- yesterday, KST. Never today: today is not over.
#   3. org chart     -- rendered from the observation step 1 just wrote.
#
# Every step is `--apply`, and every step is a command in this file rather
# than something a person types. That is the protocol: the batch runs whether
# or not anyone is watching, and what it did is in the log.

set -uo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$root" || exit 78

config_root="${APP_CONFIG_ROOT:-$HOME/.config/hk-work-assistant}"
out_root="${WORKLOG_DIGEST_OUT:-/data/rlwrld-worklog/digest}"

if [ -x .venv/bin/worklog ]; then
  worklog=.venv/bin/worklog
elif command -v worklog >/dev/null 2>&1; then
  worklog=$(command -v worklog)
else
  echo "no worklog executable found" >&2
  exit 78
fi

# The database URL lives outside the repository, in the same file the
# collection unit reads. Absence is survivable for the caller to report, not
# a reason for this script to invent one.
if [ -f "$config_root/collect.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$config_root/collect.env"
  set +a
fi

if [ -z "${DATABASE_URL:-}" ]; then
  echo "DATABASE_URL is not set; nothing was written" >&2
  exit 78
fi

# A dependency the batch needs and does not have is a failure that says what
# fixes it, once, here -- rather than a traceback three steps later.
if ! "$worklog" org --help >/dev/null 2>&1; then
  echo "the worklog CLI at $worklog cannot run; check the virtualenv" >&2
  exit 78
fi
if ! python3 -c "import openpyxl" >/dev/null 2>&1 \
   && ! "$(dirname "$worklog")/python" -c "import openpyxl" >/dev/null 2>&1; then
  echo "openpyxl is missing from the environment that runs this batch." >&2
  echo "install it: $(dirname "$worklog")/pip install openpyxl" >&2
  exit 78
fi

status=0

echo "=== roster sync"
"$worklog" org sync --apply || status=1

echo
echo "=== timeline projection (records the loader has not projected)"
# Cheap when there is nothing to do, and the thing that makes a change to
# what gets projected heal itself instead of waiting for somebody to
# remember. On 2026-09-11 that gap was 13,266 records.
"$worklog" timeline-project --apply || status=1

echo
echo "=== search corpus (documents the loader has not indexed)"
"$worklog" search-index --apply || status=1

echo
echo "=== unmapped accounts"
# After the roster, because an account the sheet has just claimed should
# close itself rather than be asked about; before the digest, because the
# digest's attribution depends on the identities this may have closed.
"$worklog" org unmapped --apply || status=1

echo
echo "=== slack thread sweep (replies whose parent predates every window)"
# The 622 orphaned replies, a bounded slice per night. They are replies that
# exist in the ledger with no parent row, so the situation they answer is
# missing -- which matters twice over now that HK wants an agent trained on
# (what people said -> what he asked). One-time by nature: a parent this
# recovers stops being an orphan, so the number falls to zero and the step
# becomes a no-op that costs one query.
#
# Capped because this is network-heavy and unattended. 150 threads a night
# clears the backlog inside a week without a run long enough to collide with
# the collection that follows it.
sweep_max="${WORKLOG_SWEEP_MAX:-150}"
if [ "$sweep_max" != "0" ]; then
  sweep_out=$("$worklog" slack-thread-sweep --apply --max-parents "$sweep_max" 2>&1)     || status=1
  printf '%s\n' "$sweep_out"
  # Left where the cloud side can read it without anyone relaying a terminal.
  mkdir -p incoming
  SWEEP="$sweep_out" python3 - > incoming/last-sweep.json <<'SWEEPSTATE' || true
import json, os, datetime
output = os.environ.get("SWEEP", "")
found = {
    "finished_at": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
    "tail": output.splitlines()[-12:],
}
for line in reversed(output.splitlines()):
    if line.startswith("slack_thread_sweep="):
        try:
            found["summary"] = json.loads(line.split("=", 1)[1])
        except ValueError:
            pass
        break
print(json.dumps(found, ensure_ascii=False, indent=2))
SWEEPSTATE
fi

echo
echo "=== digest (yesterday, and any gap in the last week)"
# `--catch-up` rather than yesterday alone: a night the machine was off, or a
# run that died, would otherwise leave a hole that only a person typing a
# backfill command could close -- and a batch that needs a person is not a
# batch. Bounded to a week so a long outage catches up over several nights
# instead of timing out in one.
"$worklog" digest --catch-up 7 --apply || status=1

echo
echo "=== org chart"
mkdir -p "$out_root"
"$worklog" org chart --html "$out_root/org-chart.html" || status=1

echo
echo "=== digest coverage"
"$worklog" digest --status || true

exit "$status"
