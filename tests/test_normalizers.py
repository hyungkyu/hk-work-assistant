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
    normalize_slack,
)


FIXTURES = Path(__file__).parent / "fixtures"


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
