"""What each source's extractor pulls out of a raw payload.

The payload shapes are the APIs' own: a Slack message, a Notion block's rich
text, a Calendar event, a GitHub commit listing, a Slurm job row. The point of
these tests is the contract that `None` means "genuinely nothing to search",
never "the shape surprised the extractor".
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rlwrld_worklog.ledger.extract_text import (  # noqa: E402
    _MAX_TEXT_CHARS,
    document_id,
    extract_text,
)


def test_a_slack_message_yields_its_text_and_attachment_text() -> None:
    text = extract_text(
        "slack",
        "message",
        {
            "text": "배포 끝났습니다",
            "attachments": [{"title": "release v2", "text": "changelog", "fallback": "release v2"}],
            "files": [{"title": "실험 결과.pdf"}],
        },
    )
    assert "배포 끝났습니다" in text
    assert "release v2" in text and "changelog" in text
    assert "실험 결과.pdf" in text
    # The fallback duplicates the title; a document should not say it twice.
    assert text.count("release v2") == 1


def test_a_slack_join_event_with_no_text_is_none() -> None:
    assert extract_text("slack", "message", {"subtype": "channel_join", "text": ""}) is None


def test_a_notion_block_yields_every_plain_text_wherever_it_nests() -> None:
    text = extract_text(
        "notion",
        "block",
        {
            "type": "paragraph",
            "paragraph": {
                "rich_text": [
                    {"type": "text", "plain_text": "수집 주기를", "annotations": {}},
                    {"type": "mention", "plain_text": "@민수", "mention": {}},
                ]
            },
        },
    )
    assert text == "수집 주기를\n@민수"


def test_a_notion_page_title_comes_from_its_properties() -> None:
    text = extract_text(
        "notion",
        "page",
        {"properties": {"title": {"title": [{"plain_text": "주간 회의록"}]}}},
    )
    assert text == "주간 회의록"


def test_a_notion_divider_is_none() -> None:
    assert extract_text("notion", "block", {"type": "divider", "divider": {}}) is None


def test_a_calendar_event_yields_summary_description_location() -> None:
    text = extract_text(
        "google_calendar",
        "event",
        {"summary": "로봇랩 정기회의", "description": "아젠다: 수집", "location": "3층"},
    )
    assert text == "로봇랩 정기회의\n아젠다: 수집\n3층"


def test_a_github_commit_yields_its_message() -> None:
    text = extract_text(
        "github", "commit", {"sha": "abc", "commit": {"message": "Fix the walk"}}
    )
    assert "Fix the walk" in text


def test_a_github_pull_request_yields_title_and_body() -> None:
    text = extract_text("github", "pull_request", {"title": "V9", "body": "pre-window", "state": "open"})
    assert text.startswith("V9\npre-window")


def test_a_slurm_job_yields_its_name_and_owner() -> None:
    text = extract_text(
        "slurm", "job", {"job_name": "vla-train-7b", "partition": "gpu", "user": "mskim"}
    )
    assert "vla-train-7b" in text and "mskim" in text


def test_an_unknown_source_and_a_non_dict_payload_are_none() -> None:
    assert extract_text("carrier-pigeon", "message", {"text": "hi"}) is None
    assert extract_text("slack", "message", "not a dict") is None
    assert extract_text("slack", "message", None) is None


def test_a_pathological_payload_is_clipped_not_indexed_whole() -> None:
    text = extract_text("slack", "message", {"text": "가" * (_MAX_TEXT_CHARS * 2)})
    assert len(text) == _MAX_TEXT_CHARS


def test_the_document_id_matches_what_the_sql_side_computes() -> None:
    """Python's md5-uuid must equal md5(source || ':' || entity)::uuid."""
    import hashlib
    import uuid

    key = "slack:C0123/1725900000.000100"
    expected = str(uuid.UUID(hashlib.md5(key.encode()).hexdigest()))
    assert document_id("slack", "C0123/1725900000.000100") == expected
