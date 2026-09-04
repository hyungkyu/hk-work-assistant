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
    RULE_STATUSES,
    RULES,
    SOURCES,
    CollectionRule,
    RuleRegistryError,
    _validate_registry,
    active_rule,
    active_rule_stamp,
    effective_window,
    registry_as_dict,
    rule_digest_mismatches,
    rule_for_version,
    stamp_from_manifest,
)
from rlwrld_worklog.ledger.schema import LEDGER_SCHEMA_VERSION

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src" / "rlwrld_worklog"
_NOTE_KEY = re.compile(r'"([a-z_]+\.[a-z0-9_]+): ')


def test_every_published_version_is_declared_once_and_defines_known_sources() -> None:
    versions = [rule.version for rule in RULES]
    assert versions == sorted(set(versions), key=versions.index)
    assert len(versions) == len(set(versions))
    for rule in RULES:
        declared = {source.source for source in rule.sources}
        # A retired version defines what its collectors actually knew about, so
        # it may be a subset of today's sources -- but never something unknown.
        assert declared and declared <= set(SOURCES)
        assert len(declared) == len(rule.sources), "a source is defined twice"
        assert rule.title and rule.summary
        assert rule.status in RULE_STATUSES
        assert rule.effective.basis


def test_the_active_version_accounts_for_every_source_we_collect() -> None:
    assert {source.source for source in active_rule().sources} == set(SOURCES)


def test_a_retired_version_may_omit_a_source_added_after_it() -> None:
    """Adding a source must not force a false claim into an old rule.

    When a collector arrives, `SOURCES` grows. The versions published before it
    genuinely did not collect it, so back-dating a definition into them would
    make them lie. Only the active version has to cover the new source.
    """
    assert any(rule.status != "active" for rule in RULES), (
        "this invariant only means something once a version is retired"
    )
    # A synthetic version name, because narrowing a real one would move its
    # digest and the frozen-rule guard would fire first -- a different rule
    # than the one under test here.
    narrow = replace(
        RULES[0], version="V-narrow", status="superseded", sources=RULES[0].sources[:1]
    )
    _validate_registry((narrow, active_rule()))


def test_the_registry_refuses_a_version_that_defines_no_source() -> None:
    original = RULES[0]
    edited = replace(original, sources=())
    others = tuple(rule for rule in RULES if rule.version != original.version)
    with pytest.raises(RuleRegistryError, match="defines no source"):
        _validate_registry((edited,) + others)


def test_the_registry_refuses_a_version_that_defines_one_source_twice() -> None:
    original = RULES[0]
    edited = replace(original, sources=original.sources + (original.sources[0],))
    others = tuple(rule for rule in RULES if rule.version != original.version)
    with pytest.raises(RuleRegistryError, match="twice"):
        _validate_registry((edited,) + others)


def test_digest_is_stable_and_matches_what_was_published() -> None:
    for rule in RULES:
        assert rule.digest == rule.digest, "a digest must not depend on when it is taken"
        assert rule.digest.startswith("sha256:")
        if rule.status == "pending":
            # Not yet frozen: it is pinned by the same change that activates
            # it, so what gets pinned is what was in force from day one.
            assert rule.version not in PUBLISHED_DIGESTS
            continue
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
    others = tuple(rule for rule in RULES if rule.version != "V0")
    with pytest.raises(RuleRegistryError, match="frozen"):
        _validate_registry((edited,) + others)


def test_the_registry_refuses_an_active_rule_that_forgets_a_source() -> None:
    original = active_rule()
    edited = replace(original, sources=original.sources[:1])
    others = tuple(rule for rule in RULES if rule.version != original.version)
    with pytest.raises(RuleRegistryError, match="the active collection rule"):
        _validate_registry(others + (edited,))


def test_exactly_one_rule_is_active_and_it_is_the_stamped_one() -> None:
    active = [rule for rule in RULES if rule.status == "active"]
    assert [rule.version for rule in active] == [ACTIVE_RULE_VERSION]
    assert active_rule().version == ACTIVE_RULE_VERSION
    retired = tuple(replace(rule, status="superseded") for rule in RULES)
    with pytest.raises(RuleRegistryError, match="must be active"):
        _validate_registry(retired)


