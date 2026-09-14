#!/usr/bin/env bash
# Drop a one-time board reset into the outbox, exactly once.
#
# A reset is two dozen decisions taken together. They go through the queue
# like every other board edit -- each leaves a receipt, each archive carries
# its written reason, and each is pinned to the revision the item had when
# the reset was composed, so an item somebody moved in the meantime is
# refused rather than swept up.
#
# Self-disarming: a marker under the config root records which manifests have
# been queued, so this can sit in the batch and do nothing on every run after
# the first. That is the point -- HK does not run it, the batch does, and it
# cannot fire twice because it ran twice.
#
# Usage: queue-reset.sh <manifest.json>

set -uo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
manifest="${1:-}"
[ -n "$manifest" ] || { echo "usage: queue-reset.sh <manifest.json>" >&2; exit 64; }
[ -f "$manifest" ] || { echo "no such manifest: $manifest" >&2; exit 66; }

config_root="${APP_CONFIG_ROOT:-$HOME/.config/hk-work-assistant}"
outbox="$config_root/cowork/outbox/mori"
marks="$config_root/cowork/resets"

name=$(MANIFEST="$manifest" python3 -c '
import json, os
print(json.load(open(os.environ["MANIFEST"], encoding="utf-8"))["name"])
' 2>/dev/null)
[ -n "$name" ] || { echo "manifest has no name: $manifest" >&2; exit 65; }

mkdir -p "$marks" "$outbox"
mark="$marks/$name"
if [ -e "$mark" ]; then
  echo "already queued: $name"
  exit 0
fi

# The mark is written *before* the files, not after. A crash halfway through
# then leaves a reset that has to be inspected by a person, which is the safe
# failure: re-running and double-queueing archives would be worse than
# stopping. The count in the mark says how many were expected.
count=$(MANIFEST="$manifest" python3 -c '
import json, os
print(len(json.load(open(os.environ["MANIFEST"], encoding="utf-8"))["entries"]))
')
printf '%s\tqueued %s entries\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$count" > "$mark"

MANIFEST="$manifest" OUTBOX="$outbox" NAME="$name" python3 - <<'QUEUE'
import json, os, pathlib, datetime

manifest = json.load(open(os.environ["MANIFEST"], encoding="utf-8"))
outbox = pathlib.Path(os.environ["OUTBOX"])
stamp = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
name = os.environ["NAME"]

for index, entry in enumerate(manifest["entries"], start=1):
    # Written to a dot-file and renamed, so the applier never sees a file
    # that is still being written.
    final = outbox / f"{stamp}--{name}--{index:03d}.json"
    temporary = outbox / f".{final.name}"
    temporary.write_text(json.dumps(entry, ensure_ascii=False), encoding="utf-8")
    temporary.rename(final)
print(f"queued {len(manifest['entries'])} entries for {name}")
QUEUE
