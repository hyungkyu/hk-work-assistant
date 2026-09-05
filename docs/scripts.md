# `scripts/`

One section per file. Each says what the script does, who invokes it, what it
refuses to do and why, and whether it is safe to run twice.

`scripts/` holds eight shell scripts and one Markdown file. The Markdown file,
`local-work-prompt.md`, is not a script — it is data read by `wake-local.sh`, and
it is described at the end.

**Two scripts have no caller anywhere in this repository:**
`prepare-admin-config.sh` and `prepare-data-disk.sh`. Nothing runs them —
not a systemd unit, not another script, and no document outside this one names
them. Both are one-time host setup a person runs by hand. If you are looking for
why a machine is in the state it is in, those two are the steps that leave no
trace of having been taken.

Quick index:

| Script | Invoked by | Re-runnable |
|---|---|---|
| `incoming-tick.sh` | `hkwa-incoming.service` (timer, ~3 min) | yes, by design |
| `wake-local.sh` | `hkwa-wake.service` (timer, ~3 min) | yes, by design |
| `install-incoming-timer.sh` | a person, from an interactive shell | yes, stated in the script |
| `apply-incoming.sh` | a person | yes |
| `install-slack-secret.sh` | a person | yes, overwrites the secret |
| `prepare-admin-config.sh` | **nobody** — a person, by hand | yes |
| `prepare-data-disk.sh` | **nobody** — a person, with `sudo` | no, refuses after the first run |
| `worklog-local.sh` | a person, as the CLI entry point | yes |

---

## `incoming-tick.sh`

**What it does.** One tick of the cloud → production carrier. Takes a lock,
applies every `incoming/*.patch` with `git am --3way`, runs the test suite, and
pushes to `origin/main` only if the suite was green. Writes
`incoming/last-run.json` on every path, including the paths that do nothing:
"That file is the cloud side's only way to see what this machine did, so it is
written even on the paths that do nothing" (`:9-11`).

**Who invokes it.** `hkwa-incoming.service`, driven by `hkwa-incoming.timer`
roughly every three minutes (`deploy/systemd/hkwa-incoming.service:12`). Also run
once, directly, by `install-incoming-timer.sh:60` to prove the unit works.

**What it refuses, and why.**

- **A second concurrent tick.** `flock -n 9` on `incoming/.tick.lock`, else
  `outcome=busy` (`:39-44`). "A tick that overlaps the previous one would apply
  a patch on top of a tree the previous tick is still moving" (`:37-38`).
- **A dirty worktree.** `git status --porcelain --untracked-files=no` non-empty
  → `outcome=refused` (`:52-56`). Mixing an unfinished local edit with an
  incoming patch is how "what is running" stops being knowable.
- **Any branch but `main`** → `outcome=refused` (`:58-63`).
- **A history it cannot fast-forward.** `git fetch origin main` or
  `git merge --ff-only origin/main` failing → `outcome=refused` (`:65-69`).
- **Continuing past a failed patch.** On `git am` failure it aborts, moves that
  patch to `incoming/failed/`, and stops — "later patches not attempted"
  (`:71-82`).
- **Pushing something it could not test.** With no Python interpreter it records
  `outcome=untested` and pushes nothing: "A missing interpreter is a reason not
  to push, not a reason to push untested" (`:84-94`). A red suite gives
  `outcome=tests-failed` and no push (`:96-101`).

**Safe to re-run?** Yes — it is built to run every three minutes forever. Every
exit path is `exit 0` (`:34`), so systemd always sees success; the real result is
the `outcome` field in `incoming/last-run.json`. Every `outcome` value and the
tree state it leaves behind is tabulated in `docs/dev-prod-split.md`, along with
two gaps worth knowing before you trust the file.

---

## `wake-local.sh`