def test_a_versions_end_is_derived_from_its_successor_not_stored() -> None:
    """Storing an end would mean editing a published rule when it is retired.

    The same trap `status` was in: a fact that only exists once a successor
    exists cannot live inside digest-frozen content, or publishing the
    successor would have to reach back and forge the predecessor.
    """
    for rule in RULES:
        assert rule.effective.end is None, (
            f"{rule.version} stores an effective end; it must be derived"
        )
    stored = replace(
        RULES[0],
        effective=replace(RULES[0].effective, end="2026-09-02"),
    )
    others = tuple(rule for rule in RULES if rule.version != RULES[0].version)
    with pytest.raises(RuleRegistryError, match="stores an effective end"):
        _validate_registry((stored,) + others)


def test_the_derived_window_closes_a_retired_version_at_its_successors_start() -> None:
    superseded = [rule for rule in RULES if rule.status == "superseded"]
    assert superseded, "this only means something once a version is retired"
    for rule in superseded:
        successor = next(
            (later for later in RULES if later.supersedes == rule.version), None
        )
        window = effective_window(rule.version)
        if successor is None:
            # Retired without a named successor: nothing to derive an end from,
            # and inventing one would be worse than leaving it open.
            assert window["end"] is None
            assert window["superseded_by"] is None
        else:
            assert window["end"] == successor.effective.start
            assert window["superseded_by"] == successor.version
        assert window["is_current"] is False


def test_a_pending_version_does_not_close_the_version_it_will_supersede() -> None:
    """Publishing ahead of the code must not retire anything.

    A pending version names its predecessor so the repair has somewhere to
    land, but no run has followed it, so the predecessor has not stopped
    applying. Deriving an end from a start that does not exist yet would
    close the active version's window against `None` and leave the registry
    claiming nothing is current.
    """
    pending = [rule for rule in RULES if rule.status == "pending"]
    assert pending, "this invariant only means something while a version is pending"
    for rule in pending:
        assert rule.effective.start is None
        assert rule.version not in PUBLISHED_DIGESTS, "a rule freezes when it takes effect"
        window = effective_window(rule.version)
        assert window == {
            "start": None,
            "end": None,
            "superseded_by": None,
            "is_current": False,
        }
        predecessor = rule.supersedes
        if predecessor is not None:
            still_open = effective_window(predecessor)
            assert still_open["end"] is None
            assert still_open["superseded_by"] is None


def test_a_pending_version_stamps_nothing_and_activating_it_is_a_status_flip() -> None:
    """The reason `status` is outside the digest, exercised end to end."""
    pending = [rule for rule in RULES if rule.status == "pending"]
    assert pending
    for rule in pending:
        assert rule.version != ACTIVE_RULE_VERSION
        assert active_rule_stamp()["collection_rule_version"] != rule.version
        # Activation must not move the content hash, or the freeze that gets
        # pinned on the way in would differ from what was reviewed.
        assert replace(rule, status="active").digest == rule.digest


def test_the_registry_refuses_a_pending_version_that_claims_a_start() -> None:
    pending = next(rule for rule in RULES if rule.status == "pending")
    others = tuple(other for other in RULES if other.version != pending.version)
    started = replace(
        pending, effective=replace(pending.effective, start="2026-09-04")
    )
    with pytest.raises(RuleRegistryError, match="stores an effective start"):
        _validate_registry(others + (started,))


def test_the_derived_window_marks_only_the_active_version_current() -> None:
    current = [
        rule.version for rule in RULES if effective_window(rule.version)["is_current"]
    ]
    assert current == [ACTIVE_RULE_VERSION]
    active_window = effective_window(ACTIVE_RULE_VERSION)
    assert active_window["end"] is None
    assert active_window["superseded_by"] is None


def test_the_serialized_registry_carries_the_derived_window_per_rule() -> None:
    """The frozen prose can be stale; the view must not be.

    V1 and V2 were published with a basis saying they were still active, and
    the digest guard makes that text uncorrectable. A consumer reading the
    registry has to be able to get the truth from somewhere.
    """
    payload = registry_as_dict()
    for rule, published in zip(payload["rules"], RULES):
        window = rule["effective_window"]
        assert window == effective_window(published.version)
    current = [
        rule["version"] for rule in payload["rules"] if rule["effective_window"]["is_current"]
    ]
    assert current == [ACTIVE_RULE_VERSION]


