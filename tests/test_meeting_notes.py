"""Mapping a calendar meeting to the Notion page that recorded it.

The cases here are the shapes the real `M Meetings` database holds, including
the one where the title's date and the date property disagree.
"""

from __future__ import annotations

from rlwrld_worklog.meeting_notes import (
    match_note,
    meeting_date,
    page_title,
    similarity,
    strip_date_prefix,
)


def _page(title: str, date: str | None = None, date_name: str = "Meeting date"):
    properties = {
        "Name": {"type": "title", "title": [{"plain_text": title}]},
    }
    if date is not None:
        properties[date_name] = {"type": "date", "date": {"start": date}}
    return {"properties": properties}


def test_the_date_property_wins_over_the_date_in_the_title():
    """Real page: titled [09/09(수)], Meeting date 09-10. The property is right."""
    page = _page("[09/09(수)] DEEP ROBOTICS", "2026-09-10")
    assert meeting_date(page) == "2026-09-10"


def test_every_title_shape_reduces_to_the_same_name():
    for title in (
        "[09/09(수)] 딥로보틱스",
        "09/09 딥로보틱스",
        "2026-09-09 딥로보틱스",
        "9.9 딥로보틱스",
        "딥로보틱스 미팅",
        "딥로보틱스",
    ):
        assert "딥로보틱스" in strip_date_prefix(title)
    assert strip_date_prefix("[09/09(수)] 딥로보틱스") == "딥로보틱스"


def test_a_name_that_only_looks_like_a_date_is_left_alone():
    assert strip_date_prefix("Q3 로드맵") == "Q3 로드맵"
    assert strip_date_prefix("1:1 with Storm") == "1:1 with Storm"


def test_the_title_comes_from_the_title_property_not_from_every_property():
    page = _page("주간 리뷰", "2026-09-15")
    page["properties"]["Owner"] = {
        "type": "rich_text",
        "rich_text": [{"plain_text": "류형규"}],
    }
    assert page_title(page) == "주간 리뷰"


def test_a_meeting_matches_its_note_across_the_usual_differences():
    assert similarity("DEEP ROBOTICS", "[09/09(수)] DEEP ROBOTICS") == 1.0
    assert similarity("딥로보틱스 정기미팅", "딥로보틱스") >= 0.9
    assert similarity("Storm 1:1", "Gerald 1:1") < 0.6


def test_the_note_is_attached_when_one_page_clearly_fits():
    pages = {
        "p1": _page("[09/15(월)] DEEP ROBOTICS", "2026-09-15"),
        "p2": _page("주간 리서치 리뷰", "2026-09-15"),
    }
    assert match_note("DEEP ROBOTICS", "2026-09-15", pages) == "p1"


def test_two_pages_that_both_fit_attach_nothing():
    """DEEP ROBOTICS exists four times. A coin flip here writes the wrong
    decisions onto a meeting, which is worse than showing no note at all."""
    pages = {
        "p1": _page("DEEP ROBOTICS", "2026-09-15"),
        "p2": _page("DEEP ROBOTICS", "2026-09-15"),
    }
    assert match_note("DEEP ROBOTICS", "2026-09-15", pages) is None


def test_a_page_from_another_day_is_never_the_note():
    pages = {"p1": _page("DEEP ROBOTICS", "2026-09-14")}
    assert match_note("DEEP ROBOTICS", "2026-09-15", pages) is None


def test_a_page_with_no_meeting_date_is_not_a_candidate():
    pages = {"p1": _page("DEEP ROBOTICS")}
    assert match_note("DEEP ROBOTICS", "2026-09-15", pages) is None


def test_a_day_whose_pages_are_all_about_something_else_attaches_nothing():
    pages = {
        "p1": _page("채용 파이프라인 정리", "2026-09-15"),
        "p2": _page("예산 검토", "2026-09-15"),
    }
    assert match_note("DEEP ROBOTICS", "2026-09-15", pages) is None


def test_other_databases_name_the_date_property_differently():
    assert meeting_date(_page("x", "2026-09-15", date_name="회의 날짜")) == "2026-09-15"
    assert meeting_date(_page("x", "2026-09-15", date_name="Date")) == "2026-09-15"


def test_a_meeting_with_no_title_is_not_guessed_at():
    pages = {"p1": _page("DEEP ROBOTICS", "2026-09-15")}
    assert match_note("", "2026-09-15", pages) is None
    assert match_note(None, "2026-09-15", pages) is None


def test_the_meeting_line_gets_the_page_link_and_the_count_says_how_many():
    """Wired into the digest, not just a matcher sitting in a module."""
    from rlwrld_worklog.digest import attach_meeting_pages

    pages = {
        "abc": {
            "title": "[09/15(월)] DEEP ROBOTICS",
            "url": "https://www.notion.so/abc",
            "raw": _page("[09/15(월)] DEEP ROBOTICS", "2026-09-15"),
        },
        "def": {
            "title": "예산 검토",
            "url": "https://www.notion.so/def",
            "raw": _page("예산 검토", "2026-09-15"),
        },
    }
    events = [
        {"source": "google_calendar", "where": "회의: DEEP ROBOTICS", "links": []},
        {"source": "google_calendar", "where": "회의: 알 수 없는 미팅", "links": []},
        {"source": "slack", "where": "#general"},
    ]

    assert attach_meeting_pages(events, pages, "2026-09-15") == 1
    assert events[0]["links"][0]["url"] == "https://www.notion.so/abc"
    assert events[1]["links"] == []
    # A Slack line is never given a meeting note.
    assert "links" not in events[2]


def test_a_meeting_keeps_the_gemini_note_it_already_had():
    from rlwrld_worklog.digest import attach_meeting_pages

    pages = {
        "abc": {
            "title": "주간 리뷰",
            "url": "https://www.notion.so/abc",
            "raw": _page("주간 리뷰", "2026-09-15"),
        }
    }
    event = {
        "source": "google_calendar",
        "where": "회의: 주간 리뷰",
        "links": [{"url": "https://docs.google.com/x", "title": "Gemini 노트"}],
    }
    attach_meeting_pages([event], pages, "2026-09-15")
    assert [link["title"] for link in event["links"]] == ["Gemini 노트", "주간 리뷰"]
