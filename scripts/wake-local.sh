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
#   - one item per wake, the oldest ready one that is not cooling off
#   - the session's first act is to claim the item, which is what stops the
#     next tick from waking a second session for the same work
#   - a bounded tool allowlist, not blanket permission
#
# It is a launcher, not an executor. It never writes to the board: only the
# session it wakes may do that. What it does do is *read* the board again once
# the session has ended, so it can say whether the item actually moved.
#
# Writes $state_dir/last-wake.json on every path, including the paths that do
# nothing, so the cloud side can tell idle from broken.

set -uo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$root" || exit 0

config_root="${APP_CONFIG_ROOT:-$HOME/.config/hk-work-assistant}"
items="$config_root/work/items.json"
# Overridable so the behaviour of this script can be exercised without a test
# writing into the real carrier directory.
state_dir="${WAKE_STATE_DIR:-incoming}"
state="$state_dir/last-wake.json"
skips="$state_dir/wake-skips.json"
lock="$state_dir/.wake.lock"
log_dir="$config_root/cowork/logs"
executor="${WAKE_EXECUTOR:-local}"

# The prompt tells the session to run `worklog` and `python` by name, and the
# allowlist below permits exactly those two names. Neither is on the user
# manager's PATH on this machine -- `worklog` is a console script inside the
# repository's virtualenv -- so put that virtualenv in front of PATH here
# rather than teaching the prompt an absolute path that a human would then have
# to keep in step with the allowlist. That hand-kept pair is precisely what
# broke: the allowlist said `Bash(work:*)`, a command that exists nowhere, and
# every board write the prompt asked for was refused for some 300 ticks while
# the state file reported `outcome=woke` each time. Prepending is also the only
# form that survives a repository path containing a space, which this one has.
PATH="$root/.venv/bin:$PATH"
export PATH

mkdir -p "$state_dir" "$log_dir"

started=$(date -u +%Y-%m-%dT%H:%M:%SZ)
outcome="idle"
item_id=""
detail=""
skipped="[]"