def test_retiring_a_version_does_not_change_its_digest() -> None:
    """`status` is registry lifecycle, not a claim about what was collected.

    It used to be inside the digest, which meant superseding a version -- a
    thing any registry with two versions must do -- moved its digest and
    tripped the append-only check. The frozen thing is what the rule says
    about collection.
    """
    for rule in RULES:
        assert "status" not in rule.content()
        flipped = "superseded" if rule.status == "active" else "active"
        assert replace(rule, status=flipped).digest == rule.digest


def test_a_digest_recorded_under_the_earlier_schema_still_verifies() -> None:
    """Manifests on disk carry digests computed before `status` left the hash."""
    from rlwrld_worklog.collection_rules import HISTORICAL_DIGESTS, digest_is_recognised

    for version, digests in HISTORICAL_DIGESTS.items():
        for digest in digests:
            assert digest_is_recognised(version, digest), (version, digest)
        assert digest_is_recognised(version, rule_for_version(version).digest)
    assert not digest_is_recognised("V0", "sha256:" + "0" * 64)
    assert not digest_is_recognised("V0", None)


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


def test_the_registry_names_every_limitation_the_collectors_actually_record() -> None:
    """A collector that grows a new coverage note needs a new rule version.

    Checked against every published version, not just the active one: a note
    is named by whichever version introduced it, and older versions keep
    naming the notes that existed when they were written.
    """
    rule = active_rule()
    declared = {
        limitation.split(":", 1)[0]
        for published in RULES
        for source in published.sources
        for limitation in source.known_limitations
    }
    for module, source_name in (
        ("slack_collector", "slack"),
        ("notion_collector", "notion"),
        ("calendar_collector", "google_calendar"),
        # Added with V4. A collector outside this list can grow a coverage note
        # no rule names, which is exactly the drift the test exists to catch --
        # github collected a whole month before the registry knew the source.
        ("github_collector", "github"),
        # Added with V5, before slurm's first run. A manifest only reports the
        # notes that fired in that run, so the code is the only place a rule
        # can be checked against when there is no manifest yet.
        ("slurm_collector", "slurm"),
    ):
        text = (SOURCE_ROOT / f"{module}.py").read_text(encoding="utf-8")
        keys = set(_NOTE_KEY.findall(text))
        assert keys, f"{module} should record coverage note keys"
        missing = keys - declared
        assert not missing, (
            f"{module} records coverage notes {sorted(missing)} that no published rule "
            "names; append a new collection rule version rather than editing a published one"
        )
        assert rule.source_rule(source_name) is not None


def test_the_active_rule_pins_the_schema_versions_its_runs_actually_write() -> None:
    """If a schema version moves, the rule must be re-published, not patched."""
    rule = active_rule()
    assert rule.manifest_schema_version == archive.MANIFEST_SCHEMA_VERSION
    assert rule.ledger_schema_version == LEDGER_SCHEMA_VERSION
    # One profile per source the active rule declares, which is the property
    # that matters; the literal set changed three times in one evening.
    assert len(set(rule.capture_profiles)) == len(rule.sources)
    assert set(rule.capture_profiles) == {
        "live-slack-web-api/v1",
        "live-notion-api/v1",
        "live-google-calendar-api/v1",
        "live-github-api/v1",
        "live-slurm-sacct-dump/v1",
    }
    for source in rule.sources:
        assert source.density_kind in {
            "incremental_continuous",
            "incremental_or_date_slice",
            "full",
        }


def test_the_registry_serializes_with_a_digest_per_rule() -> None:
    payload = registry_as_dict()
    assert payload["active_version"] == ACTIVE_RULE_VERSION
    assert [rule["version"] for rule in payload["rules"]] == [rule.version for rule in RULES]
    for rule, published in zip(payload["rules"], RULES):
        assert rule["digest"] == published.digest
        assert {entry["source"] for entry in rule["sources"]} <= set(SOURCES)
    serialized_active = next(
        rule for rule in payload["rules"] if rule["version"] == ACTIVE_RULE_VERSION
    )
    assert {entry["source"] for entry in serialized_active["sources"]} == set(SOURCES)


def test_a_rule_is_frozen_at_runtime() -> None:
    rule: CollectionRule = RULES[0]
    with pytest.raises(Exception):
        rule.title = "changed"  # type: ignore[misc]
