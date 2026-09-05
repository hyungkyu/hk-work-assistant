"""Live capture run -> standard v1 ledger.

These are integration tests over the real collectors driven by the scripted
fake API clients from the collector test modules, so they exercise the whole
path: fake API -> immutable raw archive -> run manifest -> ledger JSONL. No
network call is made anywhere in this file.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from test_calendar_collector import (  # noqa: E402
    FakeCalendarClient,
    PRIMARY,
    TEAM as CALENDAR_ID,
    event as calendar_event,
)
from test_calendar_collector import collect as collect_calendar  # noqa: E402
from test_notion_collector import (  # noqa: E402
    DATA_SOURCE_ID,
    FakeNotionClient,
    PAGE_ID,
    page_body,
    paragraph,
)
from test_notion_collector import collect as collect_notion  # noqa: E402
from test_slack_collector import (  # noqa: E402
    CHANNEL,
    DM,
    FakeSlack,
    GROUP,
    OTHER,
    SELF,
    TEAM,
    message,
    ts,
)
from test_slack_collector import collect as collect_slack  # noqa: E402

from rlwrld_worklog.ledger.live import LIVE_CONVERTER_VERSION, convert_live_run  # noqa: E402
from rlwrld_worklog.ledger.schema import validate_record  # noqa: E402


def convert(tmp_path: Path, manifest_path: Path, source: str, **kwargs):
    return convert_live_run(
        archive_root=tmp_path,
        manifest_path=manifest_path,
        out_root=tmp_path / "staging",
        source=source,
        **kwargs,
    )


def rows(result) -> list[dict]:
    return [json.loads(line) for line in Path(result.output_path).read_text().splitlines() if line]


def by_type(records: list[dict], entity_type: str) -> list[dict]:
    return [record for record in records if record["entity_type"] == entity_type]


# ------------------------------------------------------------------ slack


@pytest.fixture
def slack_run(tmp_path: Path):
    parent = ts(-500)
    reply = ts(-100)
    client = FakeSlack(
        history={
            CHANNEL: [
                [
                    message(
                        parent,
                        reply_count=1,
                        latest_reply=reply,
                        reactions=[{"name": "eyes", "users": [SELF], "count": 1}],
                        edited={"user": OTHER, "ts": ts(-400)},
                        text=f"<@{SELF}> please review",
                    )
                ]
            ],
            DM: [[message(ts(-80), text="direct message")]],
        },
        replies={(CHANNEL, parent): [message(reply, thread_ts=parent, text="synthetic reply")]},
        searches={
            f"<@{SELF}>": [
                {
                    "ts": ts(-70),
                    "user": OTHER,
                    "text": f"<@{SELF}> mention in a channel history never reached",
                    "channel": {"id": "C0OTHERCH", "name": "other"},
                    "edited": {"ts": ts(-65)},
                }
            ]
        },
    )
    _, result = collect_slack(tmp_path, client)
    return result


def test_slack_run_converts_to_valid_ledger_records(tmp_path: Path, slack_run) -> None:
    converted = convert(tmp_path, slack_run.manifest_path, "slack")

    assert converted.schema_errors == 0
    records = rows(converted)
    assert all(validate_record(record) == [] for record in records)
    assert converted.by_entity_type["message"] == 4
    assert converted.by_entity_type["conversation"] == 2
    assert converted.by_entity_type["user"] == 2
    assert converted.by_entity_type["usergroup"] == 1
    assert {record["source"] for record in records} == {"slack"}
    assert {record["provenance"]["converter_version"] for record in records} == {LIVE_CONVERTER_VERSION}


def test_slack_message_keeps_mentions_threads_reactions_and_edits(tmp_path: Path, slack_run) -> None:
    records = rows(convert(tmp_path, slack_run.manifest_path, "slack"))
    parent = next(
        record
        for record in by_type(records, "message")
        if record["raw_payload"].get("reply_count") == 1
    )
    reply = next(
        record for record in by_type(records, "message") if record["relations"]["is_thread_reply"]
    )

    assert f"<@{SELF}>" in parent["raw_payload"]["text"], "the mention text is preserved verbatim"
    assert parent["relations"]["reactions"][0]["name"] == "eyes"
    assert parent["source_updated_at"] is not None, "an edit timestamp becomes source_updated_at"
    assert parent["source_updated_at_status"] == "observed"
    assert parent["relations"]["reply_count"] == 1
    assert reply["relations"]["parent_ts"] == parent["source_entity_key"]["ts"]
    assert reply["scope"]["channel_id"] == CHANNEL


def test_a_search_only_mention_is_a_marked_supplement(tmp_path: Path, slack_run) -> None:
    records = rows(convert(tmp_path, slack_run.manifest_path, "slack"))
    supplement = next(
        record for record in by_type(records, "message") if record["scope"]["channel_id"] == "C0OTHERCH"
    )

    assert supplement["capture_profile"] == "live-slack-search/v1"
    assert supplement["supplement_provenance"]["is_supplement"] is True
    assert supplement["supplement_provenance"]["supplement_kind"] == "search.messages"
    assert supplement["provenance"]["api_endpoint"] == "search.messages"


def test_slack_dimension_records_describe_channels_users_and_groups(tmp_path: Path, slack_run) -> None:
    records = rows(convert(tmp_path, slack_run.manifest_path, "slack"))
    dm = next(
        record for record in by_type(records, "conversation")
        if record["source_entity_key"]["id"] == DM
    )
    group = by_type(records, "usergroup")[0]

    assert dm["scope"]["container"] == "im"
    assert dm["scope"]["is_private"] is True
    assert group["source_entity_key"]["id"] == GROUP
    assert group["relations"]["member_user_ids"] == [SELF, OTHER]
    assert {record["tenant"]["workspace_id"] for record in records} == {TEAM}


def test_a_dm_message_is_marked_restricted(tmp_path: Path, slack_run) -> None:
    records = rows(convert(tmp_path, slack_run.manifest_path, "slack"))
    dm_message = next(
        record for record in by_type(records, "message") if record["scope"]["channel_id"] == DM
    )
    assert dm_message["scope"]["container"] == "im"
    assert dm_message["visibility_routing"]["visibility"] == "restricted"


def test_converting_the_same_run_twice_is_byte_identical(tmp_path: Path, slack_run) -> None:
    first = Path(convert(tmp_path, slack_run.manifest_path, "slack").output_path).read_bytes()
    second = Path(convert(tmp_path, slack_run.manifest_path, "slack").output_path).read_bytes()
    assert first == second


def test_a_failed_capture_run_is_refused_by_the_converter(tmp_path: Path) -> None:
    from rlwrld_worklog.archive import RawArchive

    archive = RawArchive(tmp_path, "slack", "run-failed", "test")
    manifest = archive.finish({"status": "failed", "error": "synthetic"})
    with pytest.raises(ValueError, match="successful"):
        convert(tmp_path, manifest, "slack")


# ----------------------------------------------------------------- notion


def test_notion_run_converts_pages_blocks_comments_users_and_sources(tmp_path: Path) -> None:
    client = FakeNotionClient(
        search_results=[
            {"object": "page", "id": PAGE_ID, "last_edited_time": "2026-08-26T01:00:00Z"},
            {"object": "data_source", "id": DATA_SOURCE_ID, "last_edited_time": "2026-08-26T02:00:00Z"},
        ],
        data_sources={
            DATA_SOURCE_ID: {
                "object": "data_source",
                "id": DATA_SOURCE_ID,
                "last_edited_time": "2026-08-26T02:00:00Z",
            }
        },
        comments={PAGE_ID: [{"id": "comment-1", "discussion_id": "d1", "created_time": "2026-08-26T01:30:00Z"}]},
    )
    _, _, run = collect_notion(tmp_path, client)
    converted = convert(tmp_path, run.manifest_path, "notion")

    assert converted.schema_errors == 0
    records = rows(converted)
    assert converted.by_entity_type["page"] == 1
    assert converted.by_entity_type["block"] == 3
    assert converted.by_entity_type["comment"] == 1
    assert converted.by_entity_type["user"] == 1
    assert converted.by_entity_type["data_source"] == 1
    comment = by_type(records, "comment")[0]
    assert comment["relations"]["discussion_id"] == "d1"
    assert converted.unhandled_kinds.get("search") == 1, (
        "a discovery listing is deliberately not turned into a record"
    )


def test_an_archived_notion_page_carries_its_deleted_state(tmp_path: Path) -> None:
    client = FakeNotionClient(pages={PAGE_ID: page_body(PAGE_ID, archived=True, in_trash=True)})
    _, _, run = collect_notion(tmp_path, client)
    page = by_type(rows(convert(tmp_path, run.manifest_path, "notion")), "page")[0]

    assert page["deleted_state"] == {
        "is_deleted": True,
        "kind": "archived_or_in_trash",
        "status": "observed",
    }


def test_a_notion_record_carries_who_it_named_and_says_that_it_looked(
    tmp_path: Path,
) -> None:
    """Who edited what already rode on the page object. Who mentioned whom did not.

    Slack's records say `mentions_extracted: False` because nothing re-derives
    them from message text; the Notion path does the work, so its records say
    so -- including the objects that named nobody, where an empty list means no
    mention rather than not-looked.
    """
    client = FakeNotionClient(
        blocks={
            PAGE_ID: [[paragraph(mention={"type": "user", "user": {"id": "user-2"}})]],
        },
        comments={
            PAGE_ID: [
                {
                    "id": "comment-1",
                    "discussion_id": "d1",
                    "created_time": "2026-08-26T01:30:00Z",
                    "rich_text": [
                        {
                            "type": "mention",
                            "mention": {"type": "user", "user": {"id": "user-3"}},
                            "plain_text": "@Dana",
                        }
                    ],
                }
            ]
        },
    )
    _, _, run = collect_notion(tmp_path, client)
    records = rows(convert(tmp_path, run.manifest_path, "notion"))

    block = by_type(records, "block")[0]
    comment = by_type(records, "comment")[0]
    page = by_type(records, "page")[0]
    assert block["relations"]["mentioned_user_ids"] == ["user-2"]
    assert comment["relations"]["mentioned_user_ids"] == ["user-3"]
    assert page["relations"]["mentioned_user_ids"] == []
    assert page["relations"]["last_edited_by_user_id"] == "user-1", (
        "who edited it was always free; it rides on the page object"
    )
    for record in (block, comment, page):
        assert record["relations"]["mentions_extracted"] is True


# -------------------------------------------------------- google calendar


def test_calendar_run_converts_events_and_calendar_metadata(tmp_path: Path) -> None:
    client = FakeCalendarClient(
        calendars=[
            {"id": PRIMARY, "primary": True, "accessRole": "owner", "summary": "owner", "timeZone": "Asia/Seoul"},
            {"id": CALENDAR_ID, "accessRole": "reader", "summary": "team"},
        ],
        events={
            PRIMARY: [
                calendar_event(
                    "master-1",
                    recurrence=["RRULE:FREQ=WEEKLY"],
                    attendees=[{"email": "a@example.invalid", "responseStatus": "accepted"}],
                    organizer={"email": "a@example.invalid"},
                    conferenceData={"conferenceId": "abc"},
                    attachments=[{"fileId": "F1", "fileUrl": "https://drive.invalid/F1"}],
                ),
                calendar_event("gone-1", status="cancelled"),
            ],
            CALENDAR_ID: [],
        },
    )
    _, run = collect_calendar(tmp_path, client)
    converted = convert(tmp_path, run.manifest_path, "google_calendar")

    assert converted.schema_errors == 0
    records = rows(converted)
    assert all(validate_record(record) == [] for record in records)
    assert converted.by_entity_type["event"] == 2
    assert converted.by_entity_type["calendar"] == 2

    master = next(
        record for record in by_type(records, "event") if record["source_entity_id"].endswith("master-1")
    )
    cancelled = next(
        record for record in by_type(records, "event") if record["source_entity_id"].endswith("gone-1")
    )
    assert master["scope"]["calendar_id"] == PRIMARY
    assert master["source_entity_id"] == f"{PRIMARY}:master-1"
    assert master["relations"]["is_recurrence_master"] is True
    assert master["relations"]["attendee_responses"][0]["responseStatus"] == "accepted"
    assert master["relations"]["conference_data"]["conferenceId"] == "abc"
    assert master["relations"]["attachments"][0]["fileUrl"] == "https://drive.invalid/F1"
    assert master["source_revision_id"] == '"master-1-1"', "the etag is the revision id"
    assert cancelled["deleted_state"] == {
        "is_deleted": True,
        "kind": "event_cancelled",
        "status": "observed",
    }
    assert master["tenant"] == {"workspace_id": PRIMARY, "status": "observed"}


def test_the_same_event_id_in_two_calendars_stays_two_records(tmp_path: Path) -> None:
    shared = calendar_event("shared-1")
    client = FakeCalendarClient(
        calendars=[{"id": PRIMARY, "primary": True}, {"id": CALENDAR_ID, "accessRole": "reader"}],
        events={PRIMARY: [shared], CALENDAR_ID: [shared]},
    )
    _, run = collect_calendar(tmp_path, client)
    events = by_type(rows(convert(tmp_path, run.manifest_path, "google_calendar")), "event")

    assert sorted(record["source_entity_id"] for record in events) == [
        f"{PRIMARY}:shared-1",
        f"{CALENDAR_ID}:shared-1",
    ]


def test_a_deleted_calendar_is_preserved_as_a_deleted_dimension(tmp_path: Path) -> None:
    client = FakeCalendarClient(
        calendars=[{"id": PRIMARY, "primary": True}, {"id": CALENDAR_ID, "deleted": True}],
        events={PRIMARY: [], CALENDAR_ID: []},
    )
    _, run = collect_calendar(tmp_path, client)
    calendars = by_type(rows(convert(tmp_path, run.manifest_path, "google_calendar")), "calendar")
    removed = next(record for record in calendars if record["source_entity_id"] == CALENDAR_ID)

    assert removed["deleted_state"]["is_deleted"] is True
    assert removed["deleted_state"]["kind"] == "calendar_removed"


def test_calendar_conversion_accepts_the_archive_source_spelling(tmp_path: Path) -> None:
    _, run = collect_calendar(tmp_path, FakeCalendarClient())
    assert convert(tmp_path, run.manifest_path, "google-calendar").schema_errors == 0


# ------------------------------------------------------- shared behaviour


def test_a_partial_run_is_visible_in_every_record(tmp_path: Path) -> None:
    from rlwrld_worklog.calendar_client import CalendarApiError

    client = FakeCalendarClient(
        calendars=[{"id": PRIMARY, "primary": True}, {"id": CALENDAR_ID}],
        events={PRIMARY: [calendar_event("event-1")]},
        failing={CALENDAR_ID: CalendarApiError("forbidden", code=403)},
    )
    _, run = collect_calendar(tmp_path, client)
    records = rows(convert(tmp_path, run.manifest_path, "google_calendar"))
    event_record = by_type(records, "event")[0]

    gap = event_record["coverage"]["permission_gap"]
    assert gap["run_skips"] == 1
    assert gap["by_kind"]["calendar_inaccessible"] == 1


def test_dry_run_conversion_writes_nothing(tmp_path: Path, slack_run) -> None:
    converted = convert(tmp_path, slack_run.manifest_path, "slack", dry_run=True)
    assert converted.records_written > 0
    assert converted.output_path is None
    assert not (tmp_path / "staging" / "ledger").exists()


def test_the_observation_window_is_the_run_day(tmp_path: Path, slack_run) -> None:
    manifest = json.loads(slack_run.manifest_path.read_text())
    day = datetime.fromisoformat(manifest["finished_at"]).astimezone(timezone.utc).date().isoformat()
    for record in rows(convert(tmp_path, slack_run.manifest_path, "slack")):
        assert record["observation_window"] == {
            "start": day,
            "end": day,
            "tz": "UTC",
            "granularity": "day",
        }
        assert record["coverage"]["observation_role"] == "current_head"


def test_two_runs_on_different_days_stay_separate_observations(tmp_path: Path) -> None:
    """The observation window is part of ledger identity, so a re-observation
    of an unchanged object on another day is a second row, not a collision."""
    from rlwrld_worklog.ledger.schema import ledger_id_for

    first = ledger_id_for(
        source="slack",
        entity_type="message",
        tenant_id=TEAM,
        scope_key=CHANNEL,
        source_entity_id="x",
        window_start="2026-08-30",
        content_hash="sha256:" + "0" * 64,
    )
    second = ledger_id_for(
        source="slack",
        entity_type="message",
        tenant_id=TEAM,
        scope_key=CHANNEL,
        source_entity_id="x",
        window_start="2026-08-31",
        content_hash="sha256:" + "0" * 64,
    )
    assert first != second


def test_slack_message_window_matches_the_capture_not_the_message_age(tmp_path: Path) -> None:
    old = f"{(datetime.now(timezone.utc) - timedelta(days=400)).timestamp():.6f}"
    client = FakeSlack(history={CHANNEL: [[message(old)]], DM: [[]]})
    _, run = collect_slack(tmp_path, client, since=datetime.now(timezone.utc) - timedelta(days=500))
    record = by_type(rows(convert(tmp_path, run.manifest_path, "slack")), "message")[0]

    assert record["observation_window"]["start"] == datetime.now(timezone.utc).date().isoformat()
    assert record["source_created_at"].startswith(
        (datetime.now(timezone.utc) - timedelta(days=400)).date().isoformat()
    )


# ----------------------------------------------- raw byte integrity


def raw_pages(manifest_path: Path) -> list[dict]:
    return json.loads(manifest_path.read_text())["files"]


def test_a_tampered_raw_page_is_refused_and_the_ledger_is_not_replaced(
    tmp_path: Path, slack_run
) -> None:
    """The manifest is an index; only the bytes on disk are the authority."""
    import gzip

    good = convert(tmp_path, slack_run.manifest_path, "slack")
    before = Path(good.output_path).read_bytes()

    target = tmp_path / raw_pages(slack_run.manifest_path)[0]["path"]
    target.write_bytes(gzip.compress(b'{"ok": true, "messages": [], "tampered": true}', mtime=0))

    with pytest.raises(ValueError, match="does not match the manifest hash"):
        convert(tmp_path, slack_run.manifest_path, "slack")

    assert Path(good.output_path).read_bytes() == before, (
        "a corrupted raw page must never replace a ledger file built from good bytes"
    )


def test_a_truncated_raw_page_is_refused(tmp_path: Path, slack_run) -> None:
    target = tmp_path / raw_pages(slack_run.manifest_path)[0]["path"]
    target.write_bytes(target.read_bytes()[:-5])

    with pytest.raises(ValueError, match="does not match the manifest hash"):
        convert(tmp_path, slack_run.manifest_path, "slack")


def test_a_manifest_hash_that_does_not_match_the_bytes_is_refused(
    tmp_path: Path, slack_run
) -> None:
    manifest = json.loads(slack_run.manifest_path.read_text())
    manifest["files"][0]["sha256"] = "0" * 64
    edited = slack_run.manifest_path.with_name("edited-manifest.json")
    edited.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="does not match the manifest hash"):
        convert(tmp_path, edited, "slack")


@pytest.mark.parametrize("bad_hash", [None, "", "not-a-hash", "ABCDEF" * 10, 12345])
def test_a_manifest_without_a_usable_hash_is_refused(
    tmp_path: Path, slack_run, bad_hash
) -> None:
    manifest = json.loads(slack_run.manifest_path.read_text())
    if bad_hash is None:
        manifest["files"][0].pop("sha256")
    else:
        manifest["files"][0]["sha256"] = bad_hash
    edited = slack_run.manifest_path.with_name(f"edited-{type(bad_hash).__name__}-manifest.json")
    edited.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="no usable sha256"):
        convert(tmp_path, edited, "slack")


def test_a_missing_raw_page_is_refused(tmp_path: Path, slack_run) -> None:
    (tmp_path / raw_pages(slack_run.manifest_path)[0]["path"]).unlink()

    with pytest.raises(FileNotFoundError):
        convert(tmp_path, slack_run.manifest_path, "slack")


def test_integrity_is_verified_for_every_source(tmp_path: Path) -> None:
    import gzip

    for source_name, subdirectory, run in (
        ("notion", "n", collect_notion(tmp_path / "n", FakeNotionClient())[2]),
        ("google_calendar", "c", collect_calendar(tmp_path / "c", FakeCalendarClient())[1]),
    ):
        root = tmp_path / subdirectory
        # Tamper with every page, so whichever one the converter reads first
        # for this source is the one that has to be caught.
        for item in raw_pages(run.manifest_path):
            (root / item["path"]).write_bytes(gzip.compress(b'{"tampered": true}', mtime=0))
        with pytest.raises(ValueError, match="does not match the manifest hash"):
            convert_live_run(
                archive_root=root,
                manifest_path=run.manifest_path,
                out_root=root / "staging",
                source=source_name,
            )
        assert not (root / "staging").exists(), "no ledger file is written for a tampered run"


def test_an_untampered_run_verifies_cleanly(tmp_path: Path, slack_run) -> None:
    converted = convert(tmp_path, slack_run.manifest_path, "slack")
    assert converted.records_written > 0
    assert converted.schema_errors == 0
