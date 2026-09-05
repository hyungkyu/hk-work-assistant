#!/usr/bin/env bash
# Install and start both timers for the current user:
#   hkwa-incoming  carries patches from the cloud side, tests them, pushes
#   hkwa-wake      wakes a local Claude Code session for board work that
#                  only this machine can do
# Safe to re-run: it overwrites the units and restarts the timers.

set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
units="$HOME/.config/systemd/user"
mkdir -p "$units"

for u in hkwa-incoming.service hkwa-incoming.timer \
         hkwa-wake.service hkwa-wake.timer; do
  [ -f "$root/deploy/systemd/$u" ] || continue
  sed "s#%h/Documents/ChatGPT/RLWRLD workspace#$root#g" \
    "$root/deploy/systemd/$u" > "$units/$u"
done

chmod +x "$root/scripts/incoming-tick.sh" "$root/scripts/wake-local.sh" 2>/dev/null || true
mkdir -p "$root/incoming/applied" "$root/incoming/failed"

# A systemd user service does not inherit the interactive shell's environment.
# gh reads GH_TOKEN from it, and the wake script needs `claude` on PATH; both
# would fail with no obvious cause. Hand them over from this shell, where they
# are already set, rather than writing anything to a file.
systemctl --user import-environment PATH
if [ -n "${GH_TOKEN:-}" ]; then
  systemctl --user import-environment GH_TOKEN
  echo "GH_TOKEN handed to the user manager."
else
  echo "GH_TOKEN not set in this shell; the push will use whatever credential" \
       "helper git finds. Watch for outcome=push-failed."
fi

if command -v claude >/dev/null 2>&1; then
  echo "claude found at $(command -v claude)."
else
  echo "claude is NOT on PATH. hkwa-wake will record outcome=no-claude and" \
       "wake nothing until it is."
fi

systemctl --user daemon-reload
for t in hkwa-incoming.timer hkwa-wake.timer; do
  [ -f "$units/$t" ] || continue
  systemctl --user enable --now "$t"
  systemctl --user restart "$t"
done

echo
systemctl --user list-timers 'hkwa-*' --no-pager
echo
echo "run one now:     systemctl --user start hkwa-incoming.service"
echo "                 systemctl --user start hkwa-wake.service"
echo "see what it did: cat '$root/incoming/last-run.json'"
echo "                 cat '$root/incoming/last-wake.json'"
