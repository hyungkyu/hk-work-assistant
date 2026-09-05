import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rlwrld_worklog.models import Classification, MentionKind
from rlwrld_worklog.normalizers import (
    extract_slack_mentions,
    normalize_calendar,
    normalize_github,
    normalize_notion,
    normalize_slack,
    notion_mention_user_ids,
)


FIXTURES = Path(__file__).parent / "fixtures"


def notion_page(*, edited_by: str = "user-1") -> dict:
    return {
        "object": "page",
        "id": "page-1",
        "created_time": "2026-08-31T01:00:00Z",
        "last_edited_time": "2026-08-31T02:00:00Z",
        "last_edited_by": {"id": edited_by},
        "parent": {"type": "workspace", "workspace": True},
        "properties": {},
    }


def notion_paragraph(text: str, *, user_id: str) -> dict:
    """A paragraph block whose rich text names one person, as Notion shapes it."""
    return {
        "id": f"block-{user_id}-{text.strip()}",
        "type": "paragraph",
        "paragraph": {
            "rich_text": [
                {"type": "text", "text": {"content": text}, "plain_text": text},
                {
                    "type": "mention",
                    "mention": {"type": "user", "user": {"object": "user", "id": user_id}},
                    "plain_text": "@Someone",
                },
            ]
        },
    }


