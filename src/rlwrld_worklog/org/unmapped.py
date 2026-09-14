"""Accounts that did work and belong to nobody we know.

The roster says which accounts are whose. Everything it does not say lands
here: a GitHub login, a Slack id, a Slurm account name that appears in
collected activity and matches no `org_identity` row.

This is not an error table. Every row is a question for a person, and HK
answered the general form of it on 2026-09-11: an unrecognised Slurm name is
either somebody new or somebody's former name, and no table exists that maps
the old names to the current ones. So the system does not guess. It counts
what each unknown account did, puts it in front of a person once, and stores
the answer as an identity with `origin='resolved'` -- after which the account
is known and nobody is asked again.

The counting matters as much as the asking. An account with four events is a
curiosity; one with two hundred is a person whose work is missing from every
report, and the difference is the whole reason to look.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Which identity kind an actor handle belongs to. `actor_kind` is what the
# projection recorded when it knew (a GitHub login is not a Slack id even when
# the strings match); the source is the fallback for the sources whose actor
# is unambiguous.
KIND_BY_ACTOR_KIND = {
    "github_login": "github",
    "slurm_user": "slurm",
    "git_email": "email_official",
}

KIND_BY_SOURCE = {
    "slack": "slack",
    "notion": "notion",
    "github": "github",
    "slurm": "slurm",
    "google_calendar": "email_official",
}

# An email handle can be registered under any of the three email columns the
# roster carries, so matching one means checking all three.
EMAIL_KINDS = ("email_official", "email_personal", "email_school")


def identity_kind(source: str, actor_kind: str | None) -> str | None:
    """The identity kind a handle from this source would be registered under."""
    if actor_kind and actor_kind in KIND_BY_ACTOR_KIND:
        return KIND_BY_ACTOR_KIND[actor_kind]
    return KIND_BY_SOURCE.get(source)


def candidate_kinds(kind: str) -> tuple[str, ...]:
    return EMAIL_KINDS if kind in EMAIL_KINDS else (kind,)


@dataclass
class ScanResult:
    dry_run: bool
    actors_seen: int = 0
    unmapped: int = 0
    events_unmapped: int = 0
    new_rows: int = 0
    resolved_now: int = 0
    by_kind: dict[str, int] = field(default_factory=dict)
    top: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "actors_seen": self.actors_seen,
            "unmapped": self.unmapped,
            "events_unmapped": self.events_unmapped,
            "new_rows": self.new_rows,
            # Accounts that had been open and now have an owner, because the
            # roster gained them. The queue shrinking on its own is the
            # normal case and worth seeing.
            "resolved_now": self.resolved_now,
            "by_kind": dict(sorted(self.by_kind.items())),
            "top": self.top[:20],
        }


# Every distinct actor in the timeline, with how much it did and when. Grouped
# in SQL because the timeline is millions of rows and the answer is hundreds.
_ACTORS_SQL = """
    SELECT event.source,
           event.actor_external_id,
           coalesce(event.payload->>'actor_kind', 'unknown') AS actor_kind,
           count(*) AS events,
           min(event.occurred_at) AS first_seen,
           max(event.occurred_at) AS last_seen
      FROM timeline_events event
     WHERE event.actor_external_id IS NOT NULL
       AND event.actor_external_id <> ''
     GROUP BY 1, 2, 3
"""

_UPSERT = """
INSERT INTO org_unmapped_account
    (kind, value, first_seen_at, last_seen_at, events, state)
VALUES (%(kind)s, %(value)s, %(first_seen)s, %(last_seen)s, %(events)s, 'open')
ON CONFLICT (kind, value) DO UPDATE SET
    first_seen_at = least(org_unmapped_account.first_seen_at, EXCLUDED.first_seen_at),
    last_seen_at = greatest(org_unmapped_account.last_seen_at, EXCLUDED.last_seen_at),
    events = EXCLUDED.events
