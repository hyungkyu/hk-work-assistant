"""The layer-by-layer check that replaces this week's hand-written probes."""

from __future__ import annotations

import pytest
from datetime import date

from rlwrld_worklog.reconcile import (
    Row,
    calendar_day_count,
    slack_day_count,
)


def test_a_verdict_names_the_layer_that_dropped_it():
    # Collected but never projected: the calendar bug, 2026-09-16.
    assert Row(day="d", source="google_calendar", ledger=124, timeline=0, digest=0).verdict == (
        "투영 누락"
    )
    # Projected but never written: the dry-run digest.
    assert Row(day="d", source="slack", ledger=30, timeline=30, digest=0).verdict == (
        "다이제스트 누락"
    )
    # The source holds more than was collected.
    assert Row(
        day="d", source="slack", ledger=21, timeline=21, digest=21, external=30
    ).verdict == "수집 누락"
    assert Row(day="d", source="slack", ledger=30, timeline=30, digest=30).verdict == "ok"


def test_a_meeting_recollected_daily_is_not_reported_as_a_defect():
    """One meeting, five captures: more ledger rows than the calendar holds."""
    row = Row(
        day="d", source="google_calendar", ledger=5, timeline=5, digest=1, external=1
    )
    assert row.verdict == "원장이 더 많음"


def test_an_empty_day_is_not_a_gap():
    assert Row(day="d", source="notion", ledger=0, timeline=0, digest=0).verdict == "ok"
    assert (
        Row(day="d", source="slack", ledger=0, timeline=0, digest=0, external=0).verdict
        == "ok"
    )


def _match(channel: str, ts: str):
    return {"channel": {"id": channel}, "ts": ts}


def test_slack_is_asked_both_questions_the_digest_answers():
    """Written by him and naming him -- the column counts what the day holds."""
    calls = []

    class Fake:
        def call(self, method, **params):
            calls.append((method, params))
            if params["query"].startswith("from:"):
                return {"messages": {"matches": [_match("C1", "1"), _match("C1", "2")]}}
            return {"messages": {"matches": [_match("C2", "9")]}}

    assert slack_day_count(Fake(), "U07EKRU6F7H", date(2026, 9, 15)) == 3
    assert [params["query"] for _, params in calls] == [
        "from:<@U07EKRU6F7H> on:2026-09-15",
        "<@U07EKRU6F7H> on:2026-09-15",
    ]


def test_a_message_he_wrote_that_names_him_is_counted_once():
    """Totals would say two. It is one message, so the arms are unioned."""

    class Fake:
        def call(self, method, **params):
            return {"messages": {"matches": [_match("C1", "1")]}}

    assert slack_day_count(Fake(), "U1", date(2026, 9, 15)) == 1


def test_slack_reports_not_measured_rather_than_zero_when_it_answers_oddly():
    class Fake:
        def call(self, method, **params):
            return {"ok": False}

    assert slack_day_count(Fake(), "U1", date(2026, 9, 15)) is None


def test_a_day_too_long_to_page_through_is_not_measured_rather_than_undercounted():
    class Fake:
        def call(self, method, **params):
            return {
                "messages": {
                    "matches": [_match("C1", str(params["page"]))],
                    "paging": {"pages": 999},
                }
            }

    assert slack_day_count(Fake(), "U1", date(2026, 9, 15)) is None


def test_every_page_of_a_long_day_is_read():
    seen = []

    class Fake:
        def call(self, method, **params):
            seen.append(params["page"])
            return {
                "messages": {
                    "matches": [_match("C1", f"{params['query'][:4]}-{params['page']}")],
                    "paging": {"pages": 3},
                }
            }

    assert slack_day_count(Fake(), "U1", date(2026, 9, 15)) == 6
    assert seen == [1, 2, 3, 1, 2, 3]


def test_a_meeting_on_two_calendars_is_counted_once():
    class Fake:
        def iter_day_events(self, calendar_id, *, time_min, time_max):
            return [{"id": "evt-1"}, {"id": f"only-{calendar_id}"}]

    assert calendar_day_count(Fake(), ["a@x", "b@x"], date(2026, 9, 15)) == 3


def test_the_calendar_window_is_the_kst_day():
    seen = {}

    class Fake:
        def iter_day_events(self, calendar_id, *, time_min, time_max):
            seen.update({"timeMin": time_min, "timeMax": time_max})
            return []

    calendar_day_count(Fake(), ["a@x"], date(2026, 9, 15))
    assert seen["timeMin"].startswith("2026-09-15T00:00:00+09:00")
    assert seen["timeMax"].startswith("2026-09-16T00:00:00+09:00")


