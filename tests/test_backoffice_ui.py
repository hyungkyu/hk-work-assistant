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
ADMIN_JS = STATIC / "admin.js"
ADMIN_CSS = STATIC / "admin.css"


@pytest.fixture(scope="module")
def html() -> str:
    return ADMIN.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def script(html: str) -> str:
    # The script now lives in its own file (admin.js), split out of the page
    # so markup, style and behaviour are three files that different work can
    # touch without colliding. The page references it and nothing else runs
    # inline, which the tests below assert.
    return ADMIN_JS.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def css() -> str:
    # Styles split into their own file, same reason as the script.
    return ADMIN_CSS.read_text(encoding="utf-8")


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
        # HK, 2026-09-28: 북마크를 만들자. 사내 주요 시스템에 접근하기 위함이야.
        # First, and on its own, because it is the one screen opened to leave
        # the backoffice rather than to read something in it.
        ("바로가기", [("bookmarks", "북마크")]),
        ("업무", [("work", "업무 현황"), ("roadmap", "로드맵")]),
        # HK, 2026-10-01: 업무 밑에 정보 를 넣고, 정보 안에 GPU 가격 을.
        # Reference about the world outside, which is neither the org's own
        # activity (다이제스트) nor the running of this system (운영).
        ("정보", [("gpu", "GPU 가격")]),
        # The org chart and the per-person day sit together, above 운영,
        # because they are what somebody opens to answer a question about a
        # person -- the collection screens are how the data behind them got
        # there.
        (
            "다이제스트",
            [("org", "조직도"), ("person", "일자별"), ("unmapped", "미확인 계정")],
        ),
        # HK, 2026-09-21: 어드민에서 Q&A pair 를 선택/삭제할 수 있게 하면 어때?
        # Its own group rather than a row inside 다이제스트: the digest screens
        # answer "what happened", and this one asks him a question.
        ("말투", [("pairs", "질문·답변 검수")]),
        (
            "운영",
            [
                ("collection", "수집 현황"),
                ("search", "검색"),
                ("server", "서버 상태"),
                ("schedules", "스케줄"),
                ("release", "릴리즈 노트"),
            ],
        ),
        (
            "설정",
            [
                ("connections", "연결"),
                ("storage", "저장소 · 백업"),
                ("models", "로컬 모델"),
                ("security", "보안"),
                # 2026-09-29: the roadmap's team x product mapping. It sits in
                # 설정 rather than beside 로드맵 because 로드맵 is the screen
                # everybody reads and this is the one only HK edits.
                ("mapping", "로드맵 매핑"),
                ("menu", "메뉴 편집"),
            ],
        ),
        (None, [("audit", "감사 기록")]),
    ]


def test_the_landing_screen_after_login_is_the_work_board(html: str, script: str) -> None:
    # The entry, not its attribute order. `data-requires="super_admin"` was
    # added to every owner-only entry after this test was written, and pinning
    # the exact spelling turned that into a landing-screen failure, which it
    # is not. What this line is for is that 업무 현황 is the one entry marked
    # active in the markup.
    assert re.search(r'<button data-page="work"[^>]*\bclass="active"[^>]*>업무 현황</button>', html)
    assert html.count('class="page active"') == 1
    assert 'class="page active" id="page-work"' in html
    # Both login paths now hand control to the hash router, which opens the
    # work board when there is no hash and loads whatever screen a shared
    # link names. The landing screen is the router's default, not a hardcoded
    # call, so the assertion moved with it.
    # Assert the shape, not the exact spacing: a comment between the two calls
    # is not a behaviour change.
    # Both login paths end at the hash router. What may sit between them is
    # setup that has to finish first -- arranging the left menu joined that on
    # 2026-10-01 -- so the check is that nothing branches away before the
    # router, not that the two calls are adjacent.
    handoffs = re.findall(
        r"await loadSettings\(\);(?P<between>(?:\s|//[^\n]*\n|await \w+\(\);)*)applyHash\(\);",
        script,
    )
    assert len(handoffs) == 2, "both login paths hand over to the hash router"
    for between in handoffs:
        assert "showPage(" not in between, "nothing opens a screen behind the router"
    assert "const DEFAULT_PAGE = 'work';" in script