**What it does.** Wakes one headless Claude Code session for one board item that
only the production machine can do. Picks the oldest non-archived item whose
`status` is `ready` and whose `assigned_to` equals `$WAKE_EXECUTOR` (default
`local`), renders `local-work-prompt.md` with that item's id, and runs
`claude -p` under `timeout(1)` with a bounded tool allowlist. Writes
`incoming/last-wake.json` on every path and a full transcript to
`$APP_CONFIG_ROOT/cowork/logs/wake-<UTC stamp>-<item_id>.log`.

**Who invokes it.** `hkwa-wake.service`, driven by `hkwa-wake.timer` roughly
every three minutes (`deploy/systemd/hkwa-wake.service:8`).

**What it refuses, and why.**

- **A second concurrent session.** `flock -n 8` on `incoming/.wake.lock`, else
  `outcome=busy` (`:49-54`). "A second session started while the first is
  mid-edit would fight it over the same working tree" (`:47-48`).
- **More than one item per wake.** The picker returns exactly one id
  (`:81-91`) — "one item per wake, the oldest ready one" (`:14`).
- **Running with no `claude` on `PATH`** → `outcome=no-claude` (`:56-60`), rather
  than failing silently inside the unit.
- **Running with no board** → `outcome=no-board` (`:62-66`), and with no prompt
  file → `outcome=no-prompt` (`:109-113`).
- **Blanket permission.** It passes an explicit `--allowedTools` list rather
  than skipping permission checks — "A bounded allowlist rather than skipping
  permission checks outright" (`:100`).
- **An unbounded session.** `timeout "$wake_timeout"` (default 3600s) caps it,
  and 124 is reported as `outcome=timeout` (`:118-126`).

It notably does **not** refuse a malformed board: the picker swallows every
exception and exits silently (`:74-76`), so a corrupt `items.json` is reported as
`outcome=idle`, indistinguishable from an empty queue. That is deliberate — "the
next tick is three minutes away" (`:68-69`).

**Safe to re-run?** Yes, with two caveats a reader must know:

1. The allowlist and timeout are set at `:102-103` and `$APP_CONFIG_ROOT/wake-local.env`
   is sourced at `:105`, i.e. **after** them. That file lives outside this
   repository and can widen the allowlist arbitrarily.
