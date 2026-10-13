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


def _text(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def account_details(cursor, kind: str, value: str) -> dict[str, Any]:
    """Everything already collected that says who one account might be."""
    return accounts_details(cursor, [(kind, value)])[(kind, value)]


def accounts_details(cursor, accounts: list[tuple[str, str]]) -> dict[tuple[str, str], dict[str, Any]]:
    """What the sources say about many accounts, in a handful of queries.

    HK, 2026-10-13: 주인없는 계정은 구체적으로 이메일, 이름, 닉네임, 슬랙
    아이디, 노션 아이디, 슬럼 아이디, 깃헙 아이디 등을 구체적으로 알려주시면
    좋겠어. 내가 그걸 매핑할거야.

    Slack and Notion keep a profile for every account (name, display name,
    email); every source records where an account was active. Nothing here
    is a guess at the answer: it is what the sources said.

    Batched, because the first version asked per account and the 미확인 계정
    screen took long enough to complain about (HK, same day: 이 페이지가 너무
    느린데?). Two hundred accounts each scanned timeline_events by an actor
    column that had no index -- two hundred full scans of the largest table
    here. Now: one query per source for profiles, one indexed query for
    where everybody was active, whatever the number of accounts.
    """
    out: dict[tuple[str, str], dict[str, Any]] = {
        (kind, value): {"handle": value, "kind": kind} for kind, value in accounts
    }
    by_kind: dict[str, list[str]] = {}
    for kind, value in accounts:
        by_kind.setdefault(kind, []).append(value)

    slack = by_kind.get("slack") or []
    if slack:
        cursor.execute(
            """
            SELECT DISTINCT ON (handle) handle, raw_payload
              FROM (
                SELECT regexp_replace(source_entity_id, '^.*:', '') AS handle,
                       raw_payload, collected_at
                  FROM ledger_records
                 WHERE source = 'slack' AND entity_type = 'user'
              ) users
             WHERE handle = ANY(%s)
             ORDER BY handle, collected_at DESC
            """,
            (slack,),
        )
        for handle, payload in cursor.fetchall():
            payload = payload if isinstance(payload, dict) else {}
            profile = payload.get("profile") if isinstance(payload.get("profile"), dict) else {}
            out[("slack", handle)].update(
                {
                    "name": _text(profile.get("real_name") or payload.get("real_name")),
                    "nickname": _text(profile.get("display_name")),
                    "email": _text(profile.get("email")),
                    "title": _text(profile.get("title")),
                    "username": _text(payload.get("name")),
                    "bot": bool(payload.get("is_bot")) or None,
                    "deactivated": bool(payload.get("deleted")) or None,
                    # Slack does not say who made a bot; the app it belongs
                    # to is the most it gives, and often names the service.
                    "app": _text(profile.get("api_app_id")) if payload.get("is_bot") else None,
                }
            )

    notion = by_kind.get("notion") or []
    if notion:
        cursor.execute(
            """
            SELECT DISTINCT ON (handle) handle, raw_payload
              FROM (
                SELECT regexp_replace(source_entity_id, '^.*:', '') AS handle,
                       raw_payload, collected_at
                  FROM ledger_records
                 WHERE source = 'notion' AND entity_type = 'user'
              ) users
             WHERE handle = ANY(%s)
             ORDER BY handle, collected_at DESC
            """,
            (notion,),
        )
        for handle, payload in cursor.fetchall():
            payload = payload if isinstance(payload, dict) else {}
            person = payload.get("person") if isinstance(payload.get("person"), dict) else {}
            bot = payload.get("bot") if isinstance(payload.get("bot"), dict) else {}
            owner = bot.get("owner") if isinstance(bot.get("owner"), dict) else {}
            owner_user = owner.get("user") if isinstance(owner.get("user"), dict) else {}
            owner_person = (
                owner_user.get("person") if isinstance(owner_user.get("person"), dict) else {}
            )
            out[("notion", handle)].update(
                {
                    "name": _text(payload.get("name")),
                    "email": _text(person.get("email")),
                    "bot": (payload.get("type") == "bot") or None,
                    # A Notion bot names who made it (HK, 2026-10-13: 봇은 봇을
                    # 만든 사람이 있을 것 같고). Usually only the id; the name
                    # and email are filled in below from that user's record.
                    "owner_notion_id": _text(owner_user.get("id")),
                    "owner_name": _text(owner_user.get("name")),
                    "owner_email": _text(owner_person.get("email")),
                    "workspace_bot": (owner.get("type") == "workspace") or None,
                }
            )

    owners = sorted(
        {
            details["owner_notion_id"]
            for details in out.values()
            if details.get("owner_notion_id") and not details.get("owner_name")
        }
    )
    if owners:
        cursor.execute(
            """
            SELECT DISTINCT ON (handle) handle, raw_payload
              FROM (
                SELECT regexp_replace(source_entity_id, '^.*:', '') AS handle,
                       raw_payload, collected_at
                  FROM ledger_records
                 WHERE source = 'notion' AND entity_type = 'user'
              ) users
             WHERE handle = ANY(%s)
             ORDER BY handle, collected_at DESC
            """,
            (owners,),
        )
        found = {}
        for handle, payload in cursor.fetchall():
            payload = payload if isinstance(payload, dict) else {}
            person = payload.get("person") if isinstance(payload.get("person"), dict) else {}
            found[handle] = (_text(payload.get("name")), _text(person.get("email")))
        for details in out.values():
            name, email = found.get(details.get("owner_notion_id") or "", (None, None))
            details["owner_name"] = details.get("owner_name") or name
            details["owner_email"] = details.get("owner_email") or email

    for kind in EMAIL_KINDS:
        for value in by_kind.get(kind) or []:
            out[(kind, value)]["email"] = value

    # Where each was active, in one indexed query (0021). The channel or
    # repository somebody works in is often what gives them away.
    source_of = {"slack": "slack", "github": "github", "notion": "notion", "slurm": "slurm"}
    pairs = [(source_of[kind], value) for kind, value in accounts if kind in source_of]
    places: dict[tuple[str, str], list[dict[str, Any]]] = {}
    if pairs:
        cursor.execute(
            """
            SELECT source, actor_external_id, container_id, count(*) AS events
              FROM timeline_events
             WHERE (source, actor_external_id) IN (
                       SELECT * FROM unnest(%s::text[], %s::text[]))
               AND container_id IS NOT NULL
             GROUP BY source, actor_external_id, container_id
            """,
            ([pair[0] for pair in pairs], [pair[1] for pair in pairs]),
        )
        for source, actor, container, events in cursor.fetchall():
            places.setdefault((source, actor), []).append({"where": container, "events": events})

    channels = sorted(
        {item["where"] for (source, _), rows in places.items() if source == "slack" for item in rows}
    )
    names: dict[str, str] = {}
    if channels:
        cursor.execute(
            """
            SELECT DISTINCT ON (handle) handle, denormalized_label_snapshot
              FROM (
                SELECT regexp_replace(source_entity_id, '^.*:', '') AS handle,
                       denormalized_label_snapshot, collected_at
                  FROM ledger_records
                 WHERE source = 'slack' AND entity_type = 'conversation'
              ) conversations
             WHERE handle = ANY(%s)
             ORDER BY handle, collected_at DESC
            """,
            (channels,),
        )
        for handle, snapshot in cursor.fetchall():
            label = _text((snapshot or {}).get("name")) if isinstance(snapshot, dict) else None
            if label:
                names[handle] = label

    for kind, value in accounts:
        found = sorted(
            places.get((source_of.get(kind, ""), value), []), key=lambda item: -item["events"]
        )[:3]
        shown = [
            {"where": f"#{names[item['where']]}" if kind == "slack" and item["where"] in names
             else item["where"], "events": item["events"], "id": item["where"]}
            for item in found
        ]
        details = out[(kind, value)]
        out[(kind, value)] = {
            key: item for key, item in details.items() if item not in (None, [], "")
        } | {"places": shown}
    return out


def owner_candidates(cursor, details: dict[str, Any]) -> list[dict[str, Any]]:
    """People on the roster this account plausibly belongs to, strongest first.

    HK, 2026-10-13: 기존의 학교 학생 계정이든, 교수 계정이든, 리얼월드 퇴사자
    계정이든. 뭔가 있을거야. 학생/교수님들은 슬랙/노션 계정이 2개 일 수 있어.

    So most of these accounts are a second account of somebody already
    here -- a lab Slack beside the company one, a school Notion beside the
    company one, the account of somebody who has left. The account's own
    profile usually says who: its email is often one the roster already
    holds for that person, and its name is theirs.

    Two kinds of evidence, never mixed up:

    * 이메일 일치 -- the profile email is an identity the roster holds. One
      person; very likely right.
    * 이름 일치 -- the profile name equals a roster name or nickname. Can be
      several people (동명이인), so every match is listed and none is chosen.

    Everybody on the roster counts, including people who have left, because
    "the account of somebody who has left" is one of the cases HK named.
    Nothing is linked here; a person clicks.
    """
    found: dict[str, dict[str, Any]] = {}

    email = (details.get("email") or "").strip().lower()
    if email:
        cursor.execute(
            """
            SELECT person.person_id, person.name, identity.kind
              FROM org_identity identity
              JOIN org_person person ON person.person_id = identity.person_id
             WHERE lower(identity.value) = %s
            """,
            (email,),
        )
        for person_id, name, kind in cursor.fetchall():
            found.setdefault(
                person_id,
                {"person_id": person_id, "name": name, "why": f"이메일 일치 ({kind})", "strength": 2},
            )

    for raw in (details.get("name"), details.get("nickname")):
        name = (raw or "").strip()
        if not name:
            continue
        cursor.execute(
            """
            SELECT DISTINCT person.person_id, person.name
              FROM org_person person
              LEFT JOIN org_person_state state ON state.person_id = person.person_id
             WHERE replace(person.name, ' ', '') = replace(%(n)s, ' ', '')
                OR lower(coalesce(state.nickname, '')) = lower(%(n)s)
            """,
            {"n": name},
        )
        for person_id, person_name in cursor.fetchall():
            found.setdefault(
                person_id,
                {"person_id": person_id, "name": person_name, "why": "이름 일치", "strength": 1},
            )

    # A bot's maker (Notion says who). Its activity is that person's doing,
    # which is a reading HK may or may not want -- offered, never applied.
    owner_id = details.get("owner_notion_id")
    if owner_id:
        cursor.execute(
            """
            SELECT person.person_id, person.name
              FROM org_identity identity
              JOIN org_person person ON person.person_id = identity.person_id
             WHERE identity.kind = 'notion' AND identity.value = %s
            """,
            (owner_id,),
        )
        for person_id, name in cursor.fetchall():
            found.setdefault(
                person_id,
                {"person_id": person_id, "name": name, "why": "봇을 만든 사람", "strength": 1},
            )
    owner_email = (details.get("owner_email") or "").strip().lower()
    if owner_email:
        cursor.execute(
            """
            SELECT person.person_id, person.name
              FROM org_identity identity
              JOIN org_person person ON person.person_id = identity.person_id
             WHERE lower(identity.value) = %s
            """,
            (owner_email,),
        )
        for person_id, name in cursor.fetchall():
            found.setdefault(
                person_id,
                {"person_id": person_id, "name": name, "why": "봇을 만든 사람", "strength": 1},
            )

    if not found:
        return []
    # Say where each candidate is and whether they have left, so the person
    # deciding between two 김동현 can tell them apart without opening a tab.
    cursor.execute(
        """
        SELECT DISTINCT ON (state.person_id)
               state.person_id, state.status, state.department_raw, state.employment_type
          FROM org_person_state state
         WHERE state.person_id = ANY(%s) AND state.status <> 'absent_from_sheet'
         ORDER BY state.person_id, state.observation_id DESC
        """,
        (list(found),),
    )
    for person_id, status, department, employment in cursor.fetchall():
        found[person_id].update(
            {"status": status, "department": department, "employment_type": employment}
        )
    return sorted(found.values(), key=lambda item: (-item["strength"], item["name"]))


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
            details = accounts_details(cursor, [(row["kind"], row["value"]) for row in rows])
            for account in rows:
                account["details"] = details[(account["kind"], account["value"])]
                if account["state"] == "open":
                    account["candidates"] = owner_candidates(cursor, account["details"])
            cursor.execute(
                "SELECT state, count(*), sum(events) FROM org_unmapped_account GROUP BY state"
            )
            totals = {
                row[0]: {"accounts": row[1], "events": int(row[2] or 0)}
                for row in cursor.fetchall()
            }
    return {"state": state, "accounts": rows, "totals": totals}


# How a bot judged by `resolve_in_bulk` is recorded, so the chart can list
# them apart from other accounts judged "not a person" by hand.
BOT_NOTE = "봇"
EMAIL_NOTE = "이메일 일치로 연결"


def resolve_in_bulk(database_url: str, *, what: str) -> dict[str, Any]:
    """Answer the obvious ones in one go, and leave every judgement call alone.

    HK, 2026-10-13: 미확인 계정에서 퇴사자 정보랑 링크가 되는게 제법 있고,
    봇이 제법 있는데, 퇴사자는 조직도에서 매핑되고, 봇은 조직도 하단에
    봇계정으로 둘 수 있지 않아?

    Two kinds only, both decided by what the source itself says:

    * `email` -- the account's own profile email is an identity the roster
      holds for exactly one person. That person may have left; that is the
      point -- the activity of somebody who has left lands on them.
    * `bots` -- the source marks the account as a bot (Slack `is_bot`,
      Notion `type: bot`).

    A name match is never applied here. Two people can share a name, and
    that is a click a person makes. Each account goes through `resolve`,
    the same path a single click takes, so a bulk answer is stored exactly
    like a hand-made one and is undone the same way.
    """
    if what not in {"email", "bots"}:
        raise ValueError("what must be 'email' or 'bots'")
    listing = list_unmapped(database_url, state="open", limit=10_000)
    applied = []
    for account in listing["accounts"]:
        details = account.get("details") or {}
        if what == "bots":
            if not details.get("bot"):
                continue
            outcome = resolve(
                database_url, kind=account["kind"], value=account["value"], ignore=True, note=BOT_NOTE
            )
        else:
            strong = [item for item in account.get("candidates") or [] if item.get("strength", 0) >= 2]
            if len(strong) != 1:
                continue
            outcome = resolve(
                database_url,
                kind=account["kind"],
                value=account["value"],
                person_id=strong[0]["person_id"],
                note=EMAIL_NOTE,
            )
        if outcome.get("ok"):
            applied.append(
                {
                    "kind": account["kind"],
                    "value": account["value"],
                    "name": details.get("name") or details.get("nickname"),
                    "to": None if what == "bots" else strong[0]["name"],
                }
            )
    return {"what": what, "applied": len(applied), "accounts": applied}