def test_the_roadmap_is_a_real_screen_now(html: str, script: str) -> None:
    """It was a disabled placeholder until 2026-09-29.

    What replaced it: a screen fed by GET /api/v1/roadmap, and the first entry
    in this menu a company user may open -- which is why it is the one entry
    without `data-requires="super_admin"`.
    """
    assert '<button data-page="roadmap">로드맵</button>' in html
    assert 'id="page-roadmap"' in html
    assert "if (page === 'roadmap') loadRoadmap();" in script
    assert "api('/api/v1/roadmap')" in script
    # The dataset is not inlined into the page any more; that was the point.
    assert "roadmapState" in script
    assert 'id="payload"' not in html


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


def test_the_page_stays_usable_on_a_narrow_screen(css: str) -> None:
    assert "@media (max-width: 880px)" in css
    assert ".table-scroll { overflow-x: auto;" in css
    assert ".collection-toolbar select, .collection-toolbar input, .coverage-controls select" in css


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
        # The left menu's arrangement, read on every page load.
        "/api/v1/admin/menu",
        "/api/v1/admin/release-notes",
        "/api/v1/timeline",
        "/healthz",
        # The review screen is only a screen if its routes are mounted; the
        # router was registered in web.py and this is what says so.
        "/api/v1/admin/voice/pairs",
        "/api/v1/admin/voice/pairs/choose",
        "/api/v1/admin/voice/agreement",
        "/api/v1/admin/voice/audit",
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


def test_the_coverage_vocabulary_is_wired_into_the_page(css: str, script: str) -> None:
    for key in ("collected_with_skips", "unverified", "evidence_class", "EVIDENCE_LABELS"):
        assert key in script
    assert ".cov.unverified" in css


def test_unverified_is_drawn_off_the_good_bad_colour_scale(css: str) -> None:
    """Asserts the rendered property, not merely that the selector exists.

    A legacy date is not "good" or "bad" -- it is a weaker grade of evidence.
    Painting it in the same green or amber as a run manifest is the bug this
    styling exists to prevent, so the test checks the colours themselves.
    """
    body = _rule_body(css, ".cov.unverified {")
    for good_or_bad in ("var(--accent)", "var(--warning)", "var(--danger)", "#102b22", "#2b2415"):
        assert good_or_bad not in body, f"unverified must not use {good_or_bad}: {body}"
    assert "var(--muted)" in body
    assert "background: transparent" in body
    assert "dotted" in body

    # And the states that DO carry a verdict keep their scale.
    assert "var(--accent)" in _rule_body(css, ".cov.collected_with_skips {")


# --- The page is three files, not one (P0, 2026-09-14) ----------------------
#
# admin.html was 2,700 lines with the style and the whole script inline, so any
# two changes to it collided and no two people could work it at once. It is now
# markup + admin.css + admin.js, each servable on its own.


def test_the_page_pulls_in_the_split_out_files_and_inlines_nothing() -> None:
    html = ADMIN.read_text(encoding="utf-8")
    assert '<link rel="stylesheet" href="/backoffice/admin.css">' in html
    assert '<script src="/backoffice/admin.js"></script>' in html
    # Nothing runs or styles inline any more: exactly the split that lets the
    # three files be edited independently.
    assert "<style" not in html
    assert "<script>" not in html


def test_the_split_files_carry_the_real_content() -> None:
    assert ":root" in ADMIN_CSS.read_text(encoding="utf-8")
    assert "initialize()" in ADMIN_JS.read_text(encoding="utf-8")


def test_the_css_and_js_are_served() -> None:
    from rlwrld_worklog.web import app

    paths = set(app.openapi()["paths"])
    assert {"/backoffice/admin.css", "/backoffice/admin.js"} <= paths


def test_the_served_assets_match_the_files_and_are_typed(tmp_path, monkeypatch) -> None:
    from rlwrld_worklog import admin_web

    css = admin_web.backoffice_css()
    js = admin_web.backoffice_js()
    assert css.media_type == "text/css"
    assert js.media_type == "text/javascript"
    assert css.body.decode("utf-8") == ADMIN_CSS.read_text(encoding="utf-8")
    assert js.body.decode("utf-8") == ADMIN_JS.read_text(encoding="utf-8")


