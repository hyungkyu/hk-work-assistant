from __future__ import annotations

import json
from pathlib import Path

from rlwrld_worklog.legacy_import import LegacyImportStats, iter_legacy_events
from rlwrld_worklog.models import Source


def _write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def test_legacy_sources_normalize_to_stable_events(tmp_path: Path) -> None:
    _write(
        tmp_path / "2026-08-13/slack/common/channel.json",
        {"channel_id": "C1", "messages": [{"ts": "1786579200.1", "text": "hello", "user": "U1"}]},
    )
    _write(
        tmp_path / "2026-08-13/gcal/common/all_events.json",
        {
            "events": [
                {
                    "id": "E1",
                    "start": "2026-08-13",
                    "end": "2026-08-14",
                    "status": "confirmed",
                    "organizer": {"email": "owner@example.com"},
                }
            ]
        },
    )
    _write(
        tmp_path / "2026-08-13/notion/common/meeting.json",
        {
            "pages": [
                {
                    "id": "N1",
                    "url": "https://www.notion.so/N1",
                    "created_time": "2026-08-12T00:00:00Z",
                    "last_edited_time": "2026-08-13T00:00:00Z",
                    "properties": {},
                    "_blocks_text": "weekly notes",
                }
            ]
        },
    )
    for source, expected in [
        ("slack", Source.SLACK),
        ("google-calendar", Source.GOOGLE_CALENDAR),
        ("notion", Source.NOTION),
    ]:
        stats = LegacyImportStats()
        events = list(iter_legacy_events(tmp_path, source, stats))
        assert len(events) == 1
        assert events[0].source is expected
        assert stats.records_normalized == 1
