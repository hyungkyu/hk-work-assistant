"""The backoffice page keeps every screen it already had, plus 수집 현황.

The project has no browser in the test environment, so this checks the two
things a regression would actually break: the page's own structure (nav
entries, sections, and every element the script reaches for), and the routes
those screens call.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "src" / "rlwrld_worklog" / "static"
ADMIN = STATIC / "admin.html"


@pytest.fixture(scope="module")
def html() -> str:
    return ADMIN.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def script(html: str) -> str:
    return html[html.index("<script>") + len("<script>") : html.rindex("</script>")]


def test_the_menu_is_grouped_and_ordered_as_the_operator_asked() -> None:
    html = ADMIN.read_text(encoding="utf-8")
    groups = re.findall(
        r'<div class="nav-group">(.*?)</div>', html.split("</nav>")[0], flags=re.S
    )
    parsed = [
        (
            (re.search(r'<span class="nav-label">([^<]+)</span>', block) or [None, None])[1],
            re.findall(r'data-page="([a-z]+)"[^>]*>([^<]+)</button>', block),
        )
        for block in groups
    ]
    assert parsed == [
        ("업무", [("work", "업무 현황"), ("roadmap", "로드맵")]),
        (
            "운영",
            [
                ("collection", "수집 현황"),
                ("search", "검색"),
                ("server", "서버 상태"),
                ("schedules", "스케줄"),
            ],
        ),
        (
            "설정",
            [
                ("connections", "연결"),
                ("storage", "저장소 · 백업"),
                ("models", "로컬 모델"),
                ("security", "보안"),
            ],
        ),
        (None, [("audit", "감사 기록")]),
    ]


def test_the_landing_screen_after_login_is_the_work_board(html: str, script: str) -> None:
    assert '<button data-page="work" class="active">업무 현황</button>' in html
    assert html.count('class="page active"') == 1
    assert 'class="page active" id="page-work"' in html
    # Both login paths now hand control to the hash router, which opens the
    # work board when there is no hash and loads whatever screen a shared
    # link names. The landing screen is the router's default, not a hardcoded
    # call, so the assertion moved with it.
    # Assert the shape, not the exact spacing: a comment between the two calls
    # is not a behaviour change.
    assert len(re.findall(r"await loadSettings\(\);(?:\s|//[^\n]*\n)*applyHash\(\);", script)) == 2
    assert "const DEFAULT_PAGE = 'work';" in script


def test_the_roadmap_is_a_disabled_placeholder_with_no_behaviour(
    html: str, script: str
) -> None:
    assert '<button data-page="roadmap" disabled' in html
    assert 'id="page-roadmap"' in html
    assert "if (!button || button.disabled) return;" in script
    # A placeholder must not have grown a loader, an endpoint, or a poll.
    assert "loadRoadmap" not in script
    assert "roadmap" not in script.replace("data-page=\"roadmap\"", "")


def test_every_navigation_entry_has_exactly_one_page_section(html: str) -> None:
    pages = re.findall(r'<button data-page="([a-z]+)"', html)
    for page in pages:
        assert html.count(f'id="page-{page}"') == 1, f"page-{page} section is missing"
    sections = set(re.findall(r'id="page-([a-z]+)"', html))
    assert sections == set(pages), "no page section may be unreachable from the menu"


def test_the_existing_screens_and_the_work_board_are_untouched(html: str) -> None:
    for marker in (
        'id="metric-data-root"',
        'id="metric-drive"',
        'id="metric-slack"',
        'id="work-board"',
        'id="work-filter-assignee"',
        'id="work-add"',
        'id="work-overlay"',
        'id="work-form"',
        'id="audit-body"',
    ):
        assert marker in html, f"{marker} disappeared from the backoffice page"


def test_the_collection_screen_has_the_four_panels_the_page_promises(html: str) -> None:
    for marker in (
        'id="collection-cards"',
        'id="collection-runs-body"',
        'id="coverage-head"',
        'id="coverage-body"',
        'id="collection-rules"',
        'id="coverage-start"',
        'id="coverage-end"',
        'id="coverage-group"',
        'id="collection-environment"',
    ):
        assert marker in html


def test_every_element_the_script_reaches_for_exists(html: str, script: str) -> None:
    ids = set(re.findall(r'\sid="([^"]+)"', html))
    used = set(re.findall(r"\$\('([^']+)'\)", script))
    assert not used - ids, f"the script reads elements that do not exist: {sorted(used - ids)}"


def test_the_page_renders_server_data_as_text_never_as_markup(script: str) -> None:
    """Run ids, paths and rule text all come from disk; none of it is HTML."""
    assert "innerHTML" not in script
    assert "insertAdjacentHTML" not in script
    assert "document.write" not in script


def test_the_board_can_never_silently_drop_an_item(html: str, script: str) -> None:
    """The regression that hid an in_progress item: an unclaimed status."""
    assert "const WORK_RESIDUE" in script
    assert "residue: true" in script
    assert "columns.push({ ...WORK_RESIDUE" in script
    # Columns and labels are the server's schema, so page and store cannot drift.
    assert "await api('/api/v1/admin/work/meta')" in script
    assert "meta.columns.map(" in script
    # Anything not on screen is stated, including what a filter is hiding.
    assert "function renderWorkHint" in script
    assert "필터가 켜져 있어" in script
    assert 'id="work-hint"' in html
    assert "전체 ${workState.total}건" in script


def test_the_schedule_screen_shows_state_it_read_and_never_invents_it(
    html: str, script: str
) -> None:
    assert 'id="schedule-list"' in html
    assert "await api('/api/v1/admin/schedules')" in script
    for label in ("실행 주체", "주기", "대상 범위", "중복 방지", "로그", "실패 확인법", "상세 설명"):
        assert label in script, f"the schedule table must show {label}"
    assert "systemd 기준(확정)" in script
    assert "설정값이며 확정 아님" in script
    assert "불명 — ${next.unknown_reason" in script
    # The existing execution-time settings form stays on the page.
    assert 'id="daily-hour"' in html and 'id="timezone"' in html


def test_leaving_a_screen_stops_its_polling(script: str) -> None:
    """Navigation goes through one router, which stops every poller first.

    Previously each nav branch had to remember to stop the other screen's
    timer. Now `applyHash` stops both unconditionally and starts only the one
    the target screen needs, so a screen can no longer be left polling behind
    another one's back.
    """
    router = script[script.index("function applyHash()") : script.index("window.addEventListener('hashchange'")]
    assert router.index("stopWorkPolling();") < router.index("if (page === 'work') { loadWork();")
    assert (
        router.index("stopCollectionPolling();")
        < router.index("if (page === 'collection') { loadCollection(")
    )
    assert "startWorkPolling();" in router and "startCollectionPolling();" in router


def test_navigation_is_routed_through_the_hash(html: str, script: str) -> None:
    """One path for a click, a reload, the back button and a shared link."""
    assert "window.addEventListener('hashchange', applyHash);" in script
    assert "location.hash = target;" in script
    # Routable screens are derived from the nav, so a screen added later
    # cannot silently become unlinkable; disabled entries stay out.
    assert "document.querySelectorAll('nav button[data-page]')" in script
    assert "filter((button) => !button.disabled)" in script
    assert "history.replaceState" in script and "history.pushState" in script


def test_an_unsaved_editing_dialog_is_never_restored_by_a_link(script: str) -> None:
    router = script[script.index("function applyHash()") : script.index("window.addEventListener('hashchange'")]
    assert "closeWorkEditor();" in router
    assert "closeTimeline();" in router


def test_the_screen_shows_when_the_server_built_the_answer(html: str, script: str) -> None:
    """Not the browser's clock: that would claim a freshness the data lacks."""
    assert 'id="collection-freshness"' in html
    assert "collectionState.overviewAt = overview.generated_at;" in script
    assert "collectionState.coverageAt = payload.generated_at;" in script
    assert 'id="collection-hard-refresh"' in html
    assert "/api/v1/admin/collection/refresh?screen=all" in script


