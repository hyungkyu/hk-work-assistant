#!/usr/bin/env bash
# Translate the roadmap rows a refresh left in Korean. Run by
# hkwa-roadmap-translate.timer.
#
# A refresh runs in the container, from the backoffice button or the CLI, and
# the container has no GPU. The model that translates is on this machine. So the
# refresh leaves new and reworded rows pending -- the screen marks them
# "번역 대기" -- and this picks them up within one tick. HK, 2026-10-14: the
# content has to be multilingual, translated as it arrives, by a local model.
#
# Almost every tick has nothing to do, and then it loads no model: the pending
# count is read first. A row the model could not translate acceptably is
# remembered at its Korean hash and skipped until the Korean changes, so one
# hard line does not reload the model every fifteen minutes to fail again.

set -uo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$root" || exit 0

state="incoming/last-translate.json"
lock="incoming/.translate.lock"
remember="incoming/.translate-refused.json"
mkdir -p incoming

started=$(date -u +%Y-%m-%dT%H:%M:%SZ)

write_state() {
  STARTED="$started" OUTCOME="$1" DETAIL="$2" python3 - > "$state" <<'STATE'
import json, os
detail = os.environ.get("DETAIL", "")
try:
    body = json.loads(detail.strip().splitlines()[-1]) if detail.strip() else {}
except (ValueError, IndexError):
    body = {"detail": detail[-800:]}
body.update({"started_at": os.environ["STARTED"], "outcome": os.environ["OUTCOME"]})
print(json.dumps(body, ensure_ascii=False, sort_keys=True))
STATE
  exit 0
}

exec 5>"$lock"
flock -n 5 || write_state "busy" "a previous translate tick is still running"

if [ -x .venv/bin/worklog ]; then
  worklog=.venv/bin/worklog
else
  write_state "no-worklog" "the worklog command is not available to the user manager"
fi
[ -n "${DATABASE_URL:-}" ] || write_state "no-database" "DATABASE_URL is not set; see collect.env"

counted=$("$worklog" roadmap translate --remember "$remember" 2>&1) || \
  write_state "failed" "$counted"
pending=$(printf '%s' "$counted" | tail -1 | python3 -c 'import json,sys; print(json.load(sys.stdin)["pending"])' 2>/dev/null)
[ "${pending:-0}" = "0" ] && write_state "idle" "$counted"

out=$("$worklog" roadmap translate --apply --remember "$remember" 2>&1) || write_state "failed" "$out"
write_state "translated" "$out"
