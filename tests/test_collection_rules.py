"""The collection-rule registry is append-only and its digests are stable.

These tests are the enforcement mechanism for rule 4: a published rule may not
be edited, and the meaning of a version may not change under a reader's feet.
"""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

import pytest

from rlwrld_worklog import archive, collection_rules
from rlwrld_worklog.collection_rules import (
    ACTIVE_RULE_VERSION,
    PUBLISHED_DIGESTS,
    RULES,
    SOURCES,
    CollectionRule,
    RuleRegistryError,
    _validate_registry,
    active_rule,
    active_rule_stamp,
    registry_as_dict,
    rule_digest_mismatches,
    rule_for_version,
    stamp_from_manifest,
)
from rlwrld_worklog.ledger.schema import LEDGER_SCHEMA_VERSION

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src" / "rlwrld_worklog"
_NOTE_KEY = re.compile(r'"([a-z_]+\.[a-z0-9_]+): ')


def test_every_published_version_is_declared_once_and_covers_every_source() -> None:
    versions = [rule.version for rule in RULES]
    assert versions == sorted(set(versions), key=versions.index)
    assert len(versions) == len(set(versions))
    for rule in RULES:
        assert {source.source for source in rule.sources} == set(SOURCES)
        assert rule.title and rule.summary
        assert rule.status in {"active", "superseded"}
        assert rule.effective.basis


def test_digest_is_stable_and_matches_what_was_published() -> None:
    for rule in RULES:
        assert rule.digest == rule.digest, "a digest must not depend on when it is taken"
        assert rule.digest.startswith("sha256:")
        assert PUBLISHED_DIGESTS[rule.version] == rule.digest
    assert rule_digest_mismatches() == []
    assert registry_as_dict()["digests_pinned"] is True


def test_a_rule_edited_in_place_changes_its_digest() -> None:
    original = rule_for_version("V1")
    assert original is not None
    edited = replace(original, summary=original.summary + " and one more thing")
    assert edited.digest != original.digest


def test_the_registry_refuses_a_version_declared_twice() -> None:
    with pytest.raises(RuleRegistryError, match="declared twice"):
        _validate_registry((RULES[0], RULES[1], RULES[1]))


def test_the_registry_refuses_an_edited_published_rule() -> None:
    original = rule_for_version("V0")
    assert original is not None
    edited = replace(original, title="quietly rewritten")
    with pytest.raises(RuleRegistryError, match="frozen"):
        _validate_registry((edited, rule_for_version("V1")))


def test_the_registry_refuses_a_rule_that_forgets_a_source() -> None:
    original = rule_for_version("V1")
    assert original is not None
    edited = replace(original, sources=original.sources[:1])
    with pytest.raises(RuleRegistryError, match="does not define"):
        _validate_registry((rule_for_version("V0"), edited))


def test_exactly_one_rule_is_active_and_it_is_the_stamped_one() -> None:
    active = [rule for rule in RULES if rule.status == "active"]
    assert [rule.version for rule in active] == [ACTIVE_RULE_VERSION]
    assert active_rule().version == ACTIVE_RULE_VERSION
    with pytest.raises(RuleRegistryError, match="must be active"):
        _validate_registry((replace(RULES[1], status="superseded"),) + (RULES[0],))


def test_the_manifest_stamp_names_the_active_rule_and_its_digest() -> None:
    stamp = active_rule_stamp()
    assert stamp == {
        "collection_rule_version": ACTIVE_RULE_VERSION,
        "collection_rule_digest": active_rule().digest,
        "collection_rule_schema_version": collection_rules.RULE_REGISTRY_SCHEMA_VERSION,
    }
    assert stamp_from_manifest(stamp | {"other": 1}) == {
        "version": ACTIVE_RULE_VERSION,
        "digest": active_rule().digest,
        "schema_version": collection_rules.RULE_REGISTRY_SCHEMA_VERSION,
    }
    assert stamp_from_manifest({}) is None
    assert stamp_from_manifest({"collection_rule_version": ""}) is None


def test_v0_states_its_gaps_instead_of_implying_completeness() -> None:
    rule = rule_for_version("V0")
    assert rule is not None
    assert rule.effective.start is None and "unknown" in rule.effective.basis
    joined = " ".join(rule.unknowns)
    assert "미수집" in joined
    assert "hardcoded" in joined
    for source in rule.sources:
        assert source.density_kind == "day_slice"
        assert source.unknowns, f"{source.source} must name what V0 cannot assert"
        assert source.evidence


def test_v1_names_every_limitation_the_collectors_actually_record() -> None:
    """A collector that grows a new coverage note needs a new rule version."""
    rule = rule_for_version("V1")
    assert rule is not None
    declared = {
        limitation.split(":", 1)[0]
        for source in rule.sources
        for limitation in source.known_limitations
    }
    for module, source_name in (
        ("slack_collector", "slack"),
        ("notion_collector", "notion"),
        ("calendar_collector", "google_calendar"),
    ):
        text = (SOURCE_ROOT / f"{module}.py").read_text(encoding="utf-8")
        keys = set(_NOTE_KEY.findall(text))
        assert keys, f"{module} should record coverage note keys"
        missing = keys - declared
        assert not missing, (
            f"{module} records coverage notes {sorted(missing)} that no published rule "
            "names; append a new collection rule version rather than editing V1"
        )
        assert rule.source_rule(source_name) is not None


def test_v1_pins_the_schema_versions_its_runs_actually_write() -> None:
    """If a schema version moves, the rule must be re-published, not patched."""
    rule = rule_for_version("V1")
    assert rule is not None
    assert rule.manifest_schema_version == archive.MANIFEST_SCHEMA_VERSION
    assert rule.ledger_schema_version == LEDGER_SCHEMA_VERSION
    assert set(rule.capture_profiles) == {
        "live-slack-web-api/v1",
        "live-notion-api/v1",
        "live-google-calendar-api/v1",
    }
    for source in rule.sources:
        assert source.density_kind == "incremental_continuous"


def test_the_registry_serializes_with_a_digest_per_rule() -> None:
    payload = registry_as_dict()
    assert payload["active_version"] == ACTIVE_RULE_VERSION
    assert [rule["version"] for rule in payload["rules"]] == [rule.version for rule in RULES]
    for rule, published in zip(payload["rules"], RULES):
        assert rule["digest"] == published.digest
        assert "sources" in rule and len(rule["sources"]) == len(SOURCES)


def test_a_rule_is_frozen_at_runtime() -> None:
    rule: CollectionRule = RULES[0]
    with pytest.raises(Exception):
        rule.title = "changed"  # type: ignore[misc]
