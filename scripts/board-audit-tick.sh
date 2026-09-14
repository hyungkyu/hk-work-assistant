#!/usr/bin/env bash
# Measure whether the board still matches the work, and write the answer where
# it can be seen. Run by hkwa-board-audit.timer.
#
# P0 -- "실제로 하고 있는 일과 백오피스 업무 현황을 일치시키는 것" -- was reached by
# hand on 2026-09-05 and had drifted again the same day, because nothing was
# watching. This is what watches.
#
# It reports; it does not repair. Every finding is something a person or a
# requester has to decide about, and a batch that silently re-queued work would
# be making that decision on their behalf.

set -uo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$root" || exit 0

config_root="${APP_CONFIG_ROOT:-$HOME/.config/hk-work-assistant}"
state="incoming/last-audit.json"
lock="incoming/.audit.lock"
outbox="$config_root/cowork/outbox/mori"
# The item the summary is written onto. Absent means the audit still runs and
# still records its findings; only the board line is skipped.
target_file="$config_root/board-audit-target"
mkdir -p incoming

started=$(date -u +%Y-%m-%dT%H:%M:%SZ)

fail() {
  printf '{"started_at":"%s","finished_at":"%s","outcome":"%s","detail":%s}\n' \
    "$started" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" \
    "$(printf '%s' "$2" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))' 2>/dev/null || echo '""')" \
    > "$state"
  exit 0
}

exec 6>"$lock"
flock -n 6 || fail "busy" "a previous audit tick is still running"

if [ -x .venv/bin/worklog ]; then
  worklog=.venv/bin/worklog
elif command -v worklog >/dev/null 2>&1; then
  worklog=$(command -v worklog)
else
  fail "no-worklog" "the worklog command is not available to the user manager"
fi

# The roster is the set of executors that exist in the world the board is
# supposed to describe. Keep it here, in review, rather than inferring it from
# the board itself -- inferring it would make every orphaned item define itself
# as valid.
roster=(--executor mori --executor local --executor batch --executor hk)
[ -f "$config_root/board-audit-roster" ] && {
  roster=()
  while IFS= read -r name; do
    [ -n "$name" ] && roster+=(--executor "$name")
  done < "$config_root/board-audit-roster"
}

report=$("$worklog" work audit "${roster[@]}" 2>&1)
if [ $? -ne 0 ] || [ -z "$report" ]; then
  fail "audit-failed" "$(printf '%s' "$report" | tail -10)"
fi

# The state file carries the same `outcome` key every other tick writes, so one
# reader can tell what happened without knowing which batch produced the file.
STARTED="$started" REPORT="$report" python3 - > "$state" <<'STATE'
import json, os
report = json.loads(os.environ["REPORT"])
report["started_at"] = os.environ["STARTED"]
report["outcome"] = "clean" if report.get("ok") else "findings"
print(json.dumps(report, ensure_ascii=False, sort_keys=True))
STATE

# The board itself, trimmed, next to the audit of it. The audit says how many
# items disagree with the work; it does not say what the items are, and a
# reviewer who can only see the count has to take it on faith. Written by the
# same batch so the two files are always of the same moment.
#
# Trimmed on purpose: ids, titles, status, who holds it, the next action.
# Descriptions and history stay in the store — this is a list to check the
# work against, not a copy of the board.
board=$("$worklog" work board 2>/dev/null)
if [ -n "$board" ]; then
  BOARD="$board" STARTED="$started" python3 - > "incoming/last-board.json" <<'BOARD' || true
import json, os
board = json.loads(os.environ["BOARD"])


def walk(value):
    """Every item object anywhere in the board's column structure."""
    if isinstance(value, dict):
        if "id" in value and "status" in value:
            yield value
            return
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


keep = ("id", "title", "phase", "status", "assigned_to", "next_action", "updated_at", "revision")
items, seen = [], set()
for item in walk(board):
    if item["id"] in seen:
        continue
    seen.add(item["id"])
    items.append({key: item.get(key) for key in keep})
items.sort(key=lambda item: (str(item.get("phase") or "ZZ"), str(item.get("status")), str(item.get("title"))))
print(json.dumps(
    {"started_at": os.environ["STARTED"], "outcome": "snapshot",
     "items": len(items), "board": items},
    ensure_ascii=False, sort_keys=True, indent=2,
))
BOARD
fi

summary=$(printf '%s' "$report" | python3 -c 'import json,sys; print(json.load(sys.stdin)["summary"])' 2>/dev/null)
[ -z "$summary" ] && exit 0
[ -f "$target_file" ] || exit 0
target=$(head -n 1 "$target_file" | tr -d '[:space:]')
[ -n "$target" ] || exit 0

# Written through the queue, not straight to the store, so the audit holds no
# lock and leaves the same receipt as every other queued edit.
mkdir -p "$outbox"
stamp=$(date -u +%Y%m%dT%H%M%SZ)
tmp=$(mktemp "$outbox/.board-audit-$stamp.XXXXXX") || exit 0
TARGET="$target" SUMMARY="$summary" python3 - > "$tmp" <<'PY'
import json, os
print(json.dumps(
    {"work_id": os.environ["TARGET"], "next_action": os.environ["SUMMARY"]},
    ensure_ascii=False,
))
PY
mv "$tmp" "$outbox/$stamp--board-audit--$target.json"
