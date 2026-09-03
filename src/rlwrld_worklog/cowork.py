"""Cowork identities, directive authority, and autonomous-execution limits.

Three separate concerns live here, and they are separate on purpose.

**Who is speaking.** `hk`, `ari`, `mori` and `moa` are the parties. Everything
else that appears in a historical record -- `codex`, `claude-code`,
`claude-cowork` -- predates that naming. Those names are *not* silently
rewritten onto today's parties: an actor is resolved through an append-only
alias registry that says what is known, what is merely inferred from the time
the record was written, and what cannot be resolved at all. A timeline that
relabels history is worse than one that admits it does not know.

**What a directive authorizes.** A message in the mailbox is not permission.
Authority is checked against an allowlist -- sender, recipient, message type,
work item, observed revision -- and the check is *fail-closed*: anything the
validator cannot parse, cannot verify, or does not recognise is refused. A
message that is merely present, or a work item that merely sits in `ready`,
authorizes nothing.

**What may run unattended.** The baseline for an unattended session is
read, investigate, report and test. Writing files, committing, building and
deploying are each a separate opt-in that the directive must state explicitly.
Sudo, deletion, and changes to policy or security controls are never granted
automatically, no matter what a message says.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Iterable, Mapping

COWORK_SCHEMA_VERSION = 1

# --------------------------------------------------------------- identities

HK = "hk"
ARI = "ari"
MORI = "mori"
MOA = "moa"

PARTIES = (HK, ARI, MORI, MOA)

PARTY_ROLES = {
    HK: "decision maker and requester",
    ARI: "primary operator: requirements, rules, review criteria, handoff verification",
    MORI: "Cowork deputy: planning, work-item management, may direct moa when ari is away",
    MOA: "Claude Code executor: shell, code, tests, builds, deploys. Never self-assigns.",
}

# Authority order. A conflict is resolved by the earliest party in this tuple.
AUTHORITY_ORDER = (HK, ARI, MORI)

# Parties whose messages moa will act on at all.
DIRECTING_PARTIES = frozenset(AUTHORITY_ORDER)


def authority_rank(party: str) -> int:
    """Lower wins. Anything outside AUTHORITY_ORDER ranks last."""
    try:
        return AUTHORITY_ORDER.index(party)
    except ValueError:
        return len(AUTHORITY_ORDER)


def resolve_conflict(parties: Iterable[str]) -> str | None:
    ranked = sorted({p for p in parties if p in DIRECTING_PARTIES}, key=authority_rank)
    return ranked[0] if ranked else None


# ------------------------------------------------------------------ aliases


@dataclass(frozen=True)
class ActorAlias:
    """One historical actor name, and how far it can honestly be resolved.

    ``party`` is filled in only when the mapping is actually known. When it is
    ``None`` the name stays unresolved, and every reader is told why rather
    than being handed a guess.
    """

    name: str
    party: str | None
    resolution: str  # declared | inferred | unresolved
    basis: str
    observed_from: str | None = None
    observed_until: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "party": self.party,
            "resolution": self.resolution,
            "basis": self.basis,
            "observed_from": self.observed_from,
            "observed_until": self.observed_until,
        }


# Append-only. Correcting an entry means adding a superseding one with a new
# basis, never editing what a past reader already relied on.
ACTOR_ALIASES: tuple[ActorAlias, ...] = (
    ActorAlias(
        name="codex",
        party=None,
        resolution="unresolved",
        basis=(
            "a separate Codex session that wrote work items directly. It predates the "
            "ari/mori/moa naming and was never declared to be any of them, so it is not "
            "mapped onto a current party."
        ),
        observed_from="2026-09-01",
        observed_until="2026-09-02",
    ),
    ActorAlias(
        name="claude-code",
        party=None,
        resolution="unresolved",
        basis=(
            "an assignee label used before the rename. Some of that work was later "
            "continued by moa, but the label itself names a tool, not a party, and no "
            "record declares the mapping. Reported as legacy rather than rewritten."
        ),
        observed_from="2026-09-02",
        observed_until="2026-09-02",
    ),
    ActorAlias(
        name="claude-cowork",
        party=None,
        resolution="unresolved",
        basis=(
            "the pre-rename label for the Cowork operator seat now called mori. The "
            "seat is the same; whether a given record was written by today's mori is "
            "not recorded, so it is not asserted."
        ),
        observed_from="2026-09-02",
        observed_until="2026-09-02",
    ),
    ActorAlias(
        name="local-emergency",
        party=None,
        resolution="unresolved",
        basis="the local emergency administrator login, which carries no party identity.",
    ),
    ActorAlias(
        name="local-cli",
        party=None,
        resolution="unresolved",
        basis="the work CLI default actor when none was supplied.",
    ),
    ActorAlias(
        name="owner",
        party=None,
        resolution="unresolved",
        basis="the admin store's default actor for settings and secret changes.",
    ),
)

_ALIAS_BY_NAME = {alias.name: alias for alias in ACTOR_ALIASES}

# The date from which a bare party name in a record means that party. Records
# written before it carry the name by coincidence, not by declaration.
NAMING_EFFECTIVE_FROM = "2026-09-02"
NAMING_BASIS = (
    "observed: the ari/mori/moa naming appears in the mailbox handshake of "
    "2026-09-02 (msg_4bb85f383554c17c0e and its ACK)."
)


def _as_date(value: Any) -> date | None:
    """UTC calendar date of a timestamp, or None.

    A timestamp with no offset is read as UTC rather than as local time. The
    records this compares are written in UTC, and letting the reader's own
    timezone shift a date by a day would move records across the naming
    boundary depending on where the dashboard happens to run.
    """
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc).date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).date()


def resolve_actor(name: Any, *, at: Any = None) -> dict[str, Any]:
    """Which party an actor name stands for in a record written at ``at``.

    Never guesses. A party name used before the naming took effect is reported
    as ``inferred`` with the reason, not as ``declared``.
    """
    if not isinstance(name, str) or not name.strip():
        return {
            "name": None,
            "party": None,
            "resolution": "unresolved",
            "basis": "the record carries no actor",
        }
    actor = name.strip()
    alias = _ALIAS_BY_NAME.get(actor)
    if alias is not None:
        return alias.as_dict() | {"name": actor}
    if actor in PARTIES:
        written = _as_date(at)
        effective = _as_date(NAMING_EFFECTIVE_FROM)
        if written is not None and effective is not None and written < effective:
            return {
                "name": actor,
                "party": None,
                "resolution": "inferred",
                "basis": (
                    f"the name matches the party {actor!r} but the record predates "
                    f"{NAMING_EFFECTIVE_FROM}, when that naming took effect. "
                    + NAMING_BASIS
                ),
                "observed_from": None,
                "observed_until": None,
            }
        return {
            "name": actor,
            "party": actor,
            "resolution": "declared",
            "basis": NAMING_BASIS,
            "observed_from": NAMING_EFFECTIVE_FROM,
            "observed_until": None,
        }
    return {
        "name": actor,
        "party": None,
        "resolution": "unresolved",
        "basis": "the name is not a party and is not in the alias registry",
        "observed_from": None,
        "observed_until": None,
    }


# -------------------------------------------------------------- permissions

# What an unattended session may do with no explicit grant at all.
AUTONOMOUS_BASELINE = ("read", "investigate", "report", "test")

# Each is a separate opt-in, named by the directive that grants it.
GRANT_FLAGS = ("allow_write", "allow_commit", "allow_build", "allow_deploy")

GRANTED_ACTIONS = {
    "allow_write": "write",
    "allow_commit": "commit",
    "allow_build": "build",
    "allow_deploy": "deploy",
}

# Never granted by a message. These need a human decision every time, so there
# is deliberately no flag that turns them on.
NEVER_AUTONOMOUS = ("sudo", "delete", "policy_change", "security_control_change", "push")

MESSAGE_TYPES = ("ASSIGN", "ACK", "PROGRESS", "QUESTION", "REVIEW_REQUEST", "HANDOFF")

# A work item is part of the identity of these; a message without one cannot
# be tied to anything and is refused.
TYPES_REQUIRING_WORK_ID = ("ASSIGN", "PROGRESS", "REVIEW_REQUEST", "HANDOFF")

# Only these types can hand moa new authority. An ACK or a PROGRESS is a
# report, not an instruction.
TYPES_CARRYING_AUTHORITY = ("ASSIGN",)

_MESSAGE_ID = re.compile(r"^msg_[0-9a-f]{16,}$")
_WORK_ID = re.compile(r"^wi_[0-9a-f]{8,}$")
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")


@dataclass(frozen=True)
class DirectiveVerdict:
    """Whether a directive may be acted on, and exactly why not when it may not."""

    accepted: bool
    reasons: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    granted: tuple[str, ...] = ()
    message_id: str | None = None
    work_id: str | None = None
    expected_revision: int | None = None
    sender: str | None = None
    message_type: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "reasons": list(self.reasons),
            "warnings": list(self.warnings),
            "granted": list(self.granted),
            "message_id": self.message_id,
            "work_id": self.work_id,
            "expected_revision": self.expected_revision,
            "sender": self.sender,
            "message_type": self.message_type,
        }


def granted_actions(front_matter: Mapping[str, Any]) -> tuple[str, ...]:
    """Actions a directive explicitly grants, on top of the baseline.

    Only a literal ``true`` grants. A missing flag, a string ``"true"``, a
    number, or anything else leaves the action ungranted -- a permission must
    be stated, not coaxed out of a loose value.
    """
    actions = list(AUTONOMOUS_BASELINE)
    for flag in GRANT_FLAGS:
        if front_matter.get(flag) is True:
            actions.append(GRANTED_ACTIONS[flag])
    return tuple(actions)


def validate_directive(
    front_matter: Any,
    *,
    item: Mapping[str, Any] | None = None,
    recipient: str = MOA,
    already_processed: Iterable[str] = (),
) -> DirectiveVerdict:
    """Fail-closed check of one mailbox message before anything is executed.

    ``item`` is the work item read from the backoffice, not from the message.
    The message states what the sender believed; the item is what is true now,
    and a disagreement refuses rather than overwrites.
    """
    reasons: list[str] = []
    warnings: list[str] = []

    if not isinstance(front_matter, Mapping):
        return DirectiveVerdict(False, ("front matter is missing or is not a mapping",))

    message_id = front_matter.get("message_id")
    message_id = message_id if isinstance(message_id, str) else None
    sender = front_matter.get("from")
    sender = sender.strip() if isinstance(sender, str) else None
    message_type = front_matter.get("type")
    message_type = message_type if isinstance(message_type, str) else None
    work_id = front_matter.get("work_id")
    work_id = work_id if isinstance(work_id, str) and work_id else None
    revision = front_matter.get("expected_revision")
    revision = revision if isinstance(revision, int) and not isinstance(revision, bool) else None

    def verdict(accepted: bool) -> DirectiveVerdict:
        return DirectiveVerdict(
            accepted=accepted,
            reasons=tuple(reasons),
            warnings=tuple(warnings),
            granted=granted_actions(front_matter) if accepted else (),
            message_id=message_id,
            work_id=work_id,
            expected_revision=revision,
            sender=sender,
            message_type=message_type,
        )

    if front_matter.get("protocol_version") != 1:
        reasons.append("protocol_version must be 1")
    if message_id is None:
        reasons.append("message_id is missing")
    elif not _MESSAGE_ID.match(message_id):
        # Not fatal: the id still addresses the message. Recorded so a
        # malformed id cannot pass unnoticed.
        warnings.append(
            "message_id does not match msg_ followed by at least 16 lowercase hex characters"
        )
    if message_id is not None and message_id in set(already_processed):
        reasons.append("this message has already been processed; refusing to run it twice")

    if sender is None:
        reasons.append("from is missing")
    elif sender not in DIRECTING_PARTIES:
        reasons.append(
            f"{sender!r} may not direct {recipient}; only {', '.join(sorted(DIRECTING_PARTIES))} may"
        )
    recipient_field = front_matter.get("to")
    if recipient_field != recipient:
        reasons.append(f"the message is addressed to {recipient_field!r}, not to {recipient}")

    if message_type is None:
        reasons.append("type is missing")
    elif message_type not in MESSAGE_TYPES:
        reasons.append(f"type {message_type!r} is not one of {', '.join(MESSAGE_TYPES)}")
    elif message_type not in TYPES_CARRYING_AUTHORITY:
        reasons.append(
            f"a {message_type} carries no authority to act; only "
            f"{', '.join(TYPES_CARRYING_AUTHORITY)} does"
        )

    if message_type in TYPES_REQUIRING_WORK_ID and work_id is None:
        reasons.append(f"a {message_type} requires a work_id")
    if work_id is not None and not _WORK_ID.match(work_id):
        reasons.append(f"work_id {work_id!r} is not a valid work item id")

    if "reply_to" not in front_matter:
        warnings.append("reply_to is a required front-matter field, even when null")

    for flag in GRANT_FLAGS:
        if flag in front_matter and front_matter[flag] is not True and front_matter[flag] is not False:
            warnings.append(f"{flag} is not a boolean and is treated as not granted")

    if work_id is not None:
        if item is None:
            reasons.append("the work item named by this directive could not be read")
        else:
            if item.get("id") != work_id:
                reasons.append("the work item read does not match the directive's work_id")
            if item.get("assigned_to") != recipient:
                reasons.append(
                    f"the work item is assigned to {item.get('assigned_to')!r}, not to {recipient}"
                )
            if revision is None:
                reasons.append("expected_revision is required to act on a work item")
            elif item.get("revision") != revision:
                reasons.append(
                    f"expected_revision {revision} does not match the item's current "
                    f"revision {item.get('revision')}; refusing rather than overwriting"
                )

    return verdict(not reasons)


def is_safe_segment(value: Any) -> bool:
    """One path segment that cannot traverse. Used before any mailbox join."""
    return isinstance(value, str) and bool(_SAFE_SEGMENT.match(value)) and ".." not in value


def registry_as_dict() -> dict[str, Any]:
    return {
        "schema_version": COWORK_SCHEMA_VERSION,
        "parties": {name: PARTY_ROLES[name] for name in PARTIES},
        "authority_order": list(AUTHORITY_ORDER),
        "naming_effective_from": NAMING_EFFECTIVE_FROM,
        "naming_basis": NAMING_BASIS,
        "aliases": [alias.as_dict() for alias in ACTOR_ALIASES],
        "autonomous_baseline": list(AUTONOMOUS_BASELINE),
        "grant_flags": list(GRANT_FLAGS),
        "never_autonomous": list(NEVER_AUTONOMOUS),
        "message_types": list(MESSAGE_TYPES),
        "types_carrying_authority": list(TYPES_CARRYING_AUTHORITY),
    }