def test_the_review_screen_shows_the_answer_with_its_candidates(
    html: str, script: str
) -> None:
    """HK, 2026-09-21: 이 답변의 원 질문은 이거 일거 같다는 후보들이 있어서,
    난 그걸 선택하는거지.

    So the unit on screen is one answer with several candidate questions, one
    of them already marked -- not a list of pairs to judge one row at a time.
    """
    assert 'id="page-pairs"' in html
    assert 'id="pairs-list"' in html
    # The card is built in the script, so that is where the shape lives.
    assert "function pairCard(" in script
    assert "answer.candidates" in script
    assert "'제안'" in script and "'선택'" in script


def test_none_of_these_is_offered_as_its_own_answer(html: str, script: str) -> None:
    """The most informative correction has to be one click.

    If saying "none of these was the question" needed him to skip the row
    instead, the queue would only ever collect agreement, and the agreement
    rate would measure nothing.
    """
    assert "해당 없음" in html or "해당 없음" in script
    assert "choosePair(answer, null)" in script


def test_the_review_screen_never_rebuilds_what_it_shows(script: str) -> None:
    """The batch builds the candidates; this screen only chooses among them.

    A rebuild button here would change the queue between the moment he read a
    row and the moment he clicked it, and the stored correction would no
    longer say what he chose it over.
    """
    # To the next section marker rather than to loadSchedules: another
    # section landed between them on 2026-10-01 and was silently swept into
    # this one, which made the test read a different screen's calls.
    pairs_block = script.split("질문·답변 검수")[1].split("// ----")[0]
    # Only GET, plus the one POST that records his decision. Anything else
    # reaching the server from this screen would be it acting rather than
    # asking. ("proposed" is a field on a candidate, not a call -- the check is
    # on what this screen sends.)
    calls = __import__("re").findall(r"api\(`?'?([^'`,\)]+)", pairs_block)
    assert calls, "the screen talks to the server"
    for call in calls:
        assert call.startswith("/api/v1/admin/voice/"), call
    posts = __import__("re").findall(r"method: '(\w+)'", pairs_block)
    assert posts == ["POST"], f"one write from this screen, the decision: {posts}"
    assert "/api/v1/admin/voice/pairs/choose" in pairs_block


def test_a_decision_does_not_renumber_the_queue_under_him(script: str) -> None:
    """One row leaves; the rest stay where they were.

    Re-fetching the page after every click would reorder everything he had not
    read yet, which is how a person loses their place in a list of three
    thousand.
    """
    assert "if (card) card.remove();" in script


def test_the_proposal_rate_is_shown_next_to_the_queue(html: str, script: str) -> None:
    """The number the screen exists to move, not buried in a command.

    If the proposal keeps being wrong, reviewing is data entry and the ranking
    needs changing -- which is only visible if the rate is in front of him.
    """
    assert 'id="pairs-health"' in html
    assert "/api/v1/admin/voice/agreement" in script
    # A rate over zero decisions is an unanswered question, not 0%.
    assert "agreement_rate === null" in script


def test_the_question_is_read_before_the_answer(script: str) -> None:
    """HK, 2026-09-23: 질문/대답 순서로 해주면 좋겠어.

    The order the exchange happened in, and not only for looks: reading his
    answer first makes every candidate look plausible, because a question can
    be fitted to an answer after the fact. Question first asks "was this the
    one he was answering" instead of "could this have been".

    So in the card the candidates are built and appended before his answer is.
    """
    card = script.split("function pairCard(")[1].split("async function choosePair")[0]
    questions_at = card.index("questions.appendChild(row)")
    answer_at = card.index("said.className = 'pair-answer'")
    assert questions_at < answer_at, (
        "his answer is being rendered above the candidate questions"
    )
    assert "'질문 후보'" in card and "'HK 의 답변'" in card, (
        "each half is labelled; an unlabelled pair of blocks is a guess about "
        "which is which"
    )


