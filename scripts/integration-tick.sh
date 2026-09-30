#!/usr/bin/env bash
# 통합데브: pull what GitHub holds, test it here, say whether it is green.
#
# The gate this replaces was an accident. `incoming-tick.sh` runs the suite
# before it pushes, so for as long as every change arrived as a patch the
# testing happened. On 2026-09-30 a session committed to main on this machine
# instead, two tests went red, and the carrier stopped pushing -- which was
# the right outcome and only worked because that session's work happened to
# pass through here.
#
# HK, 2026-09-30: 각자의 작업공간에서 작업하고, 깃헙에 푸시하고, 풀은 이 pc가
# 서버니까 여기로 한다. Under that flow the carrier is no longer on the path,
# so the gate has to sit where everything converges: whatever is on
# origin/main, tested on this machine.
#
# Its own clone, never the working tree the carrier uses. Two batches sharing
# one worktree would take turns moving each other's HEAD, which is a way to
# make both of them wrong at once.
#
# Its own database, `worklog_dev`, on the same server. The suite creates and
# deletes ledger rows; pointing it at the real one would have a test truncate
# what the collection spent the night gathering.
#
# Reports. Does not fix, does not push, does not deploy. A red result is a
# fact somebody acts on, and a batch that repaired main on its own would be
# rewriting work it did not do.

set -uo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$root" || exit 0

state="incoming/last-integration.json"
lock="incoming/.integration.lock"
mkdir -p incoming

# Where the integration clone lives, and which database it may touch. Both
# overridable so this can be exercised against a throwaway pair in a test.
work="${WORKLOG_INTEGRATION_ROOT:-$HOME/.local/share/hk-work-assistant/integration}"
dev_url="${WORKLOG_INTEGRATION_DATABASE_URL:-}"

started=$(date -u +%Y-%m-%dT%H:%M:%SZ)
outcome="unknown"
detail=""
head_sha="unknown"
passed=0
skipped=0

finish() {
  printf '{"started_at":"%s","finished_at":"%s","outcome":"%s","head":"%s","passed":%d,"skipped":%d,"detail":%s}\n' \
    "$started" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$outcome" "$head_sha" \
    "$passed" "$skipped" \
    "$(printf '%s' "$detail" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))' 2>/dev/null || echo '""')" \
    > "$state"
  exit 0
}

exec 9>"$lock"
if ! flock -n 9; then
  outcome="busy"
  detail="a previous integration run still holds the lock"
  finish
fi

# The database is the whole point of this tier: without it the suite skips
# every test that needs one -- 91 of them on 2026-09-30, which is the ledger,
# the projection, the blocks and the pairing. A run that skipped those and
# reported green would be the false green this project has already lost days
# to, so it refuses instead.
if [ -z "$dev_url" ]; then
  outcome="unconfigured"
  detail="WORKLOG_INTEGRATION_DATABASE_URL is not set; the database-backed tests would silently skip"
  finish
fi

origin=$(git -C "$root" remote get-url origin 2>/dev/null)
if [ -z "$origin" ]; then
  outcome="unconfigured"
  detail="no origin remote to pull from"
  finish
fi

if [ ! -d "$work/.git" ]; then
  mkdir -p "$(dirname "$work")"
  if ! git clone -q "$origin" "$work" 2>/dev/null; then
    outcome="clone-failed"
    detail="could not clone $origin into $work"
    finish
  fi
fi

# --ff-only, and a hard reset is deliberately absent: if this clone has
# somehow acquired local commits, that is a thing to look at rather than
# something for a batch to erase at 30-minute intervals.
if ! git -C "$work" fetch -q origin main 2>/dev/null; then
  outcome="fetch-failed"
  detail="could not fetch origin/main"
  finish
fi
if ! git -C "$work" merge -q --ff-only origin/main 2>/dev/null; then
  outcome="diverged"
  detail="the integration clone has commits origin/main does not; not resetting it automatically"
  head_sha=$(git -C "$work" rev-parse --short HEAD 2>/dev/null || echo unknown)
  finish
fi
head_sha=$(git -C "$work" rev-parse --short HEAD 2>/dev/null || echo unknown)

if [ -x "$work/.venv/bin/python" ]; then
  py="$work/.venv/bin/python"
elif [ -x "$root/.venv/bin/python" ]; then
  py="$root/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  py=python3
else
  outcome="untested"
  detail="no python found; nothing was run"
  finish
fi

# Schema first. A migration that landed with the code has to be applied here
# before the suite can mean anything, and applying it to `worklog_dev` is also
# the only place a migration gets exercised outside somebody's laptop.
migrate=$(cd "$work" && DATABASE_URL="$dev_url" "$py" -m rlwrld_worklog.cli ledger-migrate --apply 2>&1)
if [ $? -ne 0 ]; then
  outcome="migrate-failed"
  detail=$(printf '%s' "$migrate" | tail -5)
  finish
fi

log=$(cd "$work" && WORKLOG_TEST_DATABASE_URL="$dev_url" "$py" -m pytest -q 2>&1)
status=$?
summary=$(printf '%s' "$log" | tail -1)
passed=$(printf '%s' "$summary" | grep -oE '[0-9]+ passed' | grep -oE '[0-9]+' || echo 0)
skipped=$(printf '%s' "$summary" | grep -oE '[0-9]+ skipped' | grep -oE '[0-9]+' || echo 0)

if [ "$status" -ne 0 ]; then
  outcome="red"
  detail=$(printf '%s' "$log" | grep -E '^(FAILED|ERROR)' | head -20)
  [ -n "$detail" ] || detail="$summary"
  finish
fi

outcome="green"
detail="$summary"
finish
