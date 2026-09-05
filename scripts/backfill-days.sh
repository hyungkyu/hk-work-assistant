#!/usr/bin/env bash
# Collect a range of KST days, one `daily-collect` run per day.
#
#   scripts/backfill-days.sh <first KST date> <last KST date> [--source X]...
#   scripts/backfill-days.sh 2026-09-01 2026-09-04
#   scripts/backfill-days.sh 2026-09-01 2026-09-04 --source slack --source notion
#
# Each day is one run bounded by that day:
#
#   daily-collect --since <day>T00:00+09:00 --until <next day>T00:00+09:00
#
# and each run goes through `run-logged.sh`, so its output lands under the data
# disk where the cloud side can read it.
#
# Why one run per day. A `daily-collect` run advances no checkpoint until it
# ends, so a multi-day run banks nothing until it finishes: on 2026-09-05 a
# five-day catch-up started as a single `--since 5d` was still going two hours
# later, and a failure at that point would have lost all four finished days
# together. Sliced by day, a failure costs one day and every day before it is
# already banked -- which is how August was backfilled, one KST day per run.
#
# This script is resumable and stops at the first day that failed. It does not
# skip past a failure: continuing would bury a gap under later successes, and a
# gap nobody can see is the failure the collection subsystem exists to prevent.
#
# State, under the log root beside the logs themselves:
#
#   <log root>/backfill-days/days.log      one line per finished attempt
#   <log root>/backfill-days/summary.json  succeeded / failed / remaining
#
# Both are written after each day rather than at the end, so a run killed part
# way through leaves the same record a run that finished does.

set -uo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

LOG_ROOT=${WORKLOG_LOG_ROOT:-/data/rlwrld-worklog/logs}

# The command each day runs, overridable so the day list, the refusals and the
# resume can be tested without a network. It is split on whitespace, as
# `run-logged.sh` receives it.
COMMAND=${WORKLOG_BACKFILL_COMMAND:-$root/.venv/bin/worklog daily-collect}
RUNNER=${WORKLOG_BACKFILL_RUNNER:-$root/scripts/run-logged.sh}

# Google Calendar is deliberately absent. Every run here carries `--until`, and
# Calendar's incremental read is a per-calendar sync token: an upper bound
# cannot be expressed for it, only ignored, so `daily-collect` refuses the run
# outright rather than collecting the live head and filing it under the
# requested day. Naming it here would make the first day of every backfill fail
# on a refusal that was knowable before the range was typed.
DEFAULT_SOURCES=(slack notion github slurm)

usage() {
  cat >&2 <<'USAGE'
usage: scripts/backfill-days.sh <first KST date> <last KST date> [--source X]...

One `daily-collect` run per KST day, in order, each bounded by that day, each
logged under the data disk. Resumable: a re-run skips days already recorded as
succeeded. Stops at the first day that fails rather than burying the gap.

  --source X   repeatable. Default: slack notion github slurm.
               google-calendar cannot be backfilled this way -- a bounded
               window cannot be expressed for a per-calendar sync token, so
               `daily-collect --until` refuses it.

Both dates are KST calendar dates (YYYY-MM-DD), inclusive. The range must be
ordered and must be over: a range reaching into today would file a day that has
not happened yet as a whole collected day.

Environment:
  WORKLOG_LOG_ROOT           where logs and state go (default /data/rlwrld-worklog/logs)
  WORKLOG_BACKFILL_COMMAND   the per-day command (default .venv/bin/worklog daily-collect)
  WORKLOG_BACKFILL_RUNNER    the logging wrapper (default scripts/run-logged.sh)

Exit codes: 0 every day collected, 1 a day failed, 64 the request was refused.
USAGE
}

first=${1:-}
last=${2:-}
if [ -z "$first" ] || [ -z "$last" ]; then
  usage
  exit 64
fi
shift 2

sources=()
while [ $# -gt 0 ]; do
  case "$1" in
    --source)
      [ $# -ge 2 ] || { echo "--source needs a value" >&2; exit 64; }
      sources+=("$2")
      shift 2
      ;;
    --source=*)
      sources+=("${1#--source=}")
      shift
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      echo "unexpected argument: $1" >&2
      usage
      exit 64
      ;;
  esac
