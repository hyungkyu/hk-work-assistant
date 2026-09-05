#!/usr/bin/env bash
# Build and restart the services when the working tree has moved, then prove
# the running image is the commit. Run by hkwa-deploy.timer.
#
# This is a batch, not a judgement. Pulling, building, restarting and hashing
# are deterministic; nothing here decides anything. It exists because the
# alternative was waiting for a session to wake up, and because on 2026-09-04
# a deploy went out from an uncommitted working tree and nobody could tell:
# "도는 것 = 커밋된 것" had never once been checked.
#
# Writes incoming/last-deploy.json on every path, including the paths that do
# nothing, so idle is never confused with broken.

set -uo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$root" || exit 0

state="incoming/last-deploy.json"
lock="incoming/.deploy.lock"
mkdir -p incoming

started=$(date -u +%Y-%m-%dT%H:%M:%SZ)
outcome="idle"
detail=""
head_sha=$(git rev-parse --short HEAD 2>/dev/null || echo unknown)

finish() {
  printf '{"started_at":"%s","finished_at":"%s","outcome":"%s","head":"%s","detail":%s}\n' \
    "$started" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$outcome" "$head_sha" \
    "$(printf '%s' "$detail" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))' 2>/dev/null || echo '""')" \
    > "$state"
  exit 0
}

exec 7>"$lock"
if ! flock -n 7; then
  outcome="busy"
  detail="a previous deploy tick is still running"
  finish
fi

if ! command -v docker >/dev/null 2>&1; then
  outcome="no-docker"
  detail="docker is not on PATH for the user manager"
  finish
fi

# A build from a dirty tree is exactly the thing this batch exists to prevent.
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  outcome="refused"
  detail="worktree is dirty; refusing to build an image nobody can name"
  finish
fi

# The carrier pushes from this machine, so a fetch is usually a no-op. It is
# here for the case where main moved somewhere else.
git fetch -q origin main 2>/dev/null
if [ -n "$(git rev-list HEAD..origin/main --count 2>/dev/null | grep -v '^0$')" ]; then
  if ! git merge -q --ff-only origin/main; then
    outcome="refused"
    detail="origin/main is ahead and would not fast-forward"
    finish
  fi
  head_sha=$(git rev-parse --short HEAD)
fi

# What is actually running, hashed from the import path the app uses -- not
# from /app/src, which is a copy that never executes. Conflating the two on
# 2026-09-04 produced three false "deployed" reports in a row.
running_hashes() {
  docker compose exec -T app python - <<'PY' 2>/dev/null
import hashlib, json, os
import rlwrld_worklog
root = os.path.dirname(rlwrld_worklog.__file__)
out = {}
for dirpath, _, names in os.walk(root):
    for name in names:
        if not (name.endswith(".py") or name.endswith(".html")):
            continue
        path = os.path.join(dirpath, name)
        with open(path, "rb") as handle:
            out[os.path.relpath(path, root)] = hashlib.sha256(handle.read()).hexdigest()
print(json.dumps({"root": root, "files": out}))
PY
}

committed_hashes() {
  git ls-tree -r --name-only HEAD src/rlwrld_worklog \
    | while IFS= read -r path; do
        case "$path" in
          *.py | *.html) ;;
          *) continue ;;
        esac
        printf '%s %s\n' \
          "${path#src/rlwrld_worklog/}" \
          "$(git show "HEAD:$path" | sha256sum | cut -d' ' -f1)"
      done
}

compare() {
  # Both sides arrive in the environment. `python3 - <<PY` already spends stdin
  # on the script itself, so a heredoc script cannot also read piped input --
  # which silently made every comparison read an empty committed side.
  RUNNING="$1" COMMITTED="$2" python3 - <<'PY'
import json, os
running = json.loads(os.environ["RUNNING"])["files"]
committed = {}
for line in os.environ["COMMITTED"].splitlines():
    line = line.strip()
    if not line:
        continue
    rel, digest = line.rsplit(" ", 1)
    committed[rel] = digest
missing = sorted(set(committed) - set(running))
extra = sorted(set(running) - set(committed))
differ = sorted(rel for rel in set(committed) & set(running) if committed[rel] != running[rel])
if missing or extra or differ:
    parts = []
    if differ:
        parts.append("differ: " + ", ".join(differ[:6]))
    if missing:
        parts.append("absent from the image: " + ", ".join(missing[:6]))
    if extra:
        parts.append("in the image but not in HEAD: " + ", ".join(extra[:6]))
    print("MISMATCH " + "; ".join(parts))
else:
    print(f"MATCH {len(committed)} files")
PY
}

committed=$(committed_hashes)
if [ -z "$committed" ]; then
  outcome="refused"
  detail="HEAD lists no module files; refusing to compare against nothing"
  finish
fi

before=$(running_hashes)
if [ -n "$before" ]; then
  case "$(compare "$before" "$committed")" in
    MATCH*)
      outcome="current"
      detail="the running image already is $head_sha"
      finish
      ;;
  esac
fi

if ! build_log=$(docker compose build 2>&1); then
  outcome="build-failed"
  detail=$(printf '%s' "$build_log" | tail -20)
  finish
fi

if ! up_log=$(docker compose up -d 2>&1); then
  outcome="up-failed"
  detail=$(printf '%s' "$up_log" | tail -20)
  finish
fi

# Give the app a moment to accept an exec before asking it what it is running.
for _ in 1 2 3 4 5 6 7 8 9 10; do
  after=$(running_hashes)
  [ -n "$after" ] && break
  sleep 3
done

if [ -z "${after:-}" ]; then
  outcome="unverified"
  detail="built and restarted, but the app container did not answer; what is running is unknown"
  finish
fi

verdict=$(compare "$after" "$committed")
case "$verdict" in
  MATCH*)
    outcome="deployed"
    detail="$verdict at $head_sha"
    ;;
  *)
    # Built, restarted, and still not the commit. Saying "deployed" here is the
    # exact false report this batch was written to end.
    outcome="image-stale"
    detail="$verdict"
    ;;
esac
finish