def test_the_coverage_table_shows_newest_dates_first(script: str) -> None:
    assert "payload.rows.slice().reverse()" in script


def test_a_date_still_in_progress_cannot_be_badged_as_collected(script: str) -> None:
    """The time axis gates the badge, mirroring the server-side verdict."""
    badge = script[script.index("function coverageBadge(cell)") : script.index("function renderCoverage")]
    assert "time === 'in_progress'" in badge
    assert "진행 중 (${at}까지)" in badge
    assert "부분수집 (${at}까지)" in badge
    assert "TIME_COVERAGE_LABELS" in script


def test_the_page_stays_usable_on_a_narrow_screen(html: str) -> None:
    assert "@media (max-width: 880px)" in html
    assert ".table-scroll { overflow-x: auto;" in html
    assert ".collection-toolbar select, .collection-toolbar input, .coverage-controls select" in html


def test_the_existing_backoffice_and_work_routes_are_all_still_served() -> None:
    from rlwrld_worklog.web import app

    paths = set(app.openapi()["paths"])
    assert {
        "/backoffice",
        "/api/v1/admin/session",
        "/api/v1/admin/settings",
        "/api/v1/admin/audit",
        "/api/v1/admin/work/items",
        "/api/v1/admin/work/meta",
        "/api/v1/admin/work/history",
        "/api/v1/timeline",
        "/healthz",
    } <= paths