RETURNING (xmax = 0) AS inserted
"""


def scan(database_url: str, *, dry_run: bool = False) -> ScanResult:
    """Find every actor with activity and no owner, and record it.

    A row already marked `ignored` (a bot, a service account somebody has
    judged) keeps that state: its counts are refreshed so the page stays
    honest about how much it is doing, but it does not reopen. Re-asking a
    question somebody already answered is how a queue becomes noise.
    """
    import psycopg

    result = ScanResult(dry_run=dry_run)

    with psycopg.connect(database_url) as connection:
        connection.autocommit = False
        with connection.cursor() as cursor:
            cursor.execute(_ACTORS_SQL)
            actors = cursor.fetchall()
            result.actors_seen = len(actors)

            cursor.execute("SELECT kind, value FROM org_identity")
            known = {(row[0], row[1]) for row in cursor.fetchall()}
            # Emails are matched across all three columns, so a lookup by
            # value alone is what the email case needs.
            known_emails = {value for kind, value in known if kind in EMAIL_KINDS}

            rows: list[dict[str, Any]] = []
            for source, value, actor_kind, events, first_seen, last_seen in actors:
                kind = identity_kind(source, actor_kind)
                if kind is None:
                    # A source whose actors we cannot classify at all. Not
                    # recorded as unmapped, because "we do not know what kind
                    # of handle this is" is a different problem from "we do
                    # not know whose it is", and filing it here would put an
                    # unanswerable question in a queue of answerable ones.
                    continue
                if kind in EMAIL_KINDS:
                    if value in known_emails:
                        continue
                elif (kind, value) in known:
                    continue
                rows.append(
                    {
                        "kind": kind,
                        "value": value,
                        "events": int(events),
                        "first_seen": first_seen,
                        "last_seen": last_seen,
                    }
                )

            result.unmapped = len(rows)
            result.events_unmapped = sum(row["events"] for row in rows)
            for row in rows:
                result.by_kind[row["kind"]] = result.by_kind.get(row["kind"], 0) + 1
            result.top = sorted(
                (
                    {"kind": row["kind"], "value": row["value"], "events": row["events"]}
                    for row in rows
                ),
                key=lambda item: -item["events"],
            )

            if not dry_run:
                for row in rows:
                    cursor.execute(_UPSERT, row)
                    inserted = cursor.fetchone()
                    if inserted and inserted[0]:
                        result.new_rows += 1
                # An account the roster has since claimed is closed here
                # rather than left open forever.
                cursor.execute(
                    """
                    UPDATE org_unmapped_account unmapped
                       SET state = 'resolved',
                           resolved_person_id = identity.person_id
                      FROM org_identity identity
                     WHERE unmapped.state = 'open'
                       AND identity.value = unmapped.value
                       AND (identity.kind = unmapped.kind
                            OR (identity.kind = ANY(%(emails)s)
                                AND unmapped.kind = ANY(%(emails)s)))
                    """,
                    {"emails": list(EMAIL_KINDS)},
                )
                result.resolved_now = cursor.rowcount if cursor.rowcount > 0 else 0
                connection.commit()
            else:
                connection.rollback()
    return result


def resolve(
    database_url: str,
    *,
    kind: str,
    value: str,
    person_id: str | None = None,
    ignore: bool = False,
    note: str | None = None,
) -> dict[str, Any]:
    """Attach an unknown account to a person, or judge it not a person.

    Attaching writes an `org_identity` row with `origin='resolved'`, which is
    what makes the answer permanent: the next scan sees the account as known
    and never asks again. This is also the only path by which a former Slurm
    name can ever reach the person who used it, since no old-to-new table
    exists (HK, 2026-09-11).
    """
    import psycopg

    if not ignore and not person_id:
        raise ValueError("resolving an account needs either a person id or --ignore")

    with psycopg.connect(database_url) as connection:
        connection.autocommit = False
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT state FROM org_unmapped_account WHERE kind = %s AND value = %s",
                (kind, value),
            )
            found = cursor.fetchone()
            if found is None:
                connection.rollback()
                return {"ok": False, "reason": "no such unmapped account", "kind": kind,
                        "value": value}

            if ignore:
                cursor.execute(
                    "UPDATE org_unmapped_account SET state = 'ignored', note = %s"
                    " WHERE kind = %s AND value = %s",
                    (note, kind, value),
                )
                connection.commit()
                return {"ok": True, "state": "ignored", "kind": kind, "value": value}

            cursor.execute("SELECT name FROM org_person WHERE person_id = %s", (person_id,))
            person = cursor.fetchone()
            if person is None:
                connection.rollback()
                return {"ok": False, "reason": "no such person", "person_id": person_id}

            # The identity is written against the newest observation, because
            # that is when we learned it -- the same rule the roster sync
            # follows, and the reason nothing here stores a date of its own.
            cursor.execute("SELECT max(observation_id) FROM roster_observation")
            observation = cursor.fetchone()[0]
            cursor.execute(
                """
                INSERT INTO org_identity
                    (kind, value, person_id, first_seen, last_seen, origin)
                VALUES (%s, %s, %s, %s, %s, 'resolved')
                ON CONFLICT (kind, value) DO UPDATE SET
                    person_id = EXCLUDED.person_id,
                    last_seen = EXCLUDED.last_seen,
                    origin = 'resolved'
                """,
                (kind, value, person_id, observation, observation),
            )
            cursor.execute(
                "UPDATE org_unmapped_account SET state = 'resolved',"
                " resolved_person_id = %s, note = %s WHERE kind = %s AND value = %s",
                (person_id, note, kind, value),
            )
            connection.commit()
    return {
        "ok": True,
        "state": "resolved",
        "kind": kind,
        "value": value,
        "person_id": person_id,
        "person": person[0],
    }


def list_unmapped(
    database_url: str, *, state: str = "open", limit: int = 100
) -> dict[str, Any]:
    """The queue, busiest first: the accounts whose absence costs most."""
    import psycopg

    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT kind, value, events, first_seen_at, last_seen_at, state,
                       resolved_person_id, note
                  FROM org_unmapped_account
                 WHERE (%(state)s = 'all' OR state = %(state)s)
                 ORDER BY events DESC, value
                 LIMIT %(limit)s
                """,
                {"state": state, "limit": limit},
            )
            rows = [
                {
                    "kind": row[0],
                    "value": row[1],
                    "events": row[2],
                    "first_seen_at": row[3].isoformat() if row[3] else None,
                    "last_seen_at": row[4].isoformat() if row[4] else None,
                    "state": row[5],
                    "resolved_person_id": row[6],
                    "note": row[7],
                }
                for row in cursor.fetchall()
            ]
            cursor.execute(
                "SELECT state, count(*), sum(events) FROM org_unmapped_account GROUP BY state"
            )
            totals = {
                row[0]: {"accounts": row[1], "events": int(row[2] or 0)}
                for row in cursor.fetchall()
            }
    return {"state": state, "accounts": rows, "totals": totals}
