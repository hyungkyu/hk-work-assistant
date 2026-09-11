"""Writing a roster observation, and reading the organisation back.

The plan (`plan.py`) decides what an observation implies; this writes it.
Everything here is additive: `org_person` and `org_identity` widen their
`last_seen`, `org_person_state` gains a row per observation, and nothing is
ever deleted or overwritten.

The one subtlety is absence. A person who disappears from the sheet must
still appear in the observation, with `status='absent_from_sheet'` -- if they
simply stopped being written, the newest observation would look like a
complete org chart that silently lost people, and every activity of theirs
would become unattributable. So each sync closes over everyone who was
present in the previous observation and was not seen in this one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .plan import duplicates, plan


@dataclass
class SyncResult:
    dry_run: bool
    observation_id: int | None = None
    source: str = ""
    rows: int = 0
    people: int = 0
    people_new: int = 0
    teams: int = 0
    identities: int = 0
    identities_new: int = 0
    absent_from_sheet: int = 0
    unchanged_workbook: bool = False
    duplicate_rows: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "observation_id": self.observation_id,
            "source": self.source,
            "rows": self.rows,
            "people": self.people,
            "people_new": self.people_new,
            "teams": self.teams,
            "identities": self.identities,
            "identities_new": self.identities_new,
            "absent_from_sheet": self.absent_from_sheet,
            "unchanged_workbook": self.unchanged_workbook,
            # Reported, never merged: two rows folding onto one person is
            # either a sheet mistake or deliberate, and only a person knows.
            "duplicate_rows": self.duplicate_rows[:20],
            "errors": self.errors[:20],
        }


def latest_observation(cursor, source: str) -> int | None:
    cursor.execute(
        "SELECT observation_id FROM roster_observation WHERE source = %s"
        " ORDER BY observation_id DESC LIMIT 1",
        (source,),
    )
    row = cursor.fetchone()
    return int(row[0]) if row else None


def last_digest(cursor, source: str) -> str | None:
    cursor.execute(
        "SELECT source_digest FROM roster_observation WHERE source = %s"
        " ORDER BY observation_id DESC LIMIT 1",
        (source,),
    )
    row = cursor.fetchone()
    return row[0] if row else None


def write_observation(
    database_url: str,
    records: list[dict],
    *,
    source: str,
    digest: str | None = None,
    observed_at: datetime | None = None,
    dry_run: bool = False,
    skip_unchanged: bool = True,
) -> SyncResult:
    """Record one tab's rows as an observation.

    `skip_unchanged` compares the workbook digest with the previous
    observation of the same tab. The batch runs daily and the sheet does not
    change daily; without this the history would fill with observations that
    say the same thing, and "when did this person's team change" would become
    a search through identical rows.
    """
    import psycopg
    from psycopg.types.json import Jsonb  # noqa: F401  (kept for symmetry with other writers)

    result = SyncResult(dry_run=dry_run, source=source, rows=len(records))
    result.duplicate_rows = [
        f"{identifier}: {' / '.join(rows)}" for identifier, rows in duplicates(records)
    ]
    when = observed_at or datetime.now(timezone.utc)

    with psycopg.connect(database_url) as connection:
        connection.autocommit = False
        with connection.cursor() as cursor:
            if skip_unchanged and digest and last_digest(cursor, source) == digest:
                result.unchanged_workbook = True
                result.observation_id = latest_observation(cursor, source)
                connection.rollback()
                return result

            previous = latest_observation(cursor, source)
            cursor.execute(
                "INSERT INTO roster_observation (observed_at, source, source_digest, row_count)"
                " VALUES (%s, %s, %s, %s) RETURNING observation_id",
                (when, source, digest, len(records)),
            )
            observation_id = int(cursor.fetchone()[0])
            result.observation_id = observation_id

            written = plan(records, observation_id)
            _write_teams(cursor, written["team"], result)
            _write_people(cursor, written["person"], observation_id, result)
            _write_states(cursor, written["person_state"], result)
            _write_identities(cursor, written["identity"], observation_id, result)
            _close_absentees(cursor, observation_id, previous, source, result)

            if dry_run:
                connection.rollback()
            else:
                connection.commit()
    return result


def _write_teams(cursor, teams: list[dict], result: SyncResult) -> None:
    for node in teams:
        cursor.execute(
            """
            INSERT INTO org_team (team_id, name, parent_team_id, path, depth)
            VALUES (%(team_id)s, %(name)s, %(parent_team_id)s, %(path)s, %(depth)s)
            ON CONFLICT (team_id) DO UPDATE SET
                name = EXCLUDED.name,
                parent_team_id = EXCLUDED.parent_team_id,
                depth = EXCLUDED.depth
            """,
            node,
        )
        result.teams += 1


def _write_people(cursor, people: dict[str, Any], observation_id: int, result: SyncResult) -> None:
    for person_id, name in people.items():
        cursor.execute(
            """
            INSERT INTO org_person (person_id, name, first_seen, last_seen)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (person_id) DO UPDATE SET
                name = EXCLUDED.name,
                last_seen = EXCLUDED.last_seen
            RETURNING first_seen = last_seen
            """,
            (person_id, name or person_id, observation_id, observation_id),
        )
        row = cursor.fetchone()
        result.people += 1
        if row and row[0]:
            result.people_new += 1


def _write_states(cursor, states: list[dict], result: SyncResult) -> None:
    for state in states:
        cursor.execute(
            """
            INSERT INTO org_person_state (
                observation_id, person_id, nickname, title, employment_type,
                affiliation, access_level, status, team_id, department_raw
            ) VALUES (
                %(observation_id)s, %(person_id)s, %(nickname)s, %(title)s,
                %(employment_type)s, %(affiliation)s, %(access_level)s,
                %(status)s, %(team_id)s, %(department_raw)s
            )
            ON CONFLICT (observation_id, person_id) DO UPDATE SET
                nickname = EXCLUDED.nickname,
                title = EXCLUDED.title,
                employment_type = EXCLUDED.employment_type,
                affiliation = EXCLUDED.affiliation,
                access_level = EXCLUDED.access_level,
                status = EXCLUDED.status,
                team_id = EXCLUDED.team_id,
                department_raw = EXCLUDED.department_raw
            """,
            state,
        )


def _write_identities(
    cursor, identities: dict[tuple[str, str], str], observation_id: int, result: SyncResult
) -> None:
    for (kind, value), person_id in identities.items():
        cursor.execute(
            """
            INSERT INTO org_identity (kind, value, person_id, first_seen, last_seen, origin)
            VALUES (%s, %s, %s, %s, %s, 'roster')
            ON CONFLICT (kind, value) DO UPDATE SET
                person_id = EXCLUDED.person_id,
                last_seen = EXCLUDED.last_seen
            RETURNING first_seen = last_seen
            """,
            (kind, value, person_id, observation_id, observation_id),
        )
        row = cursor.fetchone()
        result.identities += 1
        if row and row[0]:
            result.identities_new += 1
        # An account that was waiting for an owner now has one.
        cursor.execute(
            "UPDATE org_unmapped_account SET state = 'resolved', resolved_person_id = %s"
            " WHERE kind = %s AND value = %s AND state = 'open'",
            (person_id, kind, value),
        )


def _close_absentees(
    cursor, observation_id: int, previous: int | None, source: str, result: SyncResult
) -> None:
    """Carry forward anyone the previous observation had and this one does not.

    Without this the newest observation looks complete while quietly holding
    fewer people, and a person who left the sheet would have no state at all
    in it -- which reads as "never existed" rather than "no longer listed".
    """
    if previous is None:
        return
    cursor.execute(
        """
        INSERT INTO org_person_state (
            observation_id, person_id, nickname, title, employment_type,
            affiliation, access_level, status, team_id, department_raw
        )
        SELECT %(observation_id)s, previous.person_id, previous.nickname,
               previous.title, previous.employment_type, previous.affiliation,
               previous.access_level, 'absent_from_sheet', previous.team_id,
               previous.department_raw
          FROM org_person_state previous
         WHERE previous.observation_id = %(previous)s
           AND NOT EXISTS (
               SELECT 1 FROM org_person_state current
                WHERE current.observation_id = %(observation_id)s
                  AND current.person_id = previous.person_id
           )
        ON CONFLICT (observation_id, person_id) DO NOTHING
        """,
        {"observation_id": observation_id, "previous": previous},
    )
    result.absent_from_sheet += cursor.rowcount if cursor.rowcount > 0 else 0


def org_status(database_url: str) -> dict[str, Any]:
    """What the organisation store holds, per tab, newest observation first."""
    import psycopg

    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT source, max(observed_at), count(*), max(observation_id)
                  FROM roster_observation GROUP BY source ORDER BY source
                """
            )
            observations = [
                {
                    "source": row[0],
                    "latest_observed_at": row[1].isoformat() if row[1] else None,
                    "observations": row[2],
                    "latest_observation_id": row[3],
                }
                for row in cursor.fetchall()
            ]
            cursor.execute("SELECT count(*) FROM org_person")
            people = int(cursor.fetchone()[0])
            cursor.execute("SELECT kind, count(*) FROM org_identity GROUP BY kind ORDER BY kind")
            identities = {row[0]: row[1] for row in cursor.fetchall()}
            cursor.execute(
                "SELECT state, count(*) FROM org_unmapped_account GROUP BY state ORDER BY state"
            )
            unmapped = {row[0]: row[1] for row in cursor.fetchall()}
    return {
        "observations": observations,
        "people": people,
        "identities": identities,
        "unmapped_accounts": unmapped,
    }
