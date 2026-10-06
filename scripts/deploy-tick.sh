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

# Where the record of what was deployed lives.
#
# HK, 2026-10-06: 배포 히스토리를 알아야 내가 딴 이야기를 안할거같아.
#
# Under the data root, not in the repository: the app container mounts that
# read-only and does not mount this checkout, so this is the only place a
# file written here can be read by the 릴리즈 노트 screen. It is also the
# honest home for it -- a deploy log is a record of what happened, not source.
deploy_dir="${RAW_ARCHIVE_HOST_ROOT:-/data/rlwrld-worklog}/deploy"

finish() {
  finished=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  subject=$(git log -1 --format=%s "$head_sha" 2>/dev/null || true)
  line=$(
    STARTED="$started" FINISHED="$finished" OUTCOME="$outcome" HEAD_SHA="$head_sha" \
    SUBJECT="$subject" DETAIL="$detail" python3 -c '
import json, os
print(json.dumps({
    "started_at": os.environ["STARTED"],
    "finished_at": os.environ["FINISHED"],
    "outcome": os.environ["OUTCOME"],
    "head": os.environ["HEAD_SHA"],
    "subject": os.environ["SUBJECT"],
    "detail": os.environ["DETAIL"],
}, ensure_ascii=False))' 2>/dev/null
  )
  # The repository copy stays exactly as it was -- scripts and sessions read
  # it, and this change is not the place to move them.
  if [ -n "$line" ]; then
    printf '%s\n' "$line" > "$state"
  else
    printf '{"started_at":"%s","finished_at":"%s","outcome":"%s","head":"%s","detail":""}\n' \
      "$started" "$finished" "$outcome" "$head_sha" > "$state"
  fi

  if [ -n "$line" ] && mkdir -p "$deploy_dir" 2>/dev/null; then
    printf '%s\n' "$line" > "$deploy_dir/current.json" 2>/dev/null || true
    # Appended only when something changed. This batch runs every ten
    # minutes and almost always finds the running image already correct;
    # logging those would bury the four lines a day that mean something
    # under a hundred that do not.
    previous=$(tail -n 1 "$deploy_dir/deploy-log.jsonl" 2>/dev/null || true)
    was=$(
      PREVIOUS="$previous" python3 -c '
import json, os
try:
    row = json.loads(os.environ["PREVIOUS"])
    print(row.get("head", "") + " " + row.get("outcome", ""))
except Exception:
    print("")' 2>/dev/null
    )
    if [ "$was" != "$head_sha $outcome" ]; then
      printf '%s\n' "$line" >> "$deploy_dir/deploy-log.jsonl" 2>/dev/null || true
    fi
  fi
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

# The verdict, before the build.
#
# HK, 2026-09-30: 업무목록 -> 깃헙 -> 데브 -> 프로덕션. Until now the last
# arrow had no condition on it: this batch would build whatever was on main,
# and the integration run that says whether main works was a report nobody
# was obliged to read. A gate that only reports is a gate that is open.
#
# So production deploys a commit that 통합데브 called green, and nothing
# else. A commit with no verdict yet is not an error -- integration runs on
# its own timer and will reach it -- so this waits rather than failing, and
# says which commit it is waiting on.
verdict="incoming/last-integration.json"
if [ -f "$verdict" ]; then
  verdict_head=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("head",""))' "$verdict" 2>/dev/null)
  verdict_outcome=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("outcome",""))' "$verdict" 2>/dev/null)
else
  verdict_head=""
  verdict_outcome="missing"
fi

if [ "$verdict_head" != "$head_sha" ]; then
  outcome="awaiting-verification"
  detail="통합데브 has not tested $head_sha yet (last verdict: ${verdict_outcome:-none} on ${verdict_head:-nothing})"
  finish
fi
if [ "$verdict_outcome" != "green" ]; then
  outcome="blocked"
  detail="통합데브 says $head_sha is $verdict_outcome; not deploying it"
  finish
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

# ---------------------------------------------------------- after the build
#
# Two things that used to need a person typing a command, and that a person
# typing a command is the wrong answer to: systemd units that changed in the
# repo, and migrations the new code needs.

units_note=""
unit_src="$root/deploy/systemd"
unit_dst="$HOME/.config/systemd/user"
if [ -d "$unit_src" ] && [ -d "$unit_dst" ]; then
  changed=0
  for unit in "$unit_src"/*.service "$unit_src"/*.timer; do
    [ -f "$unit" ] || continue
    name=$(basename "$unit")
    if ! cmp -s "$unit" "$unit_dst/$name"; then
      # `sed` because the units carry %h, which systemd expands but cmp does
      # not; copying verbatim is what the installer does too.
      cp -f "$unit" "$unit_dst/$name" && changed=1
    fi
  done
  if [ "$changed" = 1 ]; then
    systemctl --user daemon-reload >/dev/null 2>&1
    for t in "$unit_src"/*.timer; do
      [ -f "$t" ] || continue
      systemctl --user enable --now "$(basename "$t")" >/dev/null 2>&1
    done
    units_note=" units-reloaded"
  fi
fi

# Pending migrations. A new timer or a new column arriving with the code and
# then waiting for somebody to notice is how this system spent 2026-09-14:
# the schema was three migrations behind the image and every symptom looked
# like something else.
#
# Applying them is opt-in, because HK's rule is that a schema change is asked
# separately (2026-09-11). `HKWA_AUTO_MIGRATE=1` in collect.env turns the ask
# into a standing yes; without it this reports and changes nothing, which is
# still better than silence.
migrate_note=""
config_root="${APP_CONFIG_ROOT:-$HOME/.config/hk-work-assistant}"
if [ -f "$config_root/collect.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$config_root/collect.env"
  set +a
fi
if [ -n "${DATABASE_URL:-}" ] && [ -x .venv/bin/worklog ]; then
  pending=$(.venv/bin/worklog ledger-migrate 2>/dev/null \
    | sed -n 's/.*"pending": \[\([^]]*\)\].*/\1/p')
  if [ -n "$pending" ] && [ "$pending" != "" ]; then
    if [ "${HKWA_AUTO_MIGRATE:-0}" = "1" ]; then
      if .venv/bin/worklog ledger-migrate --apply >/dev/null 2>&1; then
        migrate_note=" migrated:$pending"
      else
        migrate_note=" MIGRATE-FAILED:$pending"
      fi
    else
      migrate_note=" PENDING-MIGRATIONS:$pending (set HKWA_AUTO_MIGRATE=1 in collect.env to apply)"
    fi
  fi
fi

verdict=$(compare "$after" "$committed")
case "$verdict" in
  MATCH*)
    outcome="deployed"
    detail="$verdict at $head_sha$units_note$migrate_note"
    # A new build is exactly when someone wants to know whether the fix worked.
    # Read-only, and its own failure never fails the deploy.
    bash scripts/verify-tick.sh >/dev/null 2>&1 || true
    ;;
  *)
    # Built, restarted, and still not the commit. Saying "deployed" here is the
    # exact false report this batch was written to end.
    outcome="image-stale"
    detail="$verdict$units_note$migrate_note"
    ;;
esac
finish