done
[ ${#sources[@]} -gt 0 ] || sources=("${DEFAULT_SOURCES[@]}")

for source in "${sources[@]}"; do
  if [ "$source" = "google-calendar" ]; then
    echo "google-calendar cannot be backfilled by day: every run here carries --until," \
         "and a per-calendar sync token cannot express an upper bound, so daily-collect" \
         "refuses it. Collect it with the nightly incremental run." >&2
    exit 64
  fi
done

# The day list, and every refusal about the range, in one place. Refusing here
# means no state directory is created and no day is attempted for a request
# that was never going to work.
days=$(FIRST="$first" LAST="$last" python3 - <<'DAYS'
import os
import sys
from datetime import date, datetime, time, timedelta, timezone

KST = timezone(timedelta(hours=9))

try:
    first = date.fromisoformat(os.environ["FIRST"])
    last = date.fromisoformat(os.environ["LAST"])
except ValueError as error:
    sys.exit(f"both dates must be KST calendar dates as YYYY-MM-DD: {error}")

if last < first:
    sys.exit(
        f"{first} is after {last}: the range runs first day to last day, and reversing it "
        "silently would collect a different range than the one asked for"
    )

# The range's exclusive upper bound is midnight KST after the last day. A bound
# that has not passed yet means the last day is not over: collecting it would
# file a partial day as a whole collected one, and nothing afterwards could tell
# the difference.
bound = datetime.combine(last + timedelta(days=1), time.min, tzinfo=KST)
now = datetime.now(timezone.utc)
if bound > now:
    sys.exit(
        f"{last} is not over yet (it ends at {bound.isoformat()}, and it is now "
        f"{now.astimezone(KST).isoformat()}): a day still in progress collected as a whole "
        "day is indistinguishable afterwards from a day that was genuinely quiet"
    )

day = first
while day <= last:
    print(day.isoformat())
    day += timedelta(days=1)
DAYS
) || exit 64

state_dir="$LOG_ROOT/backfill-days"
mkdir -p "$state_dir" || { echo "cannot create $state_dir" >&2; exit 73; }
ledger="$state_dir/days.log"
summary="$state_dir/summary.json"
started=$(date -u +%Y-%m-%dT%H:%M:%SZ)

# Rewritten after every day, so the cloud side reading it mid-backfill sees the
# days already banked rather than nothing until the end.
write_summary() {
  STATE_STARTED="$started" \
  STATE_OUTCOME="$1" \
  STATE_FAILED_DAY="${2:-}" \
  STATE_DAYS="$days" \
  STATE_SOURCES="${sources[*]}" \
  STATE_LEDGER="$ledger" \
  STATE_FIRST="$first" \
  STATE_LAST="$last" \
  STATE_LOG_ROOT="$LOG_ROOT" \
  python3 - > "$summary.tmp" <<'SUMMARY'
import json
import os
from datetime import datetime, timezone

days = [line for line in os.environ["STATE_DAYS"].splitlines() if line.strip()]

# The ledger is append-only, so the last word about a day wins: a day that
# failed and was collected on a later attempt reads as succeeded.
outcomes: dict[str, dict[str, object]] = {}
try:
    with open(os.environ["STATE_LEDGER"], encoding="utf-8") as handle:
        for line in handle:
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 4:
                continue
            day, outcome, code, stamp = parts[0], parts[1], parts[2], parts[3]
            outcomes[day] = {"outcome": outcome, "exit_code": int(code), "at": stamp}
except OSError:
    outcomes = {}

succeeded = [day for day in days if outcomes.get(day, {}).get("outcome") == "ok"]
failed = [
    {"day": day, "exit_code": outcomes[day]["exit_code"]}
    for day in days
    if outcomes.get(day, {}).get("outcome") == "failed"
]
remaining = [day for day in days if day not in outcomes]

print(json.dumps({
    "schema_version": 1,
    "started_at": os.environ["STATE_STARTED"],
    "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "outcome": os.environ["STATE_OUTCOME"],
    "first_day": os.environ["STATE_FIRST"],
    "last_day": os.environ["STATE_LAST"],
    "sources": os.environ["STATE_SOURCES"].split(),
    "days": len(days),
    "succeeded": succeeded,
    "failed": failed,
    "failed_day": os.environ["STATE_FAILED_DAY"] or None,
    "remaining": remaining,
    "log_root": os.environ["STATE_LOG_ROOT"],
}, ensure_ascii=False, indent=2))
SUMMARY
  mv -f "$summary.tmp" "$summary" 2>/dev/null || true
}

already_collected() {
  # The last recorded word about this day. A day that failed once and was
  # collected on a later attempt must not be run a third time.
  [ -f "$ledger" ] || return 1
  local verdict
  verdict=$(awk -F'\t' -v day="$1" '$1 == day { last = $2 } END { print last }' "$ledger")
  [ "$verdict" = "ok" ]
}

read -r -a command_parts <<< "$COMMAND"
source_flags=()
for source in "${sources[@]}"; do
  source_flags+=(--source "$source")
done

echo "backfill ${first}..${last} (${#sources[@]} sources: ${sources[*]})"
write_summary "running"

for day in $days; do
  next=$(DAY="$day" python3 -c '
import os
from datetime import date, timedelta
print((date.fromisoformat(os.environ["DAY"]) + timedelta(days=1)).isoformat())
')
  if already_collected "$day"; then
    echo "$day  already collected, skipping"
    continue
  fi

  echo "$day  collecting"
  "$RUNNER" "backfill-$day" -- "${command_parts[@]}" \
    "${source_flags[@]}" \
    --since "${day}T00:00:00+09:00" \
    --until "${next}T00:00:00+09:00"
  code=$?
  stamp=$(date -u +%Y-%m-%dT%H:%M:%SZ)

  if [ "$code" -eq 0 ]; then
    printf '%s\tok\t%s\t%s\n' "$day" "$code" "$stamp" >> "$ledger"
    write_summary "running"
    echo "$day  collected"
    continue
  fi

  # Stop here. Running the days after a failure would leave the gap sitting
  # under a row of successes, which is exactly how three sources went
  # uncollected for four days without anyone seeing it.
  printf '%s\tfailed\t%s\t%s\n' "$day" "$code" "$stamp" >> "$ledger"
  write_summary "failed" "$day"
  echo "$day  FAILED (exit $code). Stopping; the days after it were not attempted." >&2
  echo "log: $LOG_ROOT/backfill-$day/latest.log" >&2
  echo "state: $summary" >&2
  echo "fix it, then re-run the same command: the days already collected are skipped." >&2
  exit 1
done

write_summary "complete"
echo "backfill ${first}..${last} complete. state: $summary"
