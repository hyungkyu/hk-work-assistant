"""Drop everybody the roster sheet no longer lists, and keep what a person decided.

HK, 2026-10-12: 그냥 리셋해. 명단, 새로 가져오는 걸로 하면 안돼?

Yes, and it is the right instinct: the sheet is the master and these tables
are derived from it. Three days of trying to classify 191 leftover rows by
SQL kept getting the criterion wrong, because the leftovers are the residue
of two key changes (10-08's emails, 10-12's 사번) and their shape is not
one thing. Deleting what the sheet does not name is simpler and correct.

What makes it not quite `TRUNCATE`:

* People still on the sheet are kept as they are. The 2026-10-12 sync
  already attached a 사번 to every one of them without changing a single
  person_id, so their history stays attached.

* `answer_pairs` is `ON DELETE CASCADE` on the person. That table holds the
  Q&A pairs HK chose by hand in the review screen -- the one thing in this
  database nobody can rebuild. A plain delete would erase those choices
  with no error. A person carrying any human decision is therefore never
  deleted here; they are reported by name instead.

* The same for a former account somebody mapped to a person by answering
  the 미확인 계정 queue (`org_identity.origin = 'resolved'`, and
  `org_unmapped_account.resolved_person_id`). Those are answers to a
  question nobody can ask again.

* What *is* derived is deleted with the person and rebuilt: their digest
  rows and conversation blocks. The days those digests covered are
  returned, so the caller rebuilds them and the activity that used to land
  on a stale copy now lands on the person it belongs to.

Dry-run by default. Nothing is written without `apply=True`, and the whole
change is one transaction -- a reset that half-ran would be harder to read
than the mess it replaced.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# A person is on the sheet if the latest observation of any tab lists them
# as active or retired. `absent_from_sheet` is itself a state row that the
# sync writes for everybody it no longer sees -- which is why "has a row in
# the latest observation" was the wrong test, and counted every leftover as
# present.
_GONE_SQL = """
WITH latest AS (
    SELECT source, max(observation_id) AS observation_id
      FROM roster_observation
     GROUP BY source
),
listed AS (
    SELECT DISTINCT state.person_id
      FROM org_person_state state
      JOIN latest ON latest.observation_id = state.observation_id
     WHERE state.status IN ('active', 'retired')
)
SELECT person.person_id, person.name
  FROM org_person person
 WHERE person.person_id NOT IN (SELECT person_id FROM listed)
 ORDER BY person.name, person.person_id
"""

# Everything a person decided that points at somebody. Each is a reason to
# keep that somebody.
_DECISIONS_SQL = """
SELECT person_id, 'Q&A 짝 선택' AS reason, count(*)
  FROM answer_pairs
 WHERE person_id = ANY(%(ids)s) AND decided_at IS NOT NULL
 GROUP BY person_id
UNION ALL
SELECT person_id, '미확인 계정 연결', count(*)
  FROM org_identity
 WHERE person_id = ANY(%(ids)s) AND origin = 'resolved'
 GROUP BY person_id
UNION ALL
SELECT resolved_person_id, '미확인 계정 답변', count(*)
  FROM org_unmapped_account
 WHERE resolved_person_id = ANY(%(ids)s)
 GROUP BY resolved_person_id
"""


@dataclass
class ResetResult:
    apply: bool
    on_sheet: int = 0
    gone: int = 0
    deleted: int = 0
    kept: list[dict[str, Any]] = field(default_factory=list)
    digest_rows: int = 0
    blocks: int = 0
    identities: int = 0
    days_to_rebuild: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "apply": self.apply,
            "on_sheet": self.on_sheet,
            "gone_from_sheet": self.gone,
            "deleted": self.deleted,
            "kept_because_a_person_decided_something": self.kept,
            "digest_rows_removed": self.digest_rows,
            "blocks_removed": self.blocks,
            "identities_removed": self.identities,
            "days_to_rebuild": self.days_to_rebuild,
        }


def reset(database_url: str, *, apply: bool = False) -> ResetResult:
    import psycopg

    with psycopg.connect(database_url) as connection:
        connection.autocommit = False
        with connection.cursor() as cursor:
            result = reset_in(cursor, apply=apply)
        if apply and result.deleted:
            connection.commit()
        else:
            connection.rollback()
    return result


def reset_in(cursor, *, apply: bool = False) -> ResetResult:
    """The reset, inside a transaction the caller owns.

    Split out so the caller decides whether it commits. That is what lets it
    be tested against a shared database: "delete everybody the sheet does
    not list" run for real would take every other test's fixtures with it,
    so the test runs this and rolls back.
    """
    result = ResetResult(apply=apply)
    cursor.execute("SELECT count(*) FROM org_person")
    total = int(cursor.fetchone()[0])

    cursor.execute(_GONE_SQL)
    gone = cursor.fetchall()
    result.gone = len(gone)
    result.on_sheet = total - len(gone)
    ids = [row[0] for row in gone]
    names = {row[0]: row[1] for row in gone}

    blocked: dict[str, list[str]] = {}
    if ids:
        cursor.execute(_DECISIONS_SQL, {"ids": ids})
        for person_id, reason, count in cursor.fetchall():
            blocked.setdefault(person_id, []).append(f"{reason} {count}건")
    result.kept = [
        {"person_id": person_id, "name": names[person_id], "why": reasons}
        for person_id, reasons in sorted(blocked.items(), key=lambda kv: names[kv[0]])
    ]

    doomed = [person_id for person_id in ids if person_id not in blocked]
    result.deleted = len(doomed)
    if not doomed:
        return result

    cursor.execute(
        "SELECT DISTINCT day FROM person_day_digest WHERE person_id = ANY(%s) ORDER BY day",
        (doomed,),
    )
    result.days_to_rebuild = [row[0].isoformat() for row in cursor.fetchall()]
    cursor.execute(
        "SELECT count(*) FROM person_day_digest WHERE person_id = ANY(%s)", (doomed,)
    )
    result.digest_rows = int(cursor.fetchone()[0])
    cursor.execute(
        "SELECT count(*) FROM conversation_blocks WHERE person_id = ANY(%s)", (doomed,)
    )
    result.blocks = int(cursor.fetchone()[0])
    cursor.execute("SELECT count(*) FROM org_identity WHERE person_id = ANY(%s)", (doomed,))
    result.identities = int(cursor.fetchone()[0])

    if not apply:
        return result

    # Children first, in the order the foreign keys require. The cascades on
    # blocks and undecided pairs would do the same, but spelling it out is
    # what makes the counts above true.
    for statement in (
        "DELETE FROM person_day_digest WHERE person_id = ANY(%s)",
        "DELETE FROM answer_pairs WHERE person_id = ANY(%s)",
        "DELETE FROM conversation_blocks WHERE person_id = ANY(%s)",
        "DELETE FROM org_identity WHERE person_id = ANY(%s)",
        "DELETE FROM org_person_state WHERE person_id = ANY(%s)",
        "DELETE FROM org_person WHERE person_id = ANY(%s)",
    ):
        cursor.execute(statement, (doomed,))
    return result
