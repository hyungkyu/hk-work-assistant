#!/usr/bin/env bash
# Install and start the dev -> prod carrier timer for the current user.
# Safe to re-run: it overwrites the units and restarts the timer.

set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
units="$HOME/.config/systemd/user"
mkdir -p "$units"

for u in hkwa-incoming.service hkwa-incoming.timer; do
  sed "s#%h/Documents/ChatGPT/RLWRLD workspace#$root#g" \
    "$root/deploy/systemd/$u" > "$units/$u"
done

chmod +x "$root/scripts/incoming-tick.sh"
mkdir -p "$root/incoming/applied" "$root/incoming/failed"

# gh authenticates from GH_TOKEN in the interactive shell. A systemd user
# service does not inherit that shell's environment, so the push would fail
# with no obvious cause. Hand the variable to the user manager from here,
# where it is already set, rather than writing the token to a file.
if [ -n "${GH_TOKEN:-}" ]; then
  systemctl --user import-environment GH_TOKEN
  echo "GH_TOKEN handed to the user manager."
else
  echo "GH_TOKEN not set in this shell; the timer's push will use whatever" \
       "credential helper git finds. Watch for outcome=push-failed."
fi

systemctl --user daemon-reload
systemctl --user enable --now hkwa-incoming.timer
systemctl --user restart hkwa-incoming.timer

echo
systemctl --user list-timers hkwa-incoming.timer --no-pager
echo
echo "run one tick now:   systemctl --user start hkwa-incoming.service"
echo "see what it did:    cat '$root/incoming/last-run.json'"
