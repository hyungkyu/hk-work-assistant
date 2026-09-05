#!/usr/bin/env bash
# Measure whether the last few KST days were actually collected, and write the
# answer where it can be seen. Run by hkwa-collection-audit.timer.
#
# On 2026-09-05 three of five sources had gone uncollected for four days. The
# evidence was on the 수집 현황 grid the whole time; nothing read it. The batch
# that was supposed to collect them named two sources, the code had grown to
# five, and the difference was visible only to somebody who opened the page.
# This is what opens the page.
#
# It fires an hour after the collection batch, so what it reads is last night's
# finished run rather than a run still going. An hour is the longest a missing
# source-day should ever go unreported.
#
# It reports; it does not collect. Every finding is a day somebody has to
# decide about -- re-run it, or accept the gap and say why -- and a batch that
# silently started a backfill would be making that decision, at 02:00, without
# anyone watching.

set -uo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$root" || exit 0

config_root="${APP_CONFIG_ROOT:-$HOME/.config/hk-work-assistant}"
state="incoming/last-collection-audit.json"
lock="incoming/.collection-audit.lock"
outbox="$config_root/cowork/outbox/mori"
# The item the summary is written onto. Absent means the audit still runs and
# still records its findings; only the board line is skipped.
target_file="$config_root/collection-audit-target"
mkdir -p incoming

started=$(date -u +%Y-%m-%dT%H:%M:%SZ)

fail() {
  printf '{"started_at":"%s","finished_at":"%s","outcome":"%s","detail":%s}\n' \
    "$started" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" \
    "$(printf '%s' "$2" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))' 2>/dev/null || echo '""')" \
    > "$state"
  exit 0
}

exec 7>"$lock"
flock -n 7 || fail "busy" "a previous collection audit tick is still running"

if [ -x .venv/bin/worklog ]; then
  worklog=.venv/bin/worklog
elif command -v worklog >/dev/null 2>&1; then
  worklog=$(command -v worklog)
else
  fail "no-worklog" "the worklog command is not available to the user manager"
fi

# Three finished days. Today is never audited: it is not over, and its cells
# are incomplete for a reason that is not a defect.
days=3
[ -f "$config_root/collection-audit-days" ] && {
  configured=$(head -n 1 "$config_root/collection-audit-days" | tr -cd '0-9')
  [ -n "$configured" ] && [ "$configured" -gt 0 ] && days="$configured"
}

report=$("$worklog" collection audit --days "$days" 2>&1)
if [ $? -ne 0 ] || [ -z "$report" ]; then
  fail "audit-failed" "$(printf '%s' "$report" | tail -10)"
fi

# The state file carries the same `outcome` key every other tick writes, so one
# reader can tell what happened without knowing which batch produced the file.
STARTED="$started" REPORT="$report" python3 - > "$state" <<'STATE'
import json, os
report = json.loads(os.environ["REPORT"])
report["started_at"] = os.environ["STARTED"]
report["outcome"] = "clean" if report.get("ok") else "gaps"
print(json.dumps(report, ensure_ascii=False, sort_keys=True))
STATE

summary=$(printf '%s' "$report" | python3 -c 'import json,sys; print(json.load(sys.stdin)["summary"])' 2>/dev/null)
[ -z "$summary" ] && exit 0
[ -f "$target_file" ] || exit 0
target=$(head -n 1 "$target_file" | tr -d '[:space:]')
[ -n "$target" ] || exit 0

# Written through the queue, not straight to the store, so the audit holds no
# lock and leaves the same receipt as every other queued edit.
mkdir -p "$outbox"
stamp=$(date -u +%Y%m%dT%H%M%SZ)
tmp=$(mktemp "$outbox/.collection-audit-$stamp.XXXXXX") || exit 0
TARGET="$target" SUMMARY="$summary" python3 - > "$tmp" <<'PY'
import json, os
print(json.dumps(
    {"work_id": os.environ["TARGET"], "next_action": os.environ["SUMMARY"]},
    ensure_ascii=False,
))
PY
mv "$tmp" "$outbox/$stamp--collection-audit--$target.json"