def test_the_command_runs_end_to_end_without_a_database(monkeypatch, capsys):
    """The check that was missing: nothing called reconcile_command at all.

    Shipped with `_database_url(args)`, a function that does not exist, and
    every unit test still passed because none of them invoked the command. So
    this one does, with the layers faked, and would have caught the NameError.
    """
    from datetime import date as date_type

    from rlwrld_worklog import cli, digest, reconcile as reconcile_module

    monkeypatch.setattr(
        digest, "resolve_people", lambda url, names: {"resolved": {names[0]: "p_1"}, "unresolved": [], "ambiguous": {}}
    )
    monkeypatch.setattr(
        reconcile_module,
        "reconcile",
        lambda url, person_id, days, **kwargs: reconcile_module.ReconcileResult(
            rows=[
                reconcile_module.Row(
                    day=days[0].isoformat(),
                    source="google_calendar",
                    ledger=124,
                    timeline=0,
                    digest=0,
                )
            ]
        ),
    )

    class Args:
        person_name = "류형규"
        since = "2026-09-15"
        until = "2026-09-15"
        database_url = "postgresql://fake"
        source = []
        gaps_only = False
        no_source_read = True

    assert cli.reconcile_command(Args()) == 1  # a gap exits non-zero
    printed = capsys.readouterr().out
    assert "투영 누락" in printed
    assert "google_calendar" in printed
    assert date_type(2026, 9, 15).isoformat() in printed


def test_the_command_exits_clean_when_no_layer_lost_anything(monkeypatch, capsys):
    from rlwrld_worklog import cli, digest, reconcile as reconcile_module

    monkeypatch.setattr(
        digest, "resolve_people", lambda url, names: {"resolved": {"x": "p_1"}, "unresolved": [], "ambiguous": {}}
    )
    monkeypatch.setattr(
        reconcile_module,
        "reconcile",
        lambda url, person_id, days, **kwargs: reconcile_module.ReconcileResult(
            rows=[
                reconcile_module.Row(
                    day="2026-09-15", source="slack", ledger=30, timeline=30, digest=30,
                    external=30,
                )
            ]
        ),
    )

    class Args:
        person_name = "x"
        since = "2026-09-15"
        until = "2026-09-15"
        database_url = "postgresql://fake"
        source = []
        gaps_only = False
        no_source_read = True

    assert cli.reconcile_command(Args()) == 0
    assert "ok" in capsys.readouterr().out


def test_an_unknown_person_is_refused_rather_than_reported_as_empty(monkeypatch):
    import pytest

    from rlwrld_worklog import cli, digest

    monkeypatch.setattr(
        digest, "resolve_people", lambda url, names: {"resolved": {}, "unresolved": names, "ambiguous": {}}
    )

    class Args:
        person_name = "없는사람"
        since = "2026-09-15"
        until = "2026-09-15"
        database_url = "postgresql://fake"
        source = []
        gaps_only = False
        no_source_read = True

    with pytest.raises(SystemExit):
        cli.reconcile_command(Args())


def test_the_calendar_comparison_asks_both_sides_the_same_question():
    """72 against 18 was not 수집 누락; it was two different questions.

    The source side used to count every event visible on every calendar the
    account can see. The ledger side counts events the person organises or
    attends. Now both mean the same thing.
    """
    from rlwrld_worklog.reconcile import calendar_day_count, event_involves

    me = {"hk@rlwrld.ai"}
    assert event_involves({"organizer": {"email": "HK@rlwrld.ai"}}, me)
    assert event_involves({"creator": {"email": "hk@rlwrld.ai"}}, me)
    assert event_involves(
        {"attendees": [{"email": "hk@rlwrld.ai", "responseStatus": "accepted"}]}, me
    )
    # A declined invitation is not attendance on either side.
    assert not event_involves(
        {"attendees": [{"email": "hk@rlwrld.ai", "responseStatus": "declined"}]}, me
    )
    # Somebody else's meeting, visible but not his.
    assert not event_involves({"organizer": {"email": "other@rlwrld.ai"}}, me)
    assert not event_involves({}, me)

    class Fake:
        def iter_day_events(self, calendar_id, *, time_min, time_max):
            return [
                {"id": "mine", "organizer": {"email": "hk@rlwrld.ai"}},
                {"id": "holiday", "organizer": {"email": "holidays@google.com"}},
                {"id": "theirs", "organizer": {"email": "other@rlwrld.ai"}},
            ]

    # Two calendars, the same three events: one is his, and it counts once.
    assert calendar_day_count(Fake(), ["a@x", "b@x"], date(2026, 9, 15), me) == 1
    # Without an address list the old behaviour is kept, deduplicated by id.
    assert calendar_day_count(Fake(), ["a@x", "b@x"], date(2026, 9, 15), None) == 3


def test_one_meeting_on_a_dozen_calendars_is_one_meeting():
    """The ledger keys each copy by calendar, so a meeting arrived as many.

    `source_entity_id` is `calendar_id:event_id`, so the same meeting sitting
    on the organiser's calendar and on every attendee's became a dozen distinct
    rows: 54 against the 16 the calendar actually held. iCalUID is the value
    every copy shares.
    """
    from rlwrld_worklog.reconcile import _LEDGER_SQL

    assert "iCalUID" in _LEDGER_SQL
    assert "count(DISTINCT" in _LEDGER_SQL