2. Re-running does not by itself avoid waking a second session for the same
   item. The lock only covers the time a session is running. What prevents a
   duplicate is the session's own first action — `worklog work update … --status
   in_progress` (`local-work-prompt.md:15-23`) — so the guarantee lives in the
   prompt, not in the script.

Both points, and every `outcome` value, are in `docs/dev-prod-split.md`. The
authority question this script raises is recorded in `docs/cowork-mailbox.md`,
Part 3.

---

## `install-incoming-timer.sh`

**What it does.** Installs and starts both timers for the current user. Copies
the four units from `deploy/systemd/` into `$HOME/.config/systemd/user`,
substituting the hard-coded `%h/Documents/ChatGPT/RLWRLD workspace` path for
wherever the repository actually is (`:17-18`); makes the two tick scripts
executable; creates `incoming/applied` and `incoming/failed`; hands `PATH` and
`GH_TOKEN` to the user manager; enables and restarts both timers; then runs one
tick and prints `incoming/last-run.json`.

**Who invokes it.** A person, from an interactive shell. The shell matters: "A
systemd user service does not inherit the interactive shell's environment. `gh`
reads `GH_TOKEN` from it, and the wake script needs `claude` on `PATH`; both
would fail with no obvious cause. Hand them over from this shell, where they are
already set, rather than writing anything to a file" (`:24-27`).

**What it refuses, and why.** Very little — it is an installer, and it warns
instead of stopping. With no `GH_TOKEN` it says the push will fall back to
whatever credential helper git finds and to "Watch for outcome=push-failed"
(`:29-35`). With no `claude` on `PATH` it says `hkwa-wake` will record
`outcome=no-claude` and wake nothing (`:37-42`). It skips any unit file that is
not present rather than failing (`:16`, `:46`).

Its last act is the interesting one. It starts one tick and checks that
`last-run.json` appeared (`:59-66`), because "Installing a unit is not evidence
it runs. The first version of these units put an unquoted path with a space in
ExecStart, so systemd split it and the service failed before the script could
write its state file — and the only symptom was a state file that never
appeared" (`:54-57`).

**Safe to re-run?** Yes, and the script says so in its own header: "Safe to
re-run: it overwrites the units and restarts the timers" (`:6`). Re-running is
the correct response to a moved repository, a changed unit file, or a
`GH_TOKEN` that was missing the first time.

---

## `apply-incoming.sh`

**What it does.** The manual sibling of `incoming-tick.sh`. Applies every
`incoming/*.patch` with `git am --3way`, moves each to `incoming/applied/` or
`incoming/failed/`, and stops. It does **not** run the tests and does **not**
push: it prints those two commands for the operator to run (`:56-60`).

**Who invokes it.** A person — `docs/dev-prod-split.md` documents it as the
manual path.

**What it refuses, and why.**

- **A dirty worktree**, printing `git status --short` so you can see what is in
  the way (`:23-27`). The header states the reason: "a half-finished local edit
  and an incoming patch must never be resolved by guessing which one was meant"
  (`:9-10`).
- **Any branch but `main`** (`:31-35`).
- **Continuing past a failed patch**: aborts, moves it to `incoming/failed/`,
  prints "stopping. later patches were not attempted", exits 1 (`:46-52`).

Unlike the tick, it runs under `set -euo pipefail` (`:12`) and exits non-zero on
refusal, because a person is reading the output.

**Safe to re-run?** Yes. Applied patches have been moved out of `incoming/`, so
a second run either finds nothing ("incoming/: nothing to apply", `:19`) or
picks up patches that arrived since. After a failure, fix the conflict or remove
the patch from `incoming/failed/` before re-running.

---

## `install-slack-secret.sh`

**What it does.** Prompts for a Slack user OAuth token and the expected team ID,
and writes `secrets/slack.env` containing `SLACK_USER_TOKEN` and
`SLACK_EXPECTED_TEAM_ID` at mode `0600`.

**Who invokes it.** A person — `docs/setup.md` names it, and two runtime errors
point at it when the token is missing (`src/rlwrld_worklog/cli.py:289`,
`src/rlwrld_worklog/slack_collector.py:662`).

**What it refuses, and why.**

- **A token that does not begin with `xoxp-`** → exits 1 with "Expected a user
  OAuth token beginning with `xoxp-`" (`:12-15`). A bot token (`xoxb-`) reaches
  a different set of conversations, so accepting one would silently change what
  gets collected.
- **Leaving the token on screen or in shell history.** It is read with
  `read -r -s` (`:10`), never echoed, and never passed as an argument.
- **Leaving a partial file behind.** `umask 077`, write to a `mktemp` file,
  `chmod 600`, then `mv` into place, with a `trap` removing the temporary file
  on any early exit (`:8`, `:19-23`).

The team ID defaults to `T077WTVBF8W` if you press enter (`:17-18`).

**Safe to re-run?** Yes. It overwrites `secrets/slack.env` atomically, which is
how you rotate the token. Re-running is the rotation procedure; there is no
separate one.

---

## `prepare-admin-config.sh`

**What it does.** Creates the admin configuration directory and its
`credentials/` subdirectory at mode `0700` under `umask 077`, then prints the
backoffice URL where the emergency super-administrator password is created
(`:7-11`). The location is `$APP_CONFIG_HOST_ROOT`, falling back to
`$XDG_CONFIG_HOME/hk-work-assistant`, falling back to
`$HOME/.config/hk-work-assistant` (`:4-5`).

**Who invokes it.** **Nobody.** No systemd unit, no script, and no document in
this repository references it. A person runs it once when setting up a host.

**What it refuses, and why.** Nothing explicitly, but it is `set -euo pipefail`
(`:2`) and uses `install -d -m 0700` rather than a bare `mkdir`, so the
directory cannot come into existence group- or world-readable. It holds
credentials and the work board.

**Safe to re-run?** Yes. `install -d` on an existing directory re-applies the
mode and changes nothing else. Running it again is a cheap way to repair
permissions that drifted.

---

## `prepare-data-disk.sh`

**What it does.** Prepares the 2 TB data disk. Verifies the target device, writes
a GPT label and one ext4 partition labelled `rlwrld-data`, installs
`deploy/data.mount` to `/etc/systemd/system/`, enables it, and creates the
`/data/rlwrld-worklog` tree (`raw/`, `raw/slack/`, `raw/google-calendar/`,
`raw/github/`, `manifests/`, `exports/`, `logs/`) owned by `hk:hk` at mode `0750`
(`:58-66`). Ends by printing `findmnt` and `lsblk` output so the result is
visible rather than assumed (`:68-69`).

**Who invokes it.** **Nobody.** Like `prepare-admin-config.sh`, no unit, script
or document references it. A person runs it once, with `sudo`, on a new machine.

**What it refuses, and why.** This is the most defensive script in the
repository, because its mistake is unrecoverable. Every check exits before
touching anything:

- **Not running as root** (`:18`).
- **The target not being a block device** (`:19`).
- **A model or serial mismatch.** It compares `lsblk` output against the
  hard-coded `ST2000VX017-3CV102` / `WWD534T4` (`:5-6`, `:21-26`). Device names
  are not stable across boots; the serial is. A shifted `/dev/sda` therefore
  aborts instead of repartitioning the wrong disk.
- **A disk that is not empty.** If `lsblk` reports anything other than a single
  `disk` row it fails with "is no longer empty; refusing to repartition it"
  (`:28-30`).
- **A disk that is mounted** (`:32-34`).
- **A partition that never appeared.** After `partprobe` it waits up to ten
  seconds and fails rather than proceeding to `mkfs` on a missing device
  (`:41-46`).

**Safe to re-run?** **No, and deliberately so.** The second run hits the
not-empty check at `:28-30` and stops. That check is the whole safety property:
re-running after a successful run would otherwise destroy the collected data. If
you genuinely need to rebuild the disk, the refusal is what you have to remove
by hand, consciously.

---

## `worklog-local.sh`

**What it does.** Six lines. Resolves the project root, prepends
`.python-packages` and `src` to `PYTHONPATH`, and `exec`s
`python3 -m rlwrld_worklog "$@"`. It is the CLI entry point for a checkout with
no virtualenv installed.

**Who invokes it.** A person — `docs/setup.md` uses it for `google-auth` and
`legacy-drive-download`.

**What it refuses, and why.** Nothing. It is `#!/bin/sh` with `set -eu` (`:2`),
so an unset variable or a failing command stops it rather than running the CLI
in a half-built environment. `exec` means the CLI's own exit code is the
script's — nothing is swallowed.

**Safe to re-run?** Yes. It carries no state of its own; whether a given
invocation is safe depends entirely on the subcommand you pass it.

---

## `local-work-prompt.md` (not a script)

**What it is.** The prompt text `wake-local.sh` hands to the session it starts.
It is read and templated at `wake-local.sh:107-108`, with `{{ITEM_ID}}` and
`{{EXECUTOR}}` substituted. If it is missing or unreadable the wake records
`outcome=no-prompt` and starts nothing (`:109-113`).

**Why it matters here.** Several properties people assume the script enforces
are actually stated only in this file, and hold only insofar as the session
follows them:

- **Claim the item before working** (`:15-23`), which is what stops the next
  timer tick from waking a second session for the same item. The file says so
  itself: "착수 표시가 다음 타이머 틱이 같은 일로 두 번째 세션을 깨우는 것을 막는
  유일한 장치다."
- **An item with empty `detail` and `next_action` is `blocked`, not `done`**
  (`:27-31`) — "지시 없는 항목을 완료로 닫으면 보드는 조용해지지만 일은
  사라진다."
- **Touch no other item, reassign nobody, delete no data, restart no container
  unless the item says to** (`:66-72`).
- **Commit and push after a green suite** (`:57-64`). This is the instruction
  that collides with `cowork.py:269`, where `push` is listed as never
  autonomous. See `docs/cowork-mailbox.md`, Part 3.

Editing this file changes what an unattended session on the production machine
will do, with no code change and no test covering it.

## `deploy-tick.sh`

**What it does.** One deploy tick. Refuses a dirty worktree, fast-forwards to
`origin/main` if it moved, hashes the modules the running container actually
imports, and stops there if they already match `HEAD`. Otherwise
`docker compose build`, `docker compose up -d`, and hash again — reporting
`deployed` only when the running image is byte-identical to the commit.

**Who invokes it.** `hkwa-deploy.service`, driven by `hkwa-deploy.timer` every
ten minutes. Also runnable by hand: `systemctl --user start hkwa-deploy.service`.

**What it refuses and why.** It will not build from a dirty tree, because an
image built from uncommitted work cannot be named afterwards. It will not
report `deployed` on a build it could not verify: `unverified` and
`image-stale` exist precisely so that "the build command succeeded" is never
allowed to stand in for "this is what is running".

**Safe to re-run.** Yes. A tick whose image already matches records `current`
and does nothing.

**State.** `incoming/last-deploy.json`, written on every path. Outcome table in
[dev-prod-split.md](dev-prod-split.md).

## `run-logged.sh`

**What it does.** Runs a command and puts its output under
`/data/rlwrld-worklog/logs/<name>/` — the data disk, which is a connected
folder — instead of wherever the caller happened to redirect it.

```bash
scripts/run-logged.sh backfill-sep -- .venv/bin/worklog daily-collect --since 5d
```

Three files per name: the timestamped log, `latest.log`, and `last.json`
carrying the exit code, the timings, the command, and the last 4 000
characters. `last.json` is written whatever happens, including when the command
could not be started at all — "no file" and "nothing happened" must never look
the same from the other side.

**Who invokes it.** Anyone running an ad-hoc or scheduled collection by hand.
Use it instead of `> /tmp/something.log`: a log in `/tmp` exists only on the
machine that wrote it, and on 2026-09-05 that meant a running backfill could be
followed only by inspecting the archive it was writing.

**What it refuses and why.** A `<name>` that is not a single path segment, so a
log name can never escape the log root. Bad usage exits 64 rather than guessing.

**Safe to re-run.** Yes. Each run gets its own file, named with the second and
the pid so two runs in the same second cannot overwrite one another. The last
30 logs per name are kept (`WORKLOG_LOG_KEEP`); older ones are deleted, because
old logs are the first thing to fill a disk that also holds the archive.

**Exit code** is the command's own, passed through.

## The collection timer — `hkwa-collect`

`deploy/systemd/hkwa-collect.{service,timer}`, installed by
`install-incoming-timer.sh`. Fires at **01:00 Asia/Seoul** and runs

```
scripts/run-logged.sh daily-collect -- .venv/bin/worklog daily-collect
```

with **no `--source` flags**, so it collects all five sources. Naming sources
in the unit is exactly how the previous batch fell three sources behind the
code without anyone noticing.

It replaces a batch that lived in the system unit directory, which the cloud
side can neither read nor repair. This one lives in `deploy/systemd/`, is
versioned with the code, and writes its log to
`/data/rlwrld-worklog/logs/daily-collect/` where it can be read from either
side. The old system unit or cron entry must be disabled, or both will run and
contend for the same lock.

`Persistent=true` so a machine asleep at 01:00 collects when it wakes rather
than skipping the day, and the installer enables lingering so the timer fires
whether or not anyone is logged in.