def test_a_recorded_decision_moves_a_number_on_screen(script: str) -> None:
    """HK, 2026-09-23: 대답을 선택하고 있는데 제대로 되고 있는거야?

    The screen was not answering that: the row vanished and a toast said so
    for two seconds, and a dropped click would have looked identical. A count
    that moves on every decision, and stays put, is the difference between
    "it worked" and "it looked like it worked".
    """
    choose = script.split("async function choosePair(")[1].split("function renderPairCounts")[0]
    assert "pairsState.decided += 1" in choose
    assert "renderPairCounts()" in choose
    # And it is updated only after the request came back, so a failed write
    # never increments anything.
    assert choose.index("await api(") < choose.index("pairsState.decided += 1")
    assert "toast(`기록하지 못했습니다" in choose, "a failure says so"


def test_the_bookmark_screen_has_the_form_and_the_list(html: str, script: str) -> None:
    """HK, 2026-09-28: 북마크를 만들자 … 잘 그루핑해줘."""
    for marker in (
        'id="page-bookmarks"',
        'id="bookmark-group"',
        'id="bookmark-groups"',
        'id="bookmark-label"',
        'id="bookmark-url"',
        'id="bookmark-note"',
        'id="bookmark-save"',
        'id="bookmark-cancel"',
        'id="bookmark-list"',
        'id="bookmark-state"',
    ):
        assert marker in html, f"{marker} is missing from the bookmark screen"
    assert "if (page === 'bookmarks') loadBookmarks();" in script
    assert "await api('/api/v1/admin/bookmarks')" in script


def test_a_bookmark_is_only_ever_linked_when_it_is_an_http_address(script: str) -> None:
    """The store refuses the rest; this is the second place it is refused.

    Catches the mutation that sets `link.href = bookmark.url` unconditionally
    -- a `javascript:` row written into the file by hand would then run in a
    super-admin's session on every load of the screen.
    """
    assert "function safeBookmarkURL" in script
    assert "/^https?:\\/\\//i.test(url)" in script
    render = script.split("function renderBookmarkGroups(")[1].split("function bookmarkCount(")[0]
    assert "const href = safeBookmarkURL(bookmark.url);" in render
    assert render.count("link.href") == 1
    assert "if (href) { link.href = href;" in render
    # A row it cannot link is still drawn, because a hidden bad entry is one
    # nobody fixes.
    assert "열 수 없는 주소입니다" in render


def test_a_bookmark_opens_in_a_new_tab_without_handing_over_the_session(
    script: str,
) -> None:
    render = script.split("function renderBookmarkGroups(")[1].split("function bookmarkCount(")[0]
    assert "link.target = '_blank'" in render
    assert "link.rel = 'noopener noreferrer'" in render


def test_the_bookmark_routes_are_mounted() -> None:
    from rlwrld_worklog.web import app

    paths = set(app.openapi()["paths"])
    assert {"/api/v1/admin/bookmarks", "/api/v1/admin/bookmarks/{bookmark_id}"} <= paths


def test_each_unbuilt_screen_has_a_region_of_its_own(html: str) -> None:
    """Two sessions in one file, with a line that says whose is whose.

    On 2026-09-30 two sessions edited cli.py without either being able to see
    the other, and one reverted the other's work. admin.html is the file most
    likely to repeat it -- every screen lives in it. So each unbuilt screen
    gets a named region, and the comment says who owns it.
    """
    gpu = html.index('id="page-gpu"')
    menu = html.index('id="page-menu"')
    assert gpu < menu
    # Nothing of one session's section may sit inside the other's.
    between = html[gpu:menu]
    assert between.count("<section") == 1, "the regions do not overlap"


def test_the_left_menu_is_rearranged_not_rebuilt(script: str) -> None:
    """The buttons in the markup are moved; they are never created from data.

    A menu built out of server rows could name a page that has no section,
    and clicking it would open nothing -- the failure would look like a
    broken screen rather than a bad row. Moving what already exists cannot
    produce an entry that leads nowhere.
    """
    block = script.split("async function applyMenuArrangement")[1].split(
        "async function loadSchedules"
    )[0]
    assert "nav.querySelectorAll('button[data-page]')" in block, (
        "it takes the existing buttons"
    )
    assert "createElement('button')" not in block, (
        "a button invented here could point at a section that does not exist"
    )


