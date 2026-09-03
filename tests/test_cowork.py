"""Cowork identity resolution and fail-closed directive authority.

Two things these tests exist to hold down: a historical actor name is never
silently relabelled onto a current party, and a message never grants more than
it explicitly states.
"""

from __future__ import annotations

from typing import Any

import pytest

from rlwrld_worklog.cowork import (
    ACTOR_ALIASES,
    ARI,
    AUTHORITY_ORDER,
    AUTONOMOUS_BASELINE,
    DIRECTING_PARTIES,
    GRANT_FLAGS,
    HK,
    MOA,
    MORI,
    NAMING_EFFECTIVE_FROM,
    NEVER_AUTONOMOUS,
    PARTIES,
    authority_rank,
    granted_actions,
    is_safe_segment,
    registry_as_dict,
    resolve_actor,
    resolve_conflict,
    validate_directive,
)

ITEM = {"id": "wi_1966a2435fb7b710", "assigned_to": MOA, "revision": 2}


def directive(**overrides: Any) -> dict[str, Any]:
    base = {
        "protocol_version": 1,
        "message_id": "msg_" + "a" * 16,
        "from": ARI,
        "to": MOA,
        "type": "ASSIGN",
        "work_id": ITEM["id"],
        "expected_revision": ITEM["revision"],
        "reply_to": None,
        "requires_ack": True,
        "subject": "x",
    }
    base.update(overrides)
    return {key: value for key, value in base.items() if value is not ...}


# ------------------------------------------------------------- identities


def test_authority_is_hk_then_ari_then_mori() -> None:
    assert AUTHORITY_ORDER == (HK, ARI, MORI)
    assert authority_rank(HK) < authority_rank(ARI) < authority_rank(MORI)
    assert authority_rank(MOA) == len(AUTHORITY_ORDER)
    assert resolve_conflict([MORI, ARI]) == ARI
    assert resolve_conflict([MORI, HK, ARI]) == HK
    assert resolve_conflict([MOA, "stranger"]) is None
    assert MOA not in DIRECTING_PARTIES


def test_a_party_name_after_the_naming_took_effect_is_declared() -> None:
    resolved = resolve_actor(MOA, at="2026-09-02T07:22:00+00:00")
    assert resolved["party"] == MOA
    assert resolved["resolution"] == "declared"


def test_a_party_name_before_the_naming_took_effect_is_only_inferred() -> None:
    """Rule 5: a pre-rename record must not be re-read as today's party."""
    resolved = resolve_actor(MOA, at="2026-09-01T04:57:06+00:00")
    assert resolved["party"] is None
    assert resolved["resolution"] == "inferred"
    assert NAMING_EFFECTIVE_FROM in resolved["basis"]


def test_a_naive_timestamp_is_read_as_utc_not_as_local_time() -> None:
    """Otherwise the reader's timezone could move a record across the boundary."""
    assert resolve_actor(MOA, at="2026-09-02T00:30:00")["resolution"] == "declared"
    assert resolve_actor(MOA, at="2026-09-01T23:30:00")["resolution"] == "inferred"


@pytest.mark.parametrize("name", ["codex", "claude-code", "claude-cowork"])
def test_legacy_names_are_reported_unresolved_never_mapped_onto_a_party(name: str) -> None:
    resolved = resolve_actor(name, at="2026-09-02T00:00:00+00:00")
    assert resolved["party"] is None
    assert resolved["resolution"] == "unresolved"
    assert resolved["basis"]


def test_an_unknown_name_and_a_missing_name_both_stay_unresolved() -> None:
    assert resolve_actor("nobody")["resolution"] == "unresolved"
    assert resolve_actor(None)["resolution"] == "unresolved"
    assert resolve_actor("   ")["party"] is None


def test_the_alias_registry_is_unique_and_explains_every_entry() -> None:
    names = [alias.name for alias in ACTOR_ALIASES]
    assert len(names) == len(set(names))
    for alias in ACTOR_ALIASES:
        assert alias.basis
        assert alias.resolution in {"declared", "inferred", "unresolved"}
        assert alias.name not in PARTIES, "a party must not also be an alias"


# ------------------------------------------------------------ permissions


def test_the_baseline_grants_only_read_investigate_report_and_test() -> None:
    assert granted_actions({}) == AUTONOMOUS_BASELINE
    assert set(granted_actions({})) == {"read", "investigate", "report", "test"}
    for forbidden in NEVER_AUTONOMOUS:
        assert forbidden not in granted_actions({flag: True for flag in GRANT_FLAGS})


def test_each_grant_is_a_separate_explicit_opt_in() -> None:
    assert "write" in granted_actions({"allow_write": True})
    assert "commit" not in granted_actions({"allow_write": True})
    every = granted_actions({flag: True for flag in GRANT_FLAGS})
    assert set(every) == set(AUTONOMOUS_BASELINE) | {"write", "commit", "build", "deploy"}