finish() {
  printf '{"started_at":"%s","finished_at":"%s","outcome":"%s","item_id":"%s","skipped":%s,"detail":%s}\n' \
    "$started" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$outcome" "$item_id" "$skipped" \
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

# A session that cannot reach the board cannot claim, work, or close anything,
# so waking one would burn a tick and change nothing. Refusing by name says
# which half is missing; the silent version of this cost 300 ticks.
if ! command -v worklog >/dev/null 2>&1; then
  outcome="no-worklog"
  detail="worklog is on neither PATH nor $root/.venv/bin; a woken session could not record anything on the board"
  finish
fi

if [ ! -f "$items" ]; then
  outcome="no-board"
  detail="board file not found at $items"
  finish
fi

# A bounded allowlist rather than skipping permission checks outright. Override
# in $config_root/wake-local.env if a task genuinely needs more -- but only ever
# to widen it: every command the prompt prints has to appear here, and the test
# that checks that can only see the default below, not that machine's file.
allowed='Read,Glob,Grep,Edit,Write,Bash(git:*),Bash(python:*),Bash(python3:*),Bash(pytest:*),Bash(worklog:*),Bash(docker:*),Bash(docker compose:*),Bash(systemctl:*),Bash(journalctl:*),Bash(ls:*),Bash(cat:*),Bash(head:*),Bash(tail:*),Bash(grep:*),Bash(sed:*),Bash(awk:*),Bash(find:*),Bash(wc:*),Bash(jq:*),Bash(sha256sum:*),Bash(ps:*)'
wake_timeout="${WAKE_TIMEOUT:-3600}"
# How long an item that did not move is passed over for. See the picker below
# for why this is a cooling-off period and not an attempt count.
skip_hours="${WAKE_SKIP_HOURS:-6}"
# shellcheck source=/dev/null
[ -f "$config_root/wake-local.env" ] && . "$config_root/wake-local.env"

# The oldest ready item assigned to the local executor that is not cooling off,
# and what state it is in right now, so the same two values can be compared
# after the session ends.
#
# The cooling-off ledger is this script's own state, not the board's. An item
# the session could not claim is passed over for $skip_hours and then offered
# again. A period rather than a bounded number of attempts, because the two
# ways an item fails to move want opposite things: a permanent cause (a tool
# the session is not permitted to run, an instruction it cannot follow) must
# stop costing a wake every three minutes, and a transient one (a session that
# died mid-edit) must come back on its own -- nobody is watching this box to
# clear a counter. A period does both, and it costs a permanently stuck item
# four wakes a day instead of four hundred and eighty. Nothing is hidden by
# it: the item stays `ready` and untouched on the board, which is exactly what
# `worklog work audit`'s `ready_untouched` check reports after a day.
#
# A partially written board is not an error worth reporting: the next tick is
# three minutes away.
pick_report=$(ITEMS="$items" EXECUTOR="$executor" SKIPS="$skips" python3 - <<'PY' 2>/dev/null
import json, os, sys
from datetime import datetime, timezone


def blank():
    print(); print(); print(); print("[]")
    raise SystemExit(0)


try:
    with open(os.environ["ITEMS"], encoding="utf-8") as fh:
        board = json.load(fh)
except Exception:
    blank()

items = board.get("items", board if isinstance(board, list) else [])
if isinstance(items, dict):
    items = list(items.values())
items = [it for it in items if isinstance(it, dict)]

want = os.environ["EXECUTOR"]
ready = [
    it for it in items
    if it.get("status") == "ready"
    and (it.get("assigned_to") or "") == want
    and not it.get("archived_at")
]
ready.sort(key=lambda it: it.get("created_at") or "")

try:
    with open(os.environ["SKIPS"], encoding="utf-8") as fh:
        ledger = json.load(fh).get("skips") or {}
except Exception:
    ledger = {}

# An entry for an item that has left the board has nothing left to cool off.
live = {str(it.get("id")) for it in items}
ledger = {k: v for k, v in ledger.items() if k in live and isinstance(v, dict)}

now = datetime.now(timezone.utc)
skipped = []
pick = None
for candidate in ready:
    entry = ledger.get(str(candidate.get("id")))
    until = None
    if entry:
        try:
            until = datetime.strptime(
                str(entry.get("until") or ""), "%Y-%m-%dT%H:%M:%SZ"
            ).replace(tzinfo=timezone.utc)
        except Exception:
            until = None
    if until is not None and until > now:
        skipped.append({
            "item_id": candidate.get("id", ""),
            "attempts": entry.get("attempts", 0),
            "until": entry.get("until", ""),
            "reason": entry.get("reason", ""),
        })
        continue
    pick = candidate
    break

try:
    with open(os.environ["SKIPS"], "w", encoding="utf-8") as fh:
        json.dump({"skips": ledger}, fh)
except Exception:
    pass

print(pick.get("id", "") if pick else "")
print(pick.get("status", "") if pick else "")
print(pick.get("revision", "") if pick else "")
print(json.dumps(skipped))
PY
)

pick=$(printf '%s\n' "$pick_report" | sed -n 1p)
before="$(printf '%s\n' "$pick_report" | sed -n 2p) $(printf '%s\n' "$pick_report" | sed -n 3p)"
skipped=$(printf '%s\n' "$pick_report" | sed -n 4p)
[ -n "$skipped" ] || skipped="[]"

if [ -z "$pick" ]; then
  # An empty queue and a queue where everything is cooling off are both idle,
  # but only one of them means there is no work.
  if [ "$skipped" != "[]" ]; then
    detail="every ready item for $executor is cooling off after a session left it unmoved"
  fi
  finish
fi
item_id="$pick"

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

# Whether the session did anything is a question about the board, and the board
# is a file this script already reads. `claude` exiting zero says only that the
# CLI ended cleanly.
after_report=$(ITEMS="$items" SKIPS="$skips" WAKE_ITEM="$item_id" BEFORE="$before" \
  SKIP_HOURS="$skip_hours" SKIPPED_IN="$skipped" python3 - <<'PY' 2>/dev/null
import json, os
from datetime import datetime, timedelta, timezone

now = datetime.now(timezone.utc)
item_id = os.environ["WAKE_ITEM"]
skipped = json.loads(os.environ.get("SKIPPED_IN") or "[]")


def stamp(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def report(moved, note):
    print(moved); print(note); print(json.dumps(skipped))
    raise SystemExit(0)


def store(ledger):
    try:
        with open(os.environ["SKIPS"], "w", encoding="utf-8") as fh:
            json.dump({"skips": ledger}, fh)
    except Exception:
        pass


try:
    with open(os.environ["ITEMS"], encoding="utf-8") as fh:
        board = json.load(fh)
except Exception:
    # Nothing was learned about the item, so nothing is recorded against it.
    report("unknown", "")

items = board.get("items", board if isinstance(board, list) else [])
if isinstance(items, dict):
    items = list(items.values())
found = next(
    (it for it in items if isinstance(it, dict) and str(it.get("id")) == item_id), None
)
try:
    with open(os.environ["SKIPS"], encoding="utf-8") as fh:
        ledger = json.load(fh).get("skips") or {}
except Exception:
    ledger = {}

if found is None:
    # Archived, or deleted outright. Either way it left the queue.
    ledger.pop(item_id, None)
    store(ledger)
    report("yes", "")

after = "{} {}".format(found.get("status", ""), found.get("revision", ""))
if after != os.environ["BEFORE"]:
    ledger.pop(item_id, None)
    store(ledger)
    report("yes", "")

entry = ledger.get(item_id) or {"attempts": 0, "first_at": stamp(now)}
entry["attempts"] = int(entry.get("attempts") or 0) + 1
entry["last_at"] = stamp(now)
entry["until"] = stamp(now + timedelta(hours=float(os.environ.get("SKIP_HOURS") or 6)))
entry["reason"] = "a session ended and left the item at {}".format(
    after.replace(" ", " revision ")
)
ledger[item_id] = entry
store(ledger)
skipped.append(
    {
        "item_id": item_id,
        "attempts": entry["attempts"],
        "until": entry["until"],
        "reason": entry["reason"],
    }
)
report(
    "no",
    "attempt {} left it unmoved, so it is not offered again before {}".format(
        entry["attempts"], entry["until"]
    ),
)
PY
)

moved=$(printf '%s\n' "$after_report" | sed -n 1p)
skip_note=$(printf '%s\n' "$after_report" | sed -n 2p)
skipped=$(printf '%s\n' "$after_report" | sed -n 3p)
[ -n "$skipped" ] || skipped="[]"
[ -n "$moved" ] || moved="unknown"

if [ $rc -eq 124 ]; then
  outcome="timeout"
  detail="session exceeded ${wake_timeout}s; transcript at $transcript"
elif [ $rc -ne 0 ]; then
  outcome="session-failed"
  detail="claude exited $rc; $(tail -5 "$transcript" 2>/dev/null)"
elif [ "$moved" = "yes" ]; then
  outcome="woke"
  detail="$item_id moved from $before; transcript at $transcript"
elif [ "$moved" = "no" ]; then
  outcome="unclaimed"
  detail="claude exited 0 but $item_id is still $before on the board; transcript at $transcript"
else
  outcome="unclaimed"
  detail="claude exited 0 but the board could not be re-read, so $item_id cannot be shown to have moved; transcript at $transcript"
fi

# A timed-out or failed session that nevertheless left the item where it was is
# starving the queue just as surely as one that exited zero, so it cools off on
# the same terms. The outcome keeps its own name: it says more than `unclaimed`.
[ -n "$skip_note" ] && detail="$detail; $skip_note"
finish
