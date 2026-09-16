"""The layer-by-layer check that replaces this week's hand-written probes."""

from __future__ import annotations

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


def test_slack_is_asked_the_same_question_a_person_would_type():
    calls = []

    class Fake:
        def call(self, method, **params):
            calls.append((method, params))
            return {"messages": {"total": 30, "matches": []}}

    assert slack_day_count(Fake(), "U07EKRU6F7H", date(2026, 9, 15)) == 30
    method, params = calls[0]
    assert method == "search.messages"
    assert params["query"] == "from:<@U07EKRU6F7H> on:2026-09-15"


def test_slack_reports_not_measured_rather_than_zero_when_it_answers_oddly():
    class Fake:
        def call(self, method, **params):
            return {"ok": False}

    assert slack_day_count(Fake(), "U1", date(2026, 9, 15)) is None


def test_a_meeting_on_two_calendars_is_counted_once():
    class Fake:
        def list_events(self, calendar_id, **params):
            return [{"id": "evt-1"}, {"id": f"only-{calendar_id}"}]

    assert calendar_day_count(Fake(), ["a@x", "b@x"], date(2026, 9, 15)) == 3


def test_the_calendar_window_is_the_kst_day():
    seen = {}

    class Fake:
        def list_events(self, calendar_id, **params):
            seen.update(params)
            return []

    calendar_day_count(Fake(), ["a@x"], date(2026, 9, 15))
    assert seen["timeMin"].startswith("2026-09-15T00:00:00+09:00")
    assert seen["timeMax"].startswith("2026-09-16T00:00:00+09:00")
