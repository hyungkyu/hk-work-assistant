#!/usr/bin/env bash
# Re-run the layer check after a deploy and write the answer where it can be
# read without anyone typing a command. Run by scripts/deploy-tick.sh.
#
# HK, 2026-09-16: 이걸 내가 돌려야해? Fair question. Every fix this week ended
# with a command pasted into his terminal, and three of those runs were read as
# failures when the patch had simply not been applied yet. The batch already
# runs every few minutes and already knows when a new build lands, so the check
# belongs there.
#
# Read-only on purpose. It counts and reports; it never reprojects and never
# writes a digest. A batch that silently rebuilt data to make its own check
# pass would be the least trustworthy thing in this system.

set -uo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$root" || exit 0

config_root="${APP_CONFIG_ROOT:-$HOME/.config/hk-work-assistant}"
state="incoming/last-verify.json"
lock="incoming/.verify.lock"
mkdir -p incoming

started=$(date -u +%Y-%m-%dT%H:%M:%SZ)

finish() {
  STARTED="$started" OUTCOME="$1" DETAIL="${2-}" REPORT="${3-}" python3 - > "$state" <<'STATE'
import json, os
found = {
    "started_at": os.environ["STARTED"],
    "finished_at": __import__("datetime").datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
    "outcome": os.environ["OUTCOME"],
    "detail": os.environ.get("DETAIL", ""),
}
report = os.environ.get("REPORT", "")
if report:
    # The table as printed, so a reader sees the same thing the operator would.
    found["table"] = report.splitlines()[-40:]
print(json.dumps(found, ensure_ascii=False, indent=2))
STATE
  exit 0
}

exec 7>"$lock"
flock -n 7 || finish "busy" "a previous verify tick is still running"

[ -x .venv/bin/worklog ] || finish "no-worklog" "the worklog command is not available"

set -a
# shellcheck disable=SC1091
[ -f "$config_root/collect.env" ] && . "$config_root/collect.env"
set +a
[ -n "${DATABASE_URL:-}" ] || finish "no-database" "DATABASE_URL is not set for this user"

# Who to check. One name by default, because every person costs one Slack
# search per day and this runs on every deploy.
people=(류형규)
if [ -f "$config_root/verify-roster" ]; then
  people=()
  while IFS= read -r name; do
    [ -n "$name" ] && people+=("$name")
  done < "$config_root/verify-roster"
fi

# The last full week, ending yesterday: today is still arriving, and a day
# measured before its own midnight reports a gap that is not one.
until_day=$(date -d 'yesterday' +%F)
since_day=$(date -d '7 days ago' +%F)

report=""
gaps=0
for name in "${people[@]}"; do
  output=$(.venv/bin/worklog reconcile --person-name "$name" \
    --since "$since_day" --until "$until_day" --gaps-only 2>&1)
  status=$?
  report=$(printf '%s\n== %s ==\n%s' "$report" "$name" "$output")
  [ "$status" -eq 1 ] && gaps=$((gaps + 1))
  [ "$status" -gt 1 ] && finish "verify-failed" "$name: $(printf '%s' "$output" | tail -3)" "$report"
done

if [ "$gaps" -eq 0 ]; then
  finish "clean" "$since_day..$until_day, ${#people[@]} people, no layer lost anything" "$report"
fi
finish "gaps" "$since_day..$until_day, $gaps of ${#people[@]} people have gaps" "$report"