class NormalizerTests(unittest.TestCase):
    def test_slack_direct_mention_and_attachment_metadata(self):
        record = json.loads((FIXTURES / "slack.json").read_text())[0]
        event = normalize_slack(record, self_user_id="U0SELF")
        self.assertIn(Classification.REQUEST, event.classification)
        self.assertIn(Classification.QUESTION, event.classification)
        self.assertEqual(event.mentions[0].kind, MentionKind.DIRECT)
        self.assertEqual(event.mentions[0].direction, "to_self")
        self.assertNotIn("content", event.payload["files"][0])
        self.assertEqual(event.payload["files"][0]["permalink"], "https://example.slack.com/files/F1")

    def test_slack_self_mention_direction_and_response(self):
        record = json.loads((FIXTURES / "slack.json").read_text())[1]
        event = normalize_slack(record, self_user_id="U0SELF")
        self.assertEqual(event.mentions[0].direction, "from_self")
        self.assertIn(Classification.PROMISE, event.classification)
        self.assertIn(Classification.RESPONSE, event.classification)

    def test_private_calendar_event_redacts_content(self):
        record = json.loads((FIXTURES / "google_calendar.json").read_text())[1]
        event = normalize_calendar(record)
        self.assertEqual(event.payload["summary"], "Busy")
        self.assertNotIn("description", event.payload)
        self.assertNotIn("attendees", event.payload)

    def test_github_bot_is_separated(self):
        record = json.loads((FIXTURES / "github.json").read_text())[1]
        event = normalize_github(record)
        self.assertTrue(event.payload["automated"])
        self.assertEqual(event.payload["actor_type"], "Bot")

    def test_group_and_broadcast_mentions_are_indexed(self):
        mentions = extract_slack_mentions(
            "<!subteam^S123ABC|robot-team> <!channel> <!here> <!everyone>",
            actor_id="U0OTHER",
            self_user_id="U0SELF",
        )
        self.assertEqual(
            [mention.kind for mention in mentions],
            [MentionKind.USER_GROUP, MentionKind.CHANNEL, MentionKind.HERE, MentionKind.EVERYONE],
        )
        self.assertEqual([mention.priority for mention in mentions], [60, 20, 20, 20])

    def test_notion_user_mentions_are_read_out_of_blocks_and_comments(self):
        blocks = [notion_paragraph("cc ", user_id="user-2")]
        comments = [
            {
                "id": "comment-1",
                "rich_text": [
                    {
                        "type": "mention",
                        "mention": {"type": "user", "user": {"object": "user", "id": "user-3"}},
                        "plain_text": "@Dana",
                    }
                ],
            }
        ]
        event = normalize_notion(
            notion_page(), blocks=blocks, comments=comments, self_user_id="user-1"
        )
        self.assertEqual(
            [(mention.target_id, mention.kind) for mention in event.mentions],
            [("user-2", MentionKind.DIRECT), ("user-3", MentionKind.DIRECT)],
        )

    def test_notion_mention_direction_is_relative_to_the_stated_self(self):
        to_self = normalize_notion(
            notion_page(),
            blocks=[notion_paragraph("hi ", user_id="user-1")],
            self_user_id="user-1",
        )
        self.assertEqual(to_self.mentions[0].direction, "to_self")

        # The page's last editor is us, so whoever it names, we named.
        from_self = normalize_notion(
            notion_page(edited_by="user-9"),
            blocks=[notion_paragraph("hi ", user_id="user-2")],
            self_user_id="user-9",
        )
        self.assertEqual(from_self.mentions[0].direction, "from_self")

        # No self was stated, so no mention is claimed to point at one.
        anonymous = normalize_notion(
            notion_page(), blocks=[notion_paragraph("hi ", user_id="user-1")]
        )
        self.assertEqual(anonymous.mentions[0].direction, "other")

    def test_a_notion_mention_repeated_across_blocks_is_still_one_person(self):
        blocks = [notion_paragraph(f"line{index} ", user_id="user-2") for index in range(5)]
        event = normalize_notion(notion_page(), blocks=blocks)
        self.assertEqual([mention.target_id for mention in event.mentions], ["user-2"])

    def test_a_notion_page_or_date_mention_is_not_recorded_as_a_person(self):
        """A document link has no direction, so it is not squeezed into one."""
        blocks = [
            {
                "id": "block-1",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [
                        {
                            "type": "mention",
                            "mention": {"type": "page", "page": {"id": "page-2"}},
                            "plain_text": "Spec",
                        },
                        {
                            "type": "mention",
                            "mention": {"type": "date", "date": {"start": "2026-09-05"}},
                            "plain_text": "2026-09-05",
                        },
                    ]
                },
            }
        ]
        self.assertEqual(notion_mention_user_ids(blocks), [])

    def test_a_notion_mention_in_a_block_type_nobody_special_cased_is_still_found(self):
        """The walk matches the entry's shape, not a path it was taught."""
        exotic = {
            "id": "block-1",
            "type": "some_future_block",
            "some_future_block": {
                "caption": [
                    {
                        "type": "mention",
                        "mention": {"type": "user", "user": {"id": "user-7"}},
                        "plain_text": "@Kim",
                    }
                ]
            },
        }
        self.assertEqual(notion_mention_user_ids([exotic]), ["user-7"])

    def test_event_id_is_stable(self):
        record = json.loads((FIXTURES / "slack.json").read_text())[0]
        first = normalize_slack(record, self_user_id="U0SELF")
        second = normalize_slack(record, self_user_id="U0SELF")
        self.assertEqual(first.event_id, second.event_id)

    def test_live_slack_identity_matches_ledger_workspace_channel_ts(self):
        record = json.loads((FIXTURES / "slack.json").read_text())[0]
        record["team_id"] = "T0TESTWS01"
        event = normalize_slack(record, self_user_id="U0SELF")
        self.assertEqual(
            event.external_id,
            f"T0TESTWS01:{record['channel']}:{record['ts']}",
        )

    def test_event_json_round_trip(self):
        record = json.loads((FIXTURES / "slack.json").read_text())[0]
        original = normalize_slack(record, self_user_id="U0SELF")
        restored = type(original).from_dict(original.to_dict())
        self.assertEqual(restored.to_dict()["event_id"], original.event_id)
        self.assertEqual(restored.mentions, original.mentions)
        self.assertEqual(restored.classification, original.classification)


if __name__ == "__main__":
    unittest.main()
