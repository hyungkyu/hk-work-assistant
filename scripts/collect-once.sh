#!/usr/bin/env bash
# One collection run, with its answer left in a file instead of on a screen.
#
# 2026-09-17: the calendar occurrence sweep shipped and the reconcile table did
# not move. Whether the collection had run at all was the one thing needed to
# tell a broken sweep from a sweep that never fired, and it lived only in a
# terminal I cannot see -- asked for three times, never arriving. That is not
# HK's job to relay. Every other part of this system already writes what it did
# into incoming/; collection was the gap.
#
# Usage: scripts/collect-once.sh <source> [--since DATE] [extra worklog args...]

set -uo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$root" || exit 1

config_root="${APP_CONFIG_ROOT:-$HOME/.config/hk-work-assistant}"
state="incoming/last-collect.json"
mkdir -p incoming

source_name="${1:-}"
if [ -z "$source_name" ]; then
  echo "usage: scripts/collect-once.sh <source> [--since DATE] [args...]" >&2
  exit 64
fi
shift

set -a
# shellcheck disable=SC1091
[ -f "$config_root/collect.env" ] && . "$config_root/collect.env"
set +a

started=$(date -u +%Y-%m-%dT%H:%M:%SZ)
output=$(.venv/bin/worklog daily-collect --source "$source_name" "$@" 2>&1)
status=$?

# The whole output goes to the screen as usual; the file is in addition to it,
# never instead of it.
printf '%s\n' "$output"

STARTED="$started" SOURCE="$source_name" STATUS="$status" OUTPUT="$output" \
  ARGS="$*" python3 - > "$state" <<'STATE'
import json, os, re, datetime

output = os.environ.get("OUTPUT", "")
found = {
    "started_at": os.environ["STARTED"],
    "finished_at": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
    "source": os.environ["SOURCE"],
    "args": os.environ.get("ARGS", ""),
    "exit_status": int(os.environ["STATUS"]),
    "outcome": "ok" if os.environ["STATUS"] == "0" else "failed",
}

# The run prints JSON somewhere in its output. The last complete object wins:
# earlier ones are per-source, the last is the run. A run whose output is not
# JSON at all keeps its tail instead, because an unparsed run still has to say
# what happened.
for line in reversed(output.splitlines()):
    line = line.strip()
    if not line.startswith("{"):
        continue
    try:
        parsed = json.loads(line)
    except ValueError:
        continue
    if isinstance(parsed, dict):
        found["summary"] = parsed
        break
else:
    found["tail"] = output.splitlines()[-20:]

print(json.dumps(found, ensure_ascii=False, indent=2))
STATE

exit "$status"