def test_the_backoffice_page_is_served_without_caching() -> None:
    from rlwrld_worklog.admin_web import backoffice_page

    response = backoffice_page()
    assert response.headers["Cache-Control"] == "no-store"
    assert b'data-page="collection"' in response.body
    assert b'data-page="schedules"' in response.body


def test_the_work_board_offers_an_activity_timeline(html: str, script: str) -> None:
    for marker in ('id="timeline-overlay"', 'id="timeline-list"', 'id="timeline-state"',
                   'id="timeline-close"', 'id="timeline-title"'):
        assert marker in html
    assert "openTimeline" in script
    assert "/timeline?limit=" in script


def test_the_timeline_names_legacy_records_instead_of_hiding_them(script: str) -> None:
    """A pre-timeline record must read as incomplete, not as empty."""
    assert "record_schema === 'legacy'" in script
    assert "unknown_fields" in script
    assert "TIMELINE_RESOLUTION_LABELS" in script
    for label in ("declared", "inferred", "unresolved"):
        assert label in script


def _rule_body(html: str, selector: str) -> str:
    """The declarations of one CSS rule, so a test can assert what it renders."""
    start = html.index(selector) + len(selector)
    return html[start : html.index("}", start)]


def test_the_coverage_vocabulary_is_wired_into_the_page(html: str, script: str) -> None:
    for key in ("collected_with_skips", "unverified", "evidence_class", "EVIDENCE_LABELS"):
        assert key in script
    assert ".cov.unverified" in html


def test_unverified_is_drawn_off_the_good_bad_colour_scale(html: str) -> None:
    """Asserts the rendered property, not merely that the selector exists.

    A legacy date is not "good" or "bad" -- it is a weaker grade of evidence.
    Painting it in the same green or amber as a run manifest is the bug this
    styling exists to prevent, so the test checks the colours themselves.
    """
    body = _rule_body(html, ".cov.unverified {")
    for good_or_bad in ("var(--accent)", "var(--warning)", "var(--danger)", "#102b22", "#2b2415"):
        assert good_or_bad not in body, f"unverified must not use {good_or_bad}: {body}"
    assert "var(--muted)" in body
    assert "background: transparent" in body
    assert "dotted" in body

    # And the states that DO carry a verdict keep their scale.
    assert "var(--accent)" in _rule_body(html, ".cov.collected_with_skips {")
