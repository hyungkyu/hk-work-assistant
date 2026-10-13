"""One person on both tabs: the company row and their Virtual Lab row, joined.

HK, 2026-10-12: 카이스트의 김동영과 리얼월드의 김동영은 같은 사람이야. 다만
소속이 두개인거지. 버추얼랩은 겸직이라서.

The two tabs key people differently -- a 사번 on the internal tab, a school
email on the external one -- so a person on both became two people, and
their Slurm work (external identities) and their Slack work (internal ones)
were attributed to two different rows.

Joining them keeps both affiliations. Nothing about the person's place on
either tab is rewritten: each tab's state rows stay as they were, under one
person_id, and the chart already reads one state row per tab -- so a joined
person appears under the company and under their lab, which is what 겸직
means. The next sync keeps them joined without any special case, because
the external row's school email is now one of this person's identities and
`resolve_person_id` follows it.

Candidates are found by name, which is why it never acts on its own: two
people can share one (HK, same day: 김준수는 동명이인이야). The caller names
whom to leave out, and the default is to show the list and change nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Somebody listed on the internal tab with a 사번, and somebody listed on the
# external tab, under the same name. "Listed" means active or retired in the
# newest observation of that tab -- `absent_from_sheet` is a state row the
# sync writes for people it no longer sees, and counting it as presence is
# the mistake three hand-written queries made in a row.
_CANDIDATES_SQL = """
WITH latest AS (
    SELECT source, max(observation_id) AS observation_id
      FROM roster_observation GROUP BY source
),
listed AS (
    SELECT state.person_id, state.department_raw, observation.source
      FROM org_person_state state
      JOIN roster_observation observation ON observation.observation_id = state.observation_id
      JOIN latest ON latest.observation_id = state.observation_id
     WHERE state.status IN ('active', 'retired')
),
internal AS (
    SELECT DISTINCT listed.person_id, person.name
      FROM listed JOIN org_person person ON person.person_id = listed.person_id
     WHERE listed.source = 'roster_seed_2'
       AND EXISTS (SELECT 1 FROM org_identity identity
                    WHERE identity.person_id = listed.person_id
                      AND identity.kind = 'employee_no')
),
external AS (
    SELECT DISTINCT listed.person_id, person.name
      FROM listed JOIN org_person person ON person.person_id = listed.person_id
     WHERE listed.source = 'roster_seed_ext'
)
SELECT internal.name, internal.person_id, external.person_id
  FROM internal
  JOIN external ON external.name = internal.name AND external.person_id <> internal.person_id
 ORDER BY internal.name
"""


@dataclass
class MergeResult:
    apply: bool
    pairs: list[dict[str, Any]] = field(default_factory=list)
    excluded: list[str] = field(default_factory=list)
    ambiguous: list[str] = field(default_factory=list)
    days_to_rebuild: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "apply": self.apply,
            "joined" if self.apply else "would_join": self.pairs,
            "excluded_by_you": self.excluded,
            "ambiguous_not_touched": self.ambiguous,
            "days_to_rebuild": self.days_to_rebuild,
        }


def candidates(cursor) -> list[tuple[str, str, str]]:
    cursor.execute(_CANDIDATES_SQL)
    return [(row[0], row[1], row[2]) for row in cursor.fetchall()]


def join_person(cursor, *, keep: str, drop: str) -> list[str]:
    """Move everything about `drop` onto `keep`, then remove `drop`.

    Returns the KST days whose digests must be rebuilt. Digests are derived
    and keyed by (person, day), so the dropped person's rows are deleted
    rather than moved -- moving them would collide with the kept person's
    row for the same day, and a rebuild produces the right single row anyway.
    """
    cursor.execute(
        "SELECT DISTINCT day FROM person_day_digest WHERE person_id = %s", (drop,)
    )
    days = [row[0].isoformat() for row in cursor.fetchall()]

    # Facts about the person: moved, never dropped. Each tab's state rows
    # live in that tab's own observations, so moving them cannot collide.
    cursor.execute("UPDATE org_identity SET person_id = %s WHERE person_id = %s", (keep, drop))
    cursor.execute(
        "UPDATE org_person_state SET person_id = %s WHERE person_id = %s", (keep, drop)
    )
    cursor.execute(
        "UPDATE org_unmapped_account SET resolved_person_id = %s WHERE resolved_person_id = %s",
        (keep, drop),
    )
    # A person's decisions travel with them; nothing a person chose is lost
    # because two rows turned out to be one human.
    cursor.execute("UPDATE answer_pairs SET person_id = %s WHERE person_id = %s", (keep, drop))
    # Derived: rebuilt.
    cursor.execute("DELETE FROM person_day_digest WHERE person_id = %s", (drop,))
    cursor.execute("DELETE FROM conversation_blocks WHERE person_id = %s", (drop,))
    cursor.execute(
        """
        UPDATE org_person keep_row
           SET first_seen = LEAST(keep_row.first_seen, drop_row.first_seen),
               last_seen = GREATEST(keep_row.last_seen, drop_row.last_seen)
          FROM org_person drop_row
         WHERE keep_row.person_id = %s AND drop_row.person_id = %s
        """,
        (keep, drop),
    )
    cursor.execute("DELETE FROM org_person WHERE person_id = %s", (drop,))
    return days


def merge_dual(cursor, *, exclude: list[str], apply: bool = False) -> MergeResult:
    """Join each internal/external same-name pair, except the names given."""
    result = MergeResult(apply=apply)
    found = candidates(cursor)

    # A name with more than one pair is never joined: which of two people is
    # the lab one is exactly what this cannot know.
    counts: dict[str, int] = {}
    for name, _, _ in found:
        counts[name] = counts.get(name, 0) + 1

    excluded = {name.strip() for name in exclude if name.strip()}
    days: set[str] = set()
    for name, keep, drop in found:
        if name in excluded:
            result.excluded.append(name)
            continue
        if counts[name] > 1:
            if name not in result.ambiguous:
                result.ambiguous.append(name)
            continue
        result.pairs.append({"name": name, "keep": keep, "drop": drop})
        if apply:
            days.update(join_person(cursor, keep=keep, drop=drop))
    result.days_to_rebuild = sorted(days)
    return result


def merge(database_url: str, *, exclude: list[str], apply: bool = False) -> MergeResult:
    import psycopg

    with psycopg.connect(database_url) as connection:
        connection.autocommit = False
        with connection.cursor() as cursor:
            result = merge_dual(cursor, exclude=exclude, apply=apply)
        if apply and result.pairs:
            connection.commit()
        else:
            connection.rollback()
    return result