def test_the_table_says_which_build_produced_it():
    """Three runs in one day were read as failures of an unapplied fix."""
    from rlwrld_worklog.reconcile import running_code

    found = running_code()
    assert isinstance(found, str) and found


def test_explain_does_not_demand_a_range_it_does_not_use(monkeypatch, capsys):
    """The flag's first real use died in the argument parser.

    `--explain DATE` names its day; requiring --since and --until beside it
    made a tool built to stop a guess cost a round trip instead. Shipped
    without a single test calling the command with the flag set -- the same
    hole as the reconcile NameError, three days apart.
    """
    from rlwrld_worklog import cli, digest, reconcile as reconcile_module

    monkeypatch.setattr(
        digest, "resolve_people", lambda url, names: {"resolved": {"x": "p_1"}, "unresolved": [], "ambiguous": {}}
    )
    monkeypatch.setattr(
        reconcile_module,
        "explain_calendar_day",
        lambda url, person, day, **kwargs: {
            "day": day.isoformat(),
            "ledger": [
                {
                    "key": "k1",
                    "entity_id": "k1",
                    "summary": "주간 리뷰",
                    "starts": "2026-09-15T10:00:00+09:00",
                    "capture_profile": "live-google-calendar-occurrences/v1",
                    "recurring_of": "weekly",
                }
            ],
            "source": [],
            "ledger_only": ["k1"],
            "source_only": [],
            "digest": [
                {"at": "10:00", "where": "회의: 주간 리뷰", "permalink": None},
                {"at": "10:00", "where": "회의: 주간 리뷰", "permalink": None},
            ],
            "digest_repeats": ["10:00 회의: 주간 리뷰"],
            "source_measured": True,
        },
    )

    class Args:
        person_name = "x"
        since = None
        until = None
        explain = "2026-09-15"
        database_url = "postgresql://fake"
        source = []
        gaps_only = False
        no_source_read = True

    assert cli.reconcile_command(Args()) == 0
    printed = capsys.readouterr().out
    assert "주간 리뷰" in printed
    assert "원장에만" in printed
    assert "live-google-calendar-occurrences/v1" in printed
    # The third side, and which line is doubled on it.
    assert "다이제스트 2줄" in printed
    assert "중복" in printed


def test_the_table_still_insists_on_a_range():
    from rlwrld_worklog import cli

    class Args:
        person_name = "x"
        since = None
        until = None
        explain = None
        database_url = "postgresql://fake"
        source = []
        gaps_only = False
        no_source_read = True

    with pytest.raises(SystemExit):
        cli.reconcile_command(Args())


def test_the_calendar_count_leaves_out_meetings_that_were_cancelled():
    """A ledger that keeps cancellations is right; a day count that does is not.

    On 2026-09-15 the ledger held eighteen calendar keys against the calendar's
    fourteen, and --explain showed the four: cancelled instances of a weekly
    meeting, still on the ledger because the ledger records observations.
    """
    from rlwrld_worklog.reconcile import _LEDGER_BY_PERSON

    assert "'cancelled'" in _LEDGER_BY_PERSON["google_calendar"]


def test_explain_can_be_pointed_at_slack(monkeypatch, capsys):
    """Two of seven days disagree by one message, in opposite directions.

    One message is not worth a theory and is worth a listing: a lagging search
    index, a deleted message and a bot post look different when you read them,
    and identical when you count them.
    """
    from rlwrld_worklog import cli, digest, reconcile as reconcile_module

    monkeypatch.setattr(
        digest, "resolve_people", lambda url, names: {"resolved": {"x": "p_1"}, "unresolved": [], "ambiguous": {}}
    )
    monkeypatch.setattr(
        reconcile_module,
        "explain_slack_day",
        lambda url, person, day, **kwargs: {
            "day": day.isoformat(),
            "ledger": [
                {
                    "channel": "C1",
                    "ts": "1.0",
                    "text": "안녕",
                    "capture_profile": "live-slack-web-api/v1",
                    "key": ("C1", "1.0"),
                }
            ],
            "source_measured": True,
            "ledger_only": [("C1", "1.0")],
            "source_only": [("C2", "9.9")],
        },
    )

    class Args:
        person_name = "x"
        since = None
        until = None
        explain = "2026-09-15"
        database_url = "postgresql://fake"
        source = ["slack"]
        gaps_only = False
        no_source_read = True

    assert cli.reconcile_command(Args()) == 0
    printed = capsys.readouterr().out
    assert "슬랙" in printed
    assert "원장에만" in printed and "안녕" in printed
    assert "원본에만" in printed and "9.9" in printed
