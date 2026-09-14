# Working on this project as an agent

Read this once. It is everything a new session needs in order to be useful
here, and it deliberately does not give you a name, a persona, or a backstory
to maintain.

HK, 2026-09-14:

> 각 개발자들에게 정체성을 부여할 필요는 없고, 각각이 빠르게 온보딩할 수
> 있게 매뉴얼을 두되, 주/부만 있으면 될 것 같아. 다만, 이들하고 소통 창구는
> 여전히 업무 목록이지.

So there are two roles and no cast of characters.

## The two roles

**주 (primary).** Holds the programme. Decides what the phases are and what
belongs in each, writes and archives board items, judges whether something is
finished, and reports to HK. One at a time. Today that is `mori`.

**부 (secondary).** Does the work an item describes and reports on that item.
Any number at once, and they are interchangeable by design: a secondary is
identified by the executor name on the item it is holding, not by a
personality. A session that finishes its item and picks up another is the
same role either way.

`ari` sits outside both: HK's joker, played onto an item when he wants it
there and holding nothing the rest of the time. The board treats it as a
valid holder and the audit does not flag it; it is not a seat somebody
occupies.

That is the whole org. Earlier versions of this project gave every agent a
name and a speciality (development, operations, analysis). That structure
described who was talking rather than what was being done, and the board —
the only thing anybody could actually check — never reflected it. It is gone.

## The board is the channel

Everything between the primary and a secondary goes through the work board,
and nothing important goes anywhere else. Not chat scrollback, not a handoff
file somebody has to remember to open, not a message in a session that ends.

Concretely, if you are a secondary:

1. **Take only what is assigned to you** and is `ready` or `in_progress`.
   Do not invent items, and do not start something because it looks useful.
   Work nobody asked for is work nobody checked.
2. **Move it to `in_progress` when you start**, with `progress_summary` kept
   to a few lines of what is true now — not a log of what you tried.
3. **`next_action` is the one field the primary reads first.** Keep it
   accurate or empty. A stale next action is worse than none.
4. **Close with evidence.** `done` requires a commit sha, a log path under
   `/data/rlwrld-worklog`, or a manifest path. The board refuses a closure
   that names nothing checkable, and it is right to.
5. **`waiting` and `blocked` mean a person has to act.** Say who and on
   what in `blocker`. A blocked item with no named decision is a stall
   nobody can clear.

If you are the primary, the reverse: write items that a session with no
memory of this conversation could pick up cold, and archive with a reason.

## What earns a board item

An item is a piece of work somebody will check the completion of. That is
the whole test, and it excludes most of what accumulated on this board
before the 2026-09-14 reset:

* **Yes:** a defect with a reproduction, a capability HK asked for, a
  migration, a phase's remaining work.
* **No:** an idea, an observation, a "we should look at" with no owner and
  no check, a note about a thing already fixed, a duplicate of an item
  under a different title.

When in doubt, the deciding question is not "is this true?" but "who would
be able to say this is done, and how?" If there is no answer, it is not an
item.

## Phases

Each item may carry a phase — `P0`, `P1`, `P2` … — naming which piece of
the programme it belongs to. It is a separate axis from priority: priority
says how soon, phase says which piece, and they disagree constantly.

The phases are HK's, in his words (2026-09-14), and they are ordered:

* **P0** — 누가 무슨 일을 하고 있고, 언제 마무리 되고, 뭐가 블로커이고, 그
  사실이 투명하게 준실시간 공유되고 실행되는 것.
* **P1** — 슬랙, 노션, 깃헙, 슬럼, 구캘 등 우리 회사의 누가 무엇을 하고
  있는지 기록되고 디비화되는 것.
* **P2** — 실제로 보고 싶은 리포트의 다양한 자세한 생성.

Read them as a dependency chain, not a schedule: P2 built on a P1 with holes
produces confident reports about data that is not there, and P1 collected
while P0 is blind means nobody can tell whether the collection is running.
Work in a later phase is not blocked by an earlier one, but a claim that an
earlier phase is finished has to survive the audit.

An item with no phase is one nobody has placed yet, which is a visible
state and not a default. Board columns read P0 first and unplaced last.

## What to trust

In this order, and the order matters:

1. **The code and the data.** What the repository does, and what the
   collected record actually contains.
2. **The receipts.** `incoming/last-*.json`, run logs under
   `/data/rlwrld-worklog`, the board's own append-only history.
3. **The board.** Accurate when items 1 and 2 back it, and a half-hourly
   audit (`docs/board-audit.md`) reports where it has drifted.
4. **Anything written in prose, including this file.** Documents go stale;
   a document that disagrees with the code is wrong about the code.

Nothing here is a substitute for reading the thing you are about to change.

## The rules that are not negotiable

* **No collected data enters the repository.** See `CONTRIBUTING.md`; this
  is the one rule with no exceptions.
* **Do not rewrite history.** The board's history file and the handoff
  records are append-only. Correct a mistake with a new entry.
* **Do not hand a person a command to run.** If something has to happen
  regularly, it goes in a batch under `scripts/` with a timer in
  `deploy/systemd/`. A step that only works when somebody remembers to
  type it is not finished.
* **Do not overwrite work you did not do.** A dirty worktree, an unpushed
  commit, an item somebody else moved — stop and say so.
* **Say when you do not know.** A guess presented as a finding is the most
  expensive thing you can produce here; this project has already lost days
  to a false green that reported `ok` while writing nothing.

## Where to read next

| You need | Read |
| --- | --- |
| Run the tests, first commit | `CONTRIBUTING.md` |
| What the system is | `docs/architecture.md` |
| The board's fields and routes | `docs/work-board-reference.md` |
| How the board is audited | `docs/board-audit.md` |
| The nightly collection | `docs/daily-collection.md` |
| What is collected and how | `docs/collection-rules.md` |
| What must never be stored | `docs/data-policy.md` |
