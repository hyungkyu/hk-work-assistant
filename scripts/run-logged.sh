#!/usr/bin/env bash
# Run a command and put its output where it can actually be read.
#
#   scripts/run-logged.sh <name> -- <command> [args...]
#   scripts/run-logged.sh backfill-sep -- .venv/bin/worklog daily-collect --since 5d
#
# The cloud side can read the connected folders and nothing else. A batch that
# logs to /tmp is a batch whose output only exists on this machine, which on
# 2026-09-05 meant a running backfill could be watched only by inspecting the
# archive it was writing. That is not a log; that is forensics.
#
# So: every ad-hoc or scheduled run goes through here, and its output lands
# under the data disk beside the archive it produced.
#
#   <log root>/<name>/<UTC stamp>.log    the run's output, verbatim
#   <log root>/<name>/latest.log         a copy of the most recent one
#   <log root>/<name>/running.json       written at start, removed at the end
#   <log root>/<name>/last.json          exit code, timings, and the tail
#
# `last.json` is written whatever happens, including when the command could not
# be started at all, because "no file" and "nothing happened" must never look
# the same from the other side.
#
# `running.json` is the start record. `last.json` alone made a running or
# killed batch invisible: it is written only at the end, so a run that was
# Ctrl-C'd (the 2026-09-08 Notion verification) or is still going left
# nothing, and the board showed a job that never visibly started or died. The
# marker is deliberately NOT removed on kill -- a marker with no matching
# finish IS the record that the run never finished, and batch_runs.py reads it
# as exactly that.

set -uo pipefail

LOG_ROOT=${WORKLOG_LOG_ROOT:-/data/rlwrld-worklog/logs}
KEEP=${WORKLOG_LOG_KEEP:-30}

name=${1:-}
shift || true
if [ -z "$name" ] || [ "${1:-}" != "--" ]; then
  echo "usage: $0 <name> -- <command> [args...]" >&2
  exit 64
fi
shift

case "$name" in
  */* | .* | "")
    echo "name must be a single path segment: '$name'" >&2
    exit 64
    ;;
esac

dir="$LOG_ROOT/$name"
mkdir -p "$dir" || { echo "cannot create $dir" >&2; exit 73; }

# The pid is part of the name, not decoration: two runs of the same name
# inside one second would otherwise write the same file and one would
# silently disappear.
stamp="$(date -u +%Y%m%dT%H%M%SZ)-$$"
log="$dir/$stamp.log"
started=$(date -u +%Y-%m-%dT%H:%M:%SZ)

running="$dir/running.json"

{
  printf '=== %s\n' "$started"
  printf '=== %s\n' "$*"
  printf '=== host %s  pid %s\n' "$(hostname)" "$$"
  # A leftover marker means the previous run of this name never wrote its
  # finish. Note it in this run's log before replacing it -- the marker file
  # itself is about to be overwritten, and this line is what survives.
  if [ -f "$running" ]; then
    printf '=== previous run left a running marker (never finished): %s\n' \
      "$(cat "$running" 2>/dev/null | tr -d '\n' | head -c 400)"
  fi
  printf '\n'
} > "$log"

STARTED="$started" LOG="$log" CMD="$*" PID="$$" python3 - > "$running" <<'STATE'
import json, os
print(json.dumps({
    "started_at": os.environ["STARTED"],
    "command": os.environ["CMD"],
    "pid": int(os.environ["PID"]),
    "log": os.environ["LOG"],
}, ensure_ascii=False, indent=2))
STATE

# `script` would give a pty and keep progress output honest, but it is not
# everywhere; plain redirection with line buffering is enough for these.
"$@" >> "$log" 2>&1
code=$?

finished=$(date -u +%Y-%m-%dT%H:%M:%SZ)
printf '\n=== exit %s at %s\n' "$code" "$finished" >> "$log"
cp -f "$log" "$dir/latest.log" 2>/dev/null || true

STARTED="$started" FINISHED="$finished" CODE="$code" LOG="$log" CMD="$*" \
python3 - > "$dir/last.json" <<'STATE'
import json, os
log = os.environ["LOG"]
try:
    with open(log, encoding="utf-8", errors="replace") as handle:
        tail = handle.read()[-4000:]
except OSError as error:
    tail = f"<log unreadable: {error.__class__.__name__}>"
print(json.dumps({
    "started_at": os.environ["STARTED"],
    "finished_at": os.environ["FINISHED"],
    "command": os.environ["CMD"],
    "exit_code": int(os.environ["CODE"]),
    "outcome": "ok" if os.environ["CODE"] == "0" else "failed",
    "log": log,
    "tail": tail,
}, ensure_ascii=False, indent=2))
STATE

# The run has a finish record now; the start marker's job is done.
rm -f "$running"

# Keep the last $KEEP runs of this name. Old logs are the first thing to fill a
# data disk that also holds the archive.
ls -1t "$dir"/*.log 2>/dev/null | grep -v '/latest\.log$' | tail -n "+$((KEEP + 1))" \
  | while IFS= read -r old; do rm -f "$old"; done

exit "$code"