def test_a_screen_the_arrangement_does_not_mention_still_appears(script: str) -> None:
    """A screen added after the menu was last saved must not vanish.

    This is the rule that lets a session add a screen without touching the
    menu at all, which is the entire reason the arrangement moved out of the
    markup.
    """
    block = script.split("async function applyMenuArrangement")[1].split(
        "async function loadSchedules"
    )[0]
    # The condition moved when hidden screens stopped being treated as new
    # (2026-10-06): what is placed at the end is now the screens the
    # arrangement has never heard of, not everything it did not draw.
    assert "if (added.length)" in block, "the leftovers are placed, not dropped"


def test_the_menu_falls_back_to_the_markup_when_the_server_cannot_say(
    script: str,
) -> None:
    """The order in the file is never wrong about what exists.

    So a failed read leaves it alone rather than clearing the navigation --
    a backoffice with no menu is one nobody can use to find out why.
    """
    block = script.split("async function applyMenuArrangement")[1].split(
        "async function loadSchedules"
    )[0]
    assert "return;  // The markup's own order stands" in block
    assert "payload.source !== 'database'" in block


def test_the_menu_editor_says_which_order_it_is_showing(html: str, script: str) -> None:
    """Saved-but-not-showing and never-saved look identical otherwise."""
    assert 'id="menu-state"' in html and 'id="menu-badge"' in html
    assert "코드 순서로 보여주는 중입니다" in script
def test_the_gpu_price_screen_is_built_and_its_menu_entry_is_live(
    html: str, script: str
) -> None:
    """The last step: the button mori shipped disabled is reachable now."""
    entry = re.search(r'<button data-page="gpu"[^>]*>', html)
    assert entry and "disabled" not in entry.group(0)
    assert "if (page === 'gpu') loadCloudPricing(null);" in script
    assert "준비 중입니다" not in html[html.index('id="page-gpu"'):html.index('id="page-menu"')]


def test_the_gpu_price_routes_are_mounted() -> None:
    from rlwrld_worklog.web import app

    paths = set(app.openapi()["paths"])
    assert {
        "/api/v1/admin/cloud-pricing",
        "/api/v1/admin/cloud-pricing/refresh",
        "/api/v1/admin/cloud-pricing/snapshots/{snapshot_id}",
    } <= paths


def test_the_price_screen_shows_the_original_beside_the_converted(
    html: str, script: str
) -> None:
    """Two columns, because they are two different kinds of fact: the left is
    what the provider published, the right is ours and only as good as the
    rate printed above it."""
    # The header is drawn per category now, so it lives in the script.
    assert "['원가', 'text-align:right'], ['환율 적용', 'text-align:right']" in script
    assert 'id="cp-asof-body"' in html
    # The rate, and both of the times that bound it.
    assert "'가져온 시각'" in script
    assert "'이 요금이 생긴 시각'" in script
    assert "fx.as_of" in script
    assert "1 ${fx.base} = ${cpNumber(fx.rate, 2)} ${fx.quote}" in script


def test_a_price_we_could_not_convert_leaves_the_won_column_empty(script: str) -> None:
    """Filling it with the original would be a lie told by a table cell."""
    assert "if (value === null || value === undefined) return '—';" in script


def test_the_refresh_button_says_which_of_the_two_things_happened(
    html: str, script: str
) -> None:
    """HK's rule is visible on screen, not only in the database: a refresh
    either moves prices into a new snapshot or moves only the date."""
    assert "if (result.changed) {" in script
    assert "달라진 요금이 없어 가져온 시각과 환율만 새로 적었습니다." in script
    # A provider that failed is named rather than quietly shortening the table.
    assert "에서 받지 못했습니다" in script
    assert 'id="cp-runs-body"' in html


def test_a_drawer_pins_only_its_own_close_button(css: str) -> None:
    """2026-10-02: 요금 히스토리 could be opened and not closed.

    `.drawer .button` is a descendant rule, so it caught every button the
    drawer's body rendered -- each 열기 in the history list stacked at the same
    corner on top of 닫기, and the last one painted took the click. The close
    button is a direct child of the drawer; nothing in its body is.
    """
    assert ".drawer > .button { position: absolute;" in css
    assert ".drawer .button { position: absolute;" not in css


