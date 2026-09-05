#!/usr/bin/env bash
# Apply the patches the cloud development side left in incoming/, then push.
#
# The cloud side cannot push to GitHub: its git traffic goes through a sandbox
# proxy that only injects a credential for repositories attached to the session.
# So the cloud side develops, tests, commits, and writes `git format-patch`
# output into incoming/. This machine is the only side that can push.
#
# Refuses to run on a dirty worktree: a half-finished local edit and an
# incoming patch must never be resolved by guessing which one was meant.

set -euo pipefail

cd "$(dirname "$0")/.."
shopt -s nullglob

patches=(incoming/*.patch)
if [ ${#patches[@]} -eq 0 ]; then
  echo "incoming/: nothing to apply"
  exit 0
fi

if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  echo "worktree is dirty. commit or stash first, then run again." >&2
  git status --short --untracked-files=no >&2
  exit 1
fi

mkdir -p incoming/applied incoming/failed

branch=$(git rev-parse --abbrev-ref HEAD)
if [ "$branch" != "main" ]; then
  echo "on branch '$branch', expected main." >&2
  exit 1
fi

git fetch origin main
git merge --ff-only origin/main

applied=0
for p in "${patches[@]}"; do
  echo "== $p"
  if git am --3way "$p"; then
    mv "$p" "incoming/applied/$(basename "$p")"
    applied=$((applied + 1))
  else
    git am --abort || true
    mv "$p" "incoming/failed/$(basename "$p")"
    echo "FAILED to apply $(basename "$p") - moved to incoming/failed/." >&2
    echo "stopping. later patches were not attempted." >&2
    exit 1
  fi
done

echo "applied $applied patch(es)."
echo
echo "run the tests before pushing:"
echo "    .venv/bin/python -m pytest -q"
echo "then:"
echo "    git push origin main"
