#!/usr/bin/env bash
# Wake a local Claude Code session when the board holds work only this
# machine can do. Run by hkwa-wake.timer.
#
# The cloud side can develop and test, but it cannot touch the production
# data, the running containers, or the systemd units on this box. Those items
# sit on the board assigned to a local executor and would otherwise wait for a
# human to paste a prompt. This is the paste.
#
# Deliberately narrow:
#   - one session at a time, enforced by a lock
#   - one item per wake, the oldest ready one
#   - the session's first act is to claim the item, which is what stops the
#     next tick from waking a second session for the same work
#   - a bounded tool allowlist, not blanket permission
#
# Writes incoming/last-wake.json on every path, including the paths that do
# nothing, so the cloud side can tell idle from broken.

set -uo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$root" || exit 0

config_root="${APP_CONFIG_ROOT:-$HOME/.config/hk-work-assistant}"
items="$config_root/work/items.json"
state="incoming/last-wake.json"
lock="incoming/.wake.lock"
log_dir="$config_root/cowork/logs"
executor="${WAKE_EXECUTOR:-local}"

mkdir -p incoming "$log_dir"

started=$(date -u +%Y-%m-%dT%H:%M:%SZ)
outcome="idle"
item_id=""
detail=""

finish() {
  printf '{"started_at":"%s","finished_at":"%s","outcome":"%s","item_id":"%s","detail":%s}\n' \
    "$started" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$outcome" "$item_id" \
    "$(printf '%s' "$detail" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))' 2>/dev/null || echo '""')" \
    > "$state"
  exit 0
}

# A second session started while the first is mid-edit would fight it over the
# same working tree.
exec 8>"$lock"
if ! flock -n 8; then
  outcome="busy"
  detail="a session woken by an earlier tick is still running"
  finish
fi

if ! command -v claude >/dev/null 2>&1; then
  outcome="no-claude"
  detail="the claude CLI is not on PATH for the user manager; nothing can be woken"
  finish
fi

if [ ! -f "$items" ]; then
  outcome="no-board"
  detail="board file not found at $items"
  finish
fi

# Oldest ready item assigned to the local executor. A partially written board
# is not an error worth reporting: the next tick is three minutes away.
pick=$(ITEMS="$items" EXECUTOR="$executor" python3 - <<'PY' 2>/dev/null
import json, os, sys
try:
    with open(os.environ["ITEMS"], encoding="utf-8") as fh:
        board = json.load(fh)
except Exception:
    sys.exit(0)
items = board.get("items", board if isinstance(board, list) else [])
if isinstance(items, dict):
    items = list(items.values())
want = os.environ["EXECUTOR"]
ready = [
    it for it in items
    if isinstance(it, dict)
    and it.get("status") == "ready"
    and (it.get("assigned_to") or "") == want
    and not it.get("archived_at")
]
if not ready:
    sys.exit(0)
ready.sort(key=lambda it: it.get("created_at") or "")
print(ready[0].get("id", ""))
PY
)

if [ -z "$pick" ]; then
  finish
fi
item_id="$pick"

# A bounded allowlist rather than skipping permission checks outright. Override
# in $config_root/wake-local.env if a task genuinely needs more.
allowed='Read,Glob,Grep,Edit,Write,Bash(git:*),Bash(python3:*),Bash(pytest:*),Bash(docker:*),Bash(docker compose:*),Bash(work:*),Bash(systemctl:*),Bash(journalctl:*),Bash(ls:*),Bash(cat:*),Bash(head:*),Bash(tail:*),Bash(grep:*),Bash(sed:*),Bash(awk:*),Bash(find:*),Bash(wc:*),Bash(jq:*),Bash(sha256sum:*),Bash(ps:*)'
wake_timeout="${WAKE_TIMEOUT:-3600}"
# shellcheck source=/dev/null
[ -f "$config_root/wake-local.env" ] && . "$config_root/wake-local.env"

prompt=$(WAKE_ITEM="$item_id" WAKE_EXECUTOR="$executor" \
  python3 -c 'import os,sys; print(open("scripts/local-work-prompt.md",encoding="utf-8").read().replace("{{ITEM_ID}}",os.environ["WAKE_ITEM"]).replace("{{EXECUTOR}}",os.environ["WAKE_EXECUTOR"]))' 2>/dev/null)
if [ -z "$prompt" ]; then
  outcome="no-prompt"
  detail="scripts/local-work-prompt.md is missing or unreadable"
  finish
fi

stamp=$(date -u +%Y%m%dT%H%M%SZ)
transcript="$log_dir/wake-$stamp-$item_id.log"

timeout "$wake_timeout" claude -p "$prompt" \
  --permission-mode acceptEdits \
  --allowedTools "$allowed" \
  > "$transcript" 2>&1
rc=$?

if [ $rc -eq 124 ]; then
  outcome="timeout"
  detail="session exceeded ${wake_timeout}s; transcript at $transcript"
elif [ $rc -ne 0 ]; then
  outcome="session-failed"
  detail="claude exited $rc; $(tail -5 "$transcript" 2>/dev/null)"
else
  outcome="woke"
  detail="transcript at $transcript"
fi
finish
