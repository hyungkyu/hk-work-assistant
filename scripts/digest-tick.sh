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

status=0

echo "=== roster sync"
"$worklog" org sync --apply || status=1

echo
echo "=== digest (yesterday KST)"
"$worklog" digest --apply || status=1

echo
echo "=== org chart"
mkdir -p "$out_root"
"$worklog" org chart --html "$out_root/org-chart.html" || status=1

echo
echo "=== digest coverage"
"$worklog" digest --status || true

exit "$status"
