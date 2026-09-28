#!/usr/bin/env bash
# One tick of the dev -> prod carrier. Run by hkwa-incoming.timer.
#
# Applies whatever the cloud development side left in incoming/, runs the
# suite against this machine, and pushes only if it is green. Never pushes
# code it could not test: an untestable push is how "what is running" and
# "what is committed" came apart in the first place.
#
# Writes incoming/last-run.json every tick, whatever happens. That file is
# the cloud side's only way to see what this machine did, so it is written
# even on the paths that do nothing.

set -uo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$root" || exit 0

state="incoming/last-run.json"
lock="incoming/.tick.lock"
mkdir -p incoming/applied incoming/failed

started=$(date -u +%Y-%m-%dT%H:%M:%SZ)
applied=0
outcome="idle"
detail=""
# Which board items the applied patches named. Empty is a real state and is
# reported as such -- see `unannounced` below.
items=""

finish() {
  mkdir -p incoming
  printf '{"started_at":"%s","finished_at":"%s","outcome":"%s","applied":%d,"head":"%s","items":%s,"detail":%s}\n' \
    "$started" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$outcome" "$applied" \
    "$(git rev-parse --short HEAD 2>/dev/null || echo unknown)" \
    "$(printf '%s' "$items" | python3 -c 'import json,sys; print(json.dumps([w for w in sys.stdin.read().split() if w]))' 2>/dev/null || echo '[]')" \
    "$(printf '%s' "$detail" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))' 2>/dev/null || echo '""')" \
    > "$state"
  exit 0
}

# Single flight. A tick that overlaps the previous one would apply a patch
# on top of a tree the previous tick is still moving.
exec 9>"$lock"
if ! flock -n 9; then
  outcome="busy"
  detail="a previous tick still holds the lock"
  finish
fi

shopt -s nullglob
patches=(incoming/*.patch)
if [ ${#patches[@]} -eq 0 ]; then
  finish
fi

# A dirty worktree stops the carrier. On 2026-09-16 it stopped it for four
# hours and the reason read "worktree is dirty" every tick, which named
# neither the files nor how long, so it looked like the same harmless line
# each time. Say which files, and say since when.
dirty=$(git status --porcelain --untracked-files=no)
blocked_marker="incoming/.blocked-since"
if [ -n "$dirty" ]; then
  [ -f "$blocked_marker" ] || date -u +%Y-%m-%dT%H:%M:%SZ > "$blocked_marker"
  since=$(cat "$blocked_marker" 2>/dev/null || echo unknown)
  minutes="?"
  if [ "$since" != "unknown" ]; then
    minutes=$(( ( $(date -u +%s) - $(date -u -d "$since" +%s 2>/dev/null || date -u +%s) ) / 60 ))
  fi
  files=$(printf '%s\n' "$dirty" | awk '{print $NF}' | paste -sd' ' -)
  outcome="refused"
  detail="worktree is dirty since $since (${minutes}m); ${#patches[@]} patch(es) waiting; files: $files"
  finish
fi
rm -f "$blocked_marker"

branch=$(git rev-parse --abbrev-ref HEAD)
if [ "$branch" != "main" ]; then
  outcome="refused"
  detail="on branch '$branch', expected main"
  finish
fi

if ! git fetch -q origin main || ! git merge -q --ff-only origin/main; then
  outcome="refused"
  detail="could not fast-forward to origin/main"
  finish
fi

before=$(git rev-parse HEAD)
for p in "${patches[@]}"; do
  if git am --3way "$p" >/dev/null 2>&1; then
    mv "$p" "incoming/applied/$(basename "$p")"
    applied=$((applied + 1))
  else
    git am --abort >/dev/null 2>&1
    mv "$p" "incoming/failed/$(basename "$p")"
    outcome="apply-failed"
    detail="$(basename "$p") did not apply; moved to incoming/failed/; later patches not attempted"
    finish
  fi
done

# Which board items this landed against.
#
# HK, 2026-09-14: 주인업는 작업은 킬 하자. 네가 모르는 작업은 없어야해. Three
# separate board items exist because I agreed to announce work before starting
# it and then did not, three times -- a rule kept in memory is a rule that gets
# kept when nothing is urgent. So the carrier reads it off the commits instead
# of trusting anyone to remember: any `wi_<hex>` in the applied messages is
# recorded here, and an empty list is a patch that arrived against nothing.
#
# Recorded, not refused. Refusing would stop the carrier over bookkeeping, and
# a batch that blocks work to enforce a note about work is the wrong trade. The
# board audit is where an empty list becomes a question somebody answers.
items=$(git log --format=%B "$before"..HEAD 2>/dev/null \
        | grep -oE 'wi_[0-9a-f]{8,}' | sort -u | paste -sd' ' -)
if [ -z "$items" ]; then
  unannounced=" (no board item named)"
else
  unannounced=""
fi

# Only a suite that actually ran counts. A missing interpreter is a reason
# not to push, not a reason to push untested.
if [ -x .venv/bin/python ]; then
  py=.venv/bin/python
elif command -v python3 >/dev/null 2>&1; then
  py=python3
else
  outcome="untested"
  detail="no python found; $applied patch(es) applied locally but not pushed"
  finish
fi

test_log=$("$py" -m pytest -q 2>&1)
if [ $? -ne 0 ]; then
  outcome="tests-failed"
  detail=$(printf '%s' "$test_log" | tail -20)
  finish
fi

if git push -q origin main; then
  outcome="pushed"
  detail="$(printf '%s' "$test_log" | tail -1)${unannounced}"
else
  outcome="push-failed"
  detail="$applied patch(es) applied and green, but the push was refused"
fi
finish
