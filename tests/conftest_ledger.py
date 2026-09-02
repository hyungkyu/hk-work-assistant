"""Synthetic legacy trees for ledger tests.

Every identifier and every piece of text here is invented. No collected data
is committed to this repository (docs/data-policy.md).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

WORKSPACE = "T0TESTWS01"
CHANNEL = "C0TESTCH01"
USER_A = "U0TESTAAA1"
USER_B = "U0TESTBBB2"


def write_json(path: Path, payload: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def slack_message(ts: str, *, user: str = USER_A, **overrides: Any) -> dict[str, Any]:
    message = {
        "channel_id": CHANNEL,
        "channel_name": "test-channel",
        "ts": ts,
        "user": user,
        "type": "message",
        "subtype": None,
        "text": "synthetic message body",
        "thread_ts": None,
        "reply_count": 0,
        "reactions": [],
        "files": [],
        "permalink": f"https://example.invalid/archives/{CHANNEL}/p{ts.replace('.', '')}",
    }
    message.update(overrides)
    return message


def build_slack_tree(root: Path, *, day: str = "2026-05-01") -> Path:
    """shared common channel, a personal private channel, and the traps:
    an attribution bucket, an rsync-partial leftover, and a DM under shared."""
    write_json(
        root / "shared" / "daily_raw" / day / "slack" / "all_users.json",
        {
            "fetched_at": f"{day}T23:59:00+09:00",
            "total": 2,
            "users": [
                {"id": USER_A, "team_id": WORKSPACE, "name": "user-a", "deleted": False},
                {"id": USER_B, "team_id": WORKSPACE, "name": "user-b", "deleted": False},
            ],
            "_meta": {"visibility": "public", "source_schema_version": "1.0"},
        },
    )
    write_json(
        root / "shared" / "daily_raw" / day / "slack" / "meta.json",
        {
            "status": "ok",
            "collection_time": f"{day}T23:59:00",
            "rate_limit_hits": 7,
            "truncation_warnings": [{"where": "test-channel", "detail": "hour chunk skipped"}],
            "channel_scan": {
                "scanned_channels": ["a", "b", "c"],
                "empty_channels": ["b"],
                "active_channels": ["a", "c"],
            },
        },
    )
    write_json(
        root / "shared" / "daily_raw" / day / "slack" / "common" / "test-channel.json",
        {
            "channel_name": "test-channel",
            "channel_id": CHANNEL,
            "is_private": False,
            "date_range": {"start": day, "end": day},
            "collected_at": f"{day}T23:59:00",
            "message_count": 3,
            "messages": [
                slack_message("1777000000.000100"),
                slack_message(
                    "1777000100.000200",
                    user=USER_B,
                    thread_ts="1777000000.000100",
                    reply_count=1,
                ),
                # search-supplement shape: username/edited, no permalink
                {
                    "channel_id": CHANNEL,
                    "channel_name": "test-channel",
                    "ts": "1777000200.000300",
                    "user": USER_A,
                    "type": "message",
                    "subtype": None,
                    "text": "synthetic supplemented body",
                    "thread_ts": None,
                    "reply_count": 0,
                    "reactions": [],
                    "files": [],
                    "username": "user-a",
                    "edited": {"user": USER_A, "ts": "1777000250.000000"},
                    "_supplemented": True,
                },
            ],
            "_supplemented_runs": [{"ts": "1777000900.000000"}],
            "_meta": {
                "visibility": "public",
                "access_list": None,
                "collected_by": "tester",
                "collected_at": f"{day}T23:59:00+09:00",
                "source_schema_version": "1.0",
            },
        },
    )
    # attribution bucket: must never be converted
    write_json(
        root / "shared" / "daily_raw" / day / "slack" / "attribution" / "person.json",
        {
            "uid": "P1",
            "slack_uid": USER_A,
            "date_range": {"start": day, "end": day},
            "summary": {"sent": 1, "mentioned": 0, "replied_to_me": 0, "reacted_to_me": 0},
            "sent": [slack_message("1777000000.000100")],
            "_meta": {"visibility": "public", "source_schema_version": "1.0"},
        },
    )
    # abandoned transfer: parses fine, must still be excluded by path
    write_json(
        root / "shared" / "daily_raw" / day / "slack" / "common" / ".rsync-partial" / "test-channel.json",
        {
            "channel_id": CHANNEL,
            "message_count": 1,
            "messages": [slack_message("1777000000.000100")],
        },
    )
    # private container under the company-wide root: the routing incident
    write_json(
        root / "shared" / "daily_raw" / day / "slack" / "dm" / "D0TESTDM01.json",
        {
            "channel_id": "D0TESTDM01",
            "channel_name": "dm",
            "is_private": True,
            "date_range": {"start": day, "end": day},
            "collected_at": f"{day}T23:59:00",
            "message_count": 1,
            "messages": [
                slack_message("1777000300.000400", channel_id="D0TESTDM01", channel_name="dm")
            ],
            "_meta_patched_at": f"{day}T23:59:59",
        },
    )
    write_json(
        root / "personal" / "daily_raw" / day / "slack" / "private" / "team-private.json",
        {
            "channel_id": "C0TESTPRV1",
            "channel_name": "team-private",
            "is_private": True,
            "date_range": {"start": day, "end": day},
            "collected_at": f"{day}T23:59:00",
            "message_count": 1,
            "messages": [
                slack_message("1777000400.000500", channel_id="C0TESTPRV1", channel_name="team-private")
            ],
            "_meta": {
                "visibility": "personal",
                "access_list": ["tester"],
                "collected_by": "tester",
                "collected_at": f"{day}T23:59:00+09:00",
                "source_schema_version": "1.0",
            },
        },
    )
    return root


def notion_page(page_id: str, **overrides: Any) -> dict[str, Any]:
    page = {
        "id": page_id,
        "url": f"https://www.notion.so/{page_id.replace('-', '')}",
        "title": "Synthetic page",
        "created_time": "2026-05-01T01:00:00.000Z",
        "last_edited_time": "2026-05-01T02:00:00.000Z",
        "created_by": "11111111-1111-1111-1111-111111111111",
        "last_edited_by": "22222222-2222-2222-2222-222222222222",
        "properties": {"Name": "Synthetic page", "Owner": "<relation>", "Total": "<formula>"},
    }
    page.update(overrides)
    return page


def build_notion_tree(root: Path, *, day: str = "2026-05-01") -> Path:
    page_with_blocks = notion_page(
        "aaaaaaaa-0000-0000-0000-000000000001",
        _blocks_text="synthetic flattened body",
        _mentioned_user_ids=["11111111-1111-1111-1111-111111111111"],
        _blocks=[
            {
                "id": "bbbbbbbb-0000-0000-0000-000000000001",
                "type": "paragraph",
                "created_time": "2026-05-01T01:05:00.000Z",
                "last_edited_time": "2026-05-01T01:06:00.000Z",
                "created_by": {"object": "user", "id": "11111111-1111-1111-1111-111111111111"},
                "last_edited_by": {"object": "user", "id": "11111111-1111-1111-1111-111111111111"},
                "parent": {"type": "page_id", "page_id": "aaaaaaaa-0000-0000-0000-000000000001"},
                "paragraph": {"rich_text": [{"type": "text", "plain_text": "synthetic"}]},
                "_children": [
                    {
                        "id": "cccccccc-0000-0000-0000-000000000001",
                        "type": "bulleted_list_item",
                        "created_time": "2026-05-01T01:07:00.000Z",
                        "last_edited_time": "2026-05-01T01:07:00.000Z",
                        "parent": {"type": "block_id", "block_id": "bbbbbbbb-0000-0000-0000-000000000001"},
                    }
                ],
            }
        ],
    )
    write_json(
        root / "shared" / "daily_raw" / day / "notion" / "common" / "N1_Meetings.json",
        {
            "source_key": "N1",
            "source_name": "Meetings Notes",
            "source_id": "dddddddd-0000-0000-0000-000000000001",
            "source_type": "database",
            "date_range": {"start": day, "end": day},
            "collected_at": f"{day}T23:59:00",
            "page_count": 2,
            "pages": [page_with_blocks, notion_page("aaaaaaaa-0000-0000-0000-000000000002")],
            "_meta": {
                "visibility": "public",
                "access_list": None,
                "collected_by": "tester",
                "collected_at": f"{day}T23:59:00+09:00",
                "source_schema_version": "1.0",
            },
        },
    )
    write_json(
        root / "shared" / "daily_raw" / day / "notion" / "meta.json",
        {"status": "ok", "collection_time": f"{day}T23:59:00"},
    )
    # pre-2026-04 layout: no source directory and no _meta
    write_json(
        root / "shared" / "daily_raw" / "2026-02-01" / "common" / "N2_Docs.json",
        {
            "source_key": "N2",
            "source_name": "Docs",
            "source_id": "dddddddd-0000-0000-0000-000000000002",
            "source_type": "page",
            "date_range": {"start": "2026-02-01", "end": "2026-02-01"},
            "collected_at": "2026-02-01T23:59:00",
            "page_count": 1,
            "pages": [notion_page("aaaaaaaa-0000-0000-0000-000000000003")],
        },
    )
    # comments live only inside attribution files
    write_json(
        root / "shared" / "daily_raw" / day / "notion" / "attribution" / "person" / "comments.json",
        {
            "user_id": "11111111-1111-1111-1111-111111111111",
            "date_range": {"start": day, "end": day},
            "collected_at": f"{day}T23:59:00",
            "count": 1,
            "comments": [
                {
                    "id": "eeeeeeee-0000-0000-0000-000000000001",
                    "discussion_id": "ffffffff-0000-0000-0000-000000000001",
                    "parent_type": "page_id",
                    "parent_id": "aaaaaaaa-0000-0000-0000-000000000001",
                    "created_time": "2026-05-01T03:00:00.000Z",
                    "last_edited_time": "2026-05-01T03:00:00.000Z",
                    "created_by": "11111111-1111-1111-1111-111111111111",
                    "text": "synthetic comment",
                    "page_id": "aaaaaaaa-0000-0000-0000-000000000001",
                    "page_title": "Synthetic page",
                }
            ],
            "_meta": {"visibility": "public", "source_schema_version": "1.0"},
        },
    )
    # attribution page bucket: must never be converted
    write_json(
        root / "shared" / "daily_raw" / day / "notion" / "attribution" / "person" / "authored_pages.json",
        {
            "user_id": "11111111-1111-1111-1111-111111111111",
            "date_range": {"start": day, "end": day},
            "count": 1,
            "pages": [notion_page("aaaaaaaa-0000-0000-0000-000000000001", role="created")],
        },
    )
    return root
