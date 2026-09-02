from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from conftest_ledger import build_notion_tree  # noqa: E402

from rlwrld_worklog.ledger.common import MetaSignalResolver, text_hash  # noqa: E402
from rlwrld_worklog.ledger.legacy_notion import NotionConvertStats, iter_notion_records  # noqa: E402
from rlwrld_worklog.ledger.schema import (  # noqa: E402
    ExtractedText,
    LedgerRecord,
    ledger_id_for,
    validate_record,
)


def convert(tmp_path: Path, *, salvage_comments: bool = False):
    root = build_notion_tree(tmp_path / "legacy")
    stats = NotionConvertStats()
    items = list(
        iter_notion_records(
            root,
            stats=stats,
            meta_resolver=MetaSignalResolver(root),
            salvage_comments=salvage_comments,
        )
    )
    records = [item for item in items if isinstance(item, LedgerRecord)]
    artifacts = [item for item in items if isinstance(item, ExtractedText)]
    return root, stats, records, artifacts


def test_pages_and_blocks_are_separate_entities(tmp_path):
    _, stats, records, _ = convert(tmp_path)
    pages = [r for r in records if r.entity_type == "page"]
    blocks = [r for r in records if r.entity_type == "block"]
    # 2 pages in the current layout + 1 in the pre-2026-04 layout
    assert len(pages) == 3
    # one top-level block and its child
    assert len(blocks) == 2
    assert stats.blocks_converted == 2


def test_attribution_pages_are_never_converted(tmp_path):
    _, stats, records, _ = convert(tmp_path)
    assert stats.files_skipped_container >= 1
    assert not any("attribution" in r.provenance["source_file"] for r in records)


def test_comments_are_only_salvaged_when_asked(tmp_path):
    _, _, records, _ = convert(tmp_path)
    assert not [r for r in records if r.entity_type == "comment"]

    _, stats, records, _ = convert(tmp_path, salvage_comments=True)
    comments = [r for r in records if r.entity_type == "comment"]
    assert len(comments) == 1
    comment = comments[0]
    # The salvage path stays reversible with a single provenance filter.
    assert comment.provenance["source_file_kind"] == "attribution"
    assert comment.capture_profile.endswith("from-attribution/v1")
    assert comment.coverage["permission_gap"]["reason"].startswith("comments_only_exist")
    assert stats.comments_converted == 1


def test_blocks_text_is_a_separate_lossless_artifact(tmp_path):
    _, stats, records, artifacts = convert(tmp_path)
    assert len(artifacts) == 1
    artifact = artifacts[0]
    assert artifact.kind == "notion_blocks_text"
    assert artifact.text == "synthetic flattened body"
    assert artifact.char_length == len(artifact.text)
    assert artifact.extractor == "legacy-notion-collector/_blocks_text"
    # it is linked to its page but not folded into raw_payload
    page = next(r for r in records if r.ledger_id == artifact.ledger_id)
    assert page.entity_type == "page"
    # Artifact identity is scoped to the immutable page observation.  Two
    # legacy layouts may contain the same page and flattened text while the
    # enclosing observations differ; those must not collide in the DB.
    assert artifact.artifact_id == ledger_id_for(
        source="notion",
        entity_type="extracted_text",
        tenant_id="unknown",
        scope_key=page.source_entity_id,
        source_entity_id=page.ledger_id,
        window_start=page.observation_window["start"],
        content_hash=text_hash(artifact.text),
    )
    assert stats.extracted_text_artifacts == 1


def test_raw_payload_keeps_the_page_verbatim(tmp_path):
    _, _, records, _ = convert(tmp_path)
    page = next(
        r for r in records if r.source_entity_id == "aaaaaaaa-0000-0000-0000-000000000001"
    )
    assert page.raw_payload["_blocks_text"] == "synthetic flattened body"
    assert page.raw_payload["properties"]["Owner"] == "<relation>"


def test_lossy_property_labels_are_counted(tmp_path):
    _, stats, records, _ = convert(tmp_path)
    page = next(
        r for r in records if r.source_entity_id == "aaaaaaaa-0000-0000-0000-000000000001"
    )
    assert page.capture_completeness["lossy_fields"] == {"<relation>": 1, "<formula>": 1}
    assert page.relations["relation_properties_recoverable"] is False
    assert stats.property_type_labels == 6  # 3 pages x 2 labels each


def test_archived_state_is_unknown(tmp_path):
    _, _, records, _ = convert(tmp_path)
    for record in records:
        assert record.deleted_state["status"] == "unknown"


def test_legacy_layout_is_recorded_as_parser_provenance(tmp_path):
    _, stats, records, _ = convert(tmp_path)
    old = next(
        r for r in records if r.source_entity_id == "aaaaaaaa-0000-0000-0000-000000000003"
    )
    assert old.provenance["legacy_layout_version"] == "legacy_no_source_dir"
    # the old layout has no _meta at all
    assert old.visibility_routing["meta_present"] is False
    assert stats.legacy_layout_pages == 1
    current = next(
        r for r in records if r.source_entity_id == "aaaaaaaa-0000-0000-0000-000000000002"
    )
    assert current.provenance["legacy_layout_version"] == "current"


def test_notion_has_no_tenant_and_says_so(tmp_path):
    _, _, records, _ = convert(tmp_path)
    for record in records:
        assert record.tenant == {"workspace_id": "unknown", "status": "unknown"}


def test_blocks_record_their_depth_limit(tmp_path):
    _, _, records, _ = convert(tmp_path)
    blocks = [r for r in records if r.entity_type == "block"]
    child = next(r for r in blocks if r.source_entity_id.startswith("cccccccc"))
    assert child.relations["parent_block_id"] == "bbbbbbbb-0000-0000-0000-000000000001"
    assert child.relations["children_complete"] is False
    assert child.coverage["permission_gap"]["reason"] == "block_recursion_depth_capped_at_3"
    assert child.provenance["record_pointer"].endswith("/_children/0")


def test_every_record_validates_and_is_traceable(tmp_path):
    root, _, records, _ = convert(tmp_path, salvage_comments=True)
    for record in records:
        assert validate_record(record.to_dict()) == []
        assert (root / record.provenance["source_file"]).is_file()
        assert record.provenance["source_file_sha256"].startswith("sha256:")


def test_notion_completeness_is_not_recorded_by_the_collector(tmp_path):
    _, _, records, _ = convert(tmp_path)
    page = next(
        r for r in records if r.source_entity_id == "aaaaaaaa-0000-0000-0000-000000000001"
    )
    # meta.json exists and says ok, but records no truncation or rate-limit
    # signal, so completeness is "not_recorded" rather than "clean".
    assert page.capture_completeness["status"] == "not_recorded"
    assert page.capture_completeness["legacy_status_is_trustworthy"] is False


def test_blocks_do_not_inherit_page_property_losses(tmp_path):
    _, _, records, _ = convert(tmp_path)
    page = next(
        r for r in records if r.source_entity_id == "aaaaaaaa-0000-0000-0000-000000000001"
    )
    assert page.capture_completeness["lossy_fields"]
    for block in (r for r in records if r.entity_type == "block"):
        assert block.capture_completeness["lossy_fields"] == {}