def test_the_price_table_names_the_chip_and_its_memory(html: str, script: str) -> None:
    """HK, 2026-10-02: h100, a100, b300 등 gpu 종류와 gpu memory 정보를 같이."""
    assert "['GPU', '']" in script
    assert "['GPU 메모리', 'text-align:right']" in script
    assert "function cpGpu(" in script and "function cpGpuMemory(" in script


def test_a_memory_the_provider_did_not_publish_stays_blank(script: str) -> None:
    """Nebius and Kakao name the chip and not its memory. A100 ships as 40GB
    and 80GB, so filling the gap from the model name is wrong half the time."""
    assert "return row.gpu_memory_gb ? `${cpNumber(row.gpu_memory_gb, 0)} GB` : '—';" in script


def test_the_gpu_filter_cannot_offer_a_chip_that_is_not_there(script: str) -> None:
    assert "(data.rows || []).filter((r) => r.gpu_model).map((r) => r.gpu_model)" in script
    # Storage rows have no GPU at all, so the filter is not offered there --
    # and only there: 칩별 비교 filters by chip too.
    assert "modelSelect.disabled = cpState.category === 'storage';" in script


def test_the_price_screen_can_rank_one_chip_across_the_clouds(
    html: str, script: str
) -> None:
    """HK, 2026-10-02: 전체 리스트를 gpu 종류, 공급사 별로 filtering해서."""
    assert '<button data-category="compare">칩별 비교</button>' in html
    assert 'id="cp-compare-out"' in html
    assert "function cpCompareGroups(" in script
    # Grouped by chip, cheapest card-hour first.
    assert "groups.get(row.gpu_model).push(row)" in script
    assert "a.per_gpu_krw === null ? Infinity : Number(a.per_gpu_krw)" in script


def test_a_row_with_no_comparable_unit_sorts_last_rather_than_first(
    script: str,
) -> None:
    """Infinity, not 0: a row we cannot price per card must not win the
    ranking by having no number at all."""
    block = script.split("function cpCompareGroups")[1].split("function cpCompareTable")[0]
    assert "Infinity" in block
    assert "? 0 :" not in block


def test_comparing_and_listing_are_not_shown_at_the_same_time(script: str) -> None:
    assert "$('cp-compare-card').classList.toggle('hidden', !comparing);" in script
    assert "$('cp-table-card').classList.toggle('hidden', comparing);" in script


# ---------------------------------------------------------------- 준비 중 표시

def test_a_screen_that_exists_is_not_still_marked_as_coming_soon() -> None:
    """`disabled` is a promise that the screen is not there yet.

    The menu entries get added first, as placeholders, so the arrangement is
    settled before the screen lands. The failure that costs a person their
    time is forgetting to unlock one when the screen does land: the button is
    in the menu, it is visibly greyed, and it looks like the work was never
    done. That happened to 메뉴 편집 -- shipped, deployed, unreachable.

    So: a button is allowed to be disabled only while nothing answers it.
    """
    html = (STATIC / "admin.html").read_text(encoding="utf-8")
    built = set(re.findall(r'<section[^>]*id="page-([a-z-]+)"', html))

    disabled = {
        match.group("page")
        for match in re.finditer(
            r'<button\s+data-page="(?P<page>[a-z-]+)"(?P<attrs>[^>]*)>', html, re.S
        )
        if "disabled" in match.group("attrs")
    }

    assert not (disabled & built), (
        "이 화면은 만들어져 있는데 메뉴에서 '준비 중'으로 잠겨 있습니다: "
        f"{sorted(disabled & built)}"
    )


# ------------------------------------------------- 메뉴 배치가 화면에 닿는가

def _apply_menu_block() -> str:
    source = (STATIC / "admin.js").read_text(encoding="utf-8")
    start = source.index("async function applyMenuArrangement")
    return source[start : source.index("\n    }", start)]


def test_a_hidden_screen_leaves_the_menu_instead_of_moving_to_the_bottom() -> None:
    """HK, 2026-10-06: 자물쇠가 보임, 안보임도 안 바뀌어.

    It was doing something, just not the something it said. `as_groups`
    leaves hidden screens out of the groups, and the client treated anything
    missing from the groups as a screen added since the arrangement was
    saved -- so every 숨김 entry was faithfully put back, at the end of the
    menu. Three of them were sitting there, which also made the order look
    wrong.

    The fix needs both lists: the groups say what to draw, `pages` says what
    the arrangement has heard of. Only a screen in neither is new.
    """
    block = _apply_menu_block()
    assert "payload.pages" in block and "known" in block, (
        "the client must be able to tell hidden apart from new"
    )
    assert "if (!known.has(page)) added.push(button)" in block