@pytest.mark.parametrize("value", ["true", "yes", 1, "1", [], {}, None, False])
def test_only_a_literal_true_grants_an_action(value: Any) -> None:
    """A permission has to be stated, not coaxed out of a truthy value."""
    assert "write" not in granted_actions({"allow_write": value})


# ------------------------------------------------------- directive checks


def test_a_well_formed_assign_from_ari_is_accepted() -> None:
    verdict = validate_directive(directive(allow_write=True), item=ITEM)
    assert verdict.accepted is True
    assert verdict.reasons == ()
    assert "write" in verdict.granted


@pytest.mark.parametrize("sender", ["moa", "codex", "stranger", "", None])
def test_only_hk_ari_and_mori_may_direct_moa(sender: Any) -> None:
    verdict = validate_directive(directive(**{"from": sender}), item=ITEM)
    assert verdict.accepted is False


def test_hk_and_mori_may_also_direct() -> None:
    for sender in (HK, MORI):
        assert validate_directive(directive(**{"from": sender}), item=ITEM).accepted


@pytest.mark.parametrize("kind", ["ACK", "PROGRESS", "QUESTION", "REVIEW_REQUEST", "HANDOFF"])
def test_only_an_assign_carries_authority_to_act(kind: str) -> None:
    verdict = validate_directive(directive(type=kind), item=ITEM)
    assert verdict.accepted is False
    assert any("carries no authority" in reason for reason in verdict.reasons)


def test_a_revision_conflict_refuses_instead_of_overwriting() -> None:
    verdict = validate_directive(directive(expected_revision=9), item=ITEM)
    assert verdict.accepted is False
    assert any("refusing rather than overwriting" in reason for reason in verdict.reasons)


def test_a_missing_expected_revision_is_refused() -> None:
    payload = directive()
    payload.pop("expected_revision")
    assert validate_directive(payload, item=ITEM).accepted is False


def test_an_item_assigned_to_someone_else_is_refused() -> None:
    verdict = validate_directive(directive(), item=dict(ITEM, assigned_to=ARI))
    assert verdict.accepted is False
    assert any("assigned to" in reason for reason in verdict.reasons)


def test_a_directive_whose_item_cannot_be_read_is_refused() -> None:
    assert validate_directive(directive(), item=None).accepted is False


def test_an_already_processed_message_is_never_run_twice() -> None:
    payload = directive()
    verdict = validate_directive(payload, item=ITEM, already_processed=[payload["message_id"]])
    assert verdict.accepted is False
    assert any("already been processed" in reason for reason in verdict.reasons)


def test_a_message_addressed_to_someone_else_is_refused() -> None:
    assert validate_directive(directive(to=ARI), item=ITEM).accepted is False


@pytest.mark.parametrize(
    "payload", [None, "a string", 42, [], {"protocol_version": 2}, {}],
)
def test_validation_is_fail_closed_for_anything_unparseable(payload: Any) -> None:
    verdict = validate_directive(payload, item=ITEM)
    assert verdict.accepted is False
    assert verdict.granted == ()
    assert verdict.reasons


def test_an_assign_without_a_work_id_is_refused() -> None:
    payload = directive()
    payload.pop("work_id")
    assert validate_directive(payload, item=ITEM).accepted is False


def test_a_malformed_message_id_warns_but_does_not_block() -> None:
    """Ari's own assignment used a non-conforming id; it must still be actionable."""
    verdict = validate_directive(directive(message_id="msg_ari_wi1966_impl_v1"), item=ITEM)
    assert verdict.accepted is True
    assert any("message_id" in warning for warning in verdict.warnings)


def test_a_missing_reply_to_warns_but_does_not_block() -> None:
    payload = directive()
    payload.pop("reply_to")
    verdict = validate_directive(payload, item=ITEM)
    assert verdict.accepted is True
    assert any("reply_to" in warning for warning in verdict.warnings)


@pytest.mark.parametrize(
    "name", ["..", "../etc", "a/b", "/abs", "", ".hidden", "-x", "x" * 200, "sp ace"],
)
def test_path_segments_that_could_traverse_are_refused(name: str) -> None:
    assert is_safe_segment(name) is False


def test_a_normal_mailbox_filename_is_a_safe_segment() -> None:
    assert is_safe_segment("20260902T110000Z--msg_abc--wi_123.md") is True


def test_the_registry_publishes_the_rules_it_enforces() -> None:
    payload = registry_as_dict()
    assert payload["authority_order"] == list(AUTHORITY_ORDER)
    assert set(payload["parties"]) == set(PARTIES)
    assert payload["autonomous_baseline"] == list(AUTONOMOUS_BASELINE)
    assert payload["types_carrying_authority"] == ["ASSIGN"]
    for forbidden in ("sudo", "push", "policy_change", "security_control_change"):
        assert forbidden in payload["never_autonomous"]