def test_opening_a_screen_takes_the_owner_only_mark_off_its_button() -> None:
    """Otherwise 공개 lets colleagues through a door they cannot see.

    `data-requires="super_admin"` is what `applyRoleVisibility` hides a
    button by. Opening a screen in the routes while leaving the attribute on
    would be the most confusing possible half-measure: allowed, invisible.
    """
    block = _apply_menu_block()
    assert "removeAttribute('data-requires')" in block
    assert "setAttribute('data-requires', 'super_admin')" in block, (
        "re-locking a screen has to put the mark back"
    )


def test_the_menu_buttons_survive_being_hidden() -> None:
    """숨김 must not be a one-way door.

    Rebuilding the list from the DOM on each application would mean a button
    removed from the menu is gone from the only place the next application
    looks for it -- so un-hiding would do nothing until a page reload, and
    the person would reasonably read that as the save failing.
    """
    source = (STATIC / "admin.js").read_text(encoding="utf-8")
    assert "const menuButtons = new Map();" in source
    assert "const buttons = new Map(menuButtons);" in _apply_menu_block()


def test_the_lock_mark_goes_away_with_the_rule_that_drew_it() -> None:
    """The 🔒 is a child element, not styling on the attribute.

    So removing `data-requires` from an opened screen leaves the mark
    behind, telling the owner it is 나만 보이는 화면 while the whole company
    can open it. The most believable kind of wrong: everything looks
    consistent except the fact.
    """
    source = (STATIC / "admin.js").read_text(encoding="utf-8")
    assert "button[data-page]:not([data-requires]) .lock" in source


def test_the_count_is_its_own_column_not_part_of_the_chip(script: str) -> None:
    """HK, 2026-10-06: GPU는 가격과 수량을 별개의 컬럼으로 나눠줘.

    "H100 x8" in one cell invited reading the machine price beside it as the
    price of one card, which for p5.48xlarge is eight times wrong.
    """
    assert "function cpGpu(row) {\n      return row.gpu_model || '—';" in script
    assert "function cpCount(" in script
    assert "['수량', 'text-align:right']" in script
    # And the per-card price is a column of its own, beside the machine price.
    assert "['1장·1시간', 'text-align:right']" in script


def test_storage_is_not_given_columns_that_mean_nothing_to_it(script: str) -> None:
    """A column of dashes has to be read before it can be ignored."""
    storage = script.split("storage: [")[1].split("],")[0]
    assert "수량" not in storage and "1장·1시간" not in storage
    assert "function cpRenderHead(" in script


def test_every_price_links_to_the_page_it_can_be_checked_against(
    html: str, script: str
) -> None:
    """HK, 2026-10-06: 원본 정보를 보러 갈 수 있는 링크를 남겨줘."""
    assert "function cpSourceUrl(" in script
    assert "function cpProviderCell(" in script
    # The published page, not whatever the fetcher happened to read: AWS is
    # read from a 202MB CSV, and a citation nobody can open is not one.
    assert "if (found && found.page) return found.page;" in script


def test_the_region_a_price_belongs_to_is_on_screen(script: str) -> None:
    """HK, 2026-10-06: AWS는 리전별로 가격이 다르다고 들었는데."""
    assert "['리전', '']" in script
    assert "text: row.region || '—'," in script


def test_the_screen_can_be_narrowed_to_one_region(html: str, script: str) -> None:
    """HK, 2026-10-07: 키는 벤더&리전. Once a vendor can appear in more than
    one region, the list has to be able to hold one of them still."""
    assert 'id="cp-region"' in html
    assert "if (cpState.region && (row.region || '') !== cpState.region) return false;" in script
    # Only regions that are on screen; a vendor with no region is not offered
    # as an empty option.
    assert "(data.rows || []).filter((r) => r.region).map((r) => r.region)" in script
