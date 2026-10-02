"""What exists comes from the markup; how it is arranged comes from the database.

HK, 2026-10-01: LEFT 메뉴를 편집할 수 있게 해주면 좋겠어.

The split is the design, and it exists to stop one failure repeating. Four
sessions share this repository and every backoffice screen lives in
`admin.html`. On 2026-09-30 two of them edited one file blind to each other
and one reverted the other's work; the menu is the likeliest place for that
again, because every new screen wants a line in it and the menu test compares
the whole list by equality.

`arrange` is pure so these rules can be checked without a database or a
browser, which is also what makes them worth writing down: each one is a way
the menu could quietly lose a screen.
"""

from __future__ import annotations

import os

import pytest
from fastapi import HTTPException

from rlwrld_worklog import admin_web, menu, menu_web
from rlwrld_worklog.admin_web import SESSION_COOKIE


class FakeRequest:
    def __init__(self, *, cookies=None, headers=None) -> None:
        self.cookies = cookies or {}
        self.headers: dict[str, str] = headers or {}


@pytest.fixture()
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_CONFIG_ROOT", str(tmp_path / "config"))
    yield tmp_path


@pytest.fixture()
def owner(config):
    token, csrf = admin_web.store().create_session(
        subject="owner",
        email="hyungkyu.ryu@rlwrld.ai",
        role="super_admin",
        auth_method="google",
    )
    return FakeRequest(cookies={SESSION_COOKIE: token}), csrf


HTML = """
<nav id="nav">
  <div class="nav-group">
    <span class="nav-label">업무</span>
    <button data-page="work" data-requires="super_admin">업무 현황</button>
    <button data-page="roadmap">로드맵</button>
  </div>
  <div class="nav-group">
    <span class="nav-label">정보</span>
    <button data-page="gpu" data-requires="super_admin" disabled>GPU 가격</button>
  </div>
</nav>
"""


def test_the_markup_is_what_says_which_screens_exist() -> None:
    pages = menu.declared_pages(HTML)
    assert [page["page_id"] for page in pages] == ["work", "roadmap", "gpu"]
    assert pages[0]["group_label"] == "업무"
    assert pages[2]["unbuilt"] is True, "a screen still being built is marked, not dropped"
    assert pages[1]["owner_only"] is False


def test_the_real_admin_page_parses() -> None:
    """The fixture above is a reduction; this is the file that ships.

    A parser that works on a tidy example and not on the real markup would
    make the menu silently empty, which looks exactly like a screen that
    failed to load.
    """
    pages = menu.declared_pages()
    ids = [page["page_id"] for page in pages]
    assert "work" in ids and "menu" in ids and "gpu" in ids
    assert len(ids) == len(set(ids)), "no page is declared twice"
    assert all(page["label"] for page in pages), "every entry has a name"


def test_a_screen_nobody_has_placed_appears_at_the_end(tmp_path) -> None:
    """The rule that lets a session add a screen without touching the menu.

    A page with no stored row has to show up somewhere. At the end, in the
    markup's own order, so it is visible as new rather than slotted silently
    into the middle of an arrangement somebody made on purpose.
    """
    declared = menu.declared_pages(HTML)
    arranged = menu.arrange(
        declared,
        [
            {"page_id": "roadmap", "position": 0},
            {"page_id": "work", "position": 1},
        ],
    )
    assert [item["page_id"] for item in arranged] == ["roadmap", "work", "gpu"]
    assert arranged[-1]["arranged"] is False


def test_a_row_for_a_screen_that_no_longer_exists_is_dropped() -> None:
    """Deleting a screen must not leave an entry that opens nothing."""
    arranged = menu.arrange(
        menu.declared_pages(HTML),
        [{"page_id": "deleted-screen", "position": 0}, {"page_id": "work", "position": 1}],
    )
    assert "deleted-screen" not in [item["page_id"] for item in arranged]


def test_an_empty_name_falls_back_to_the_markup() -> None:
    """A button with no text is indistinguishable from a broken page."""
    arranged = menu.arrange(
        menu.declared_pages(HTML),
        [{"page_id": "work", "label": "   ", "group_label": "", "position": 0}],
    )
    work = next(item for item in arranged if item["page_id"] == "work")
    assert work["label"] == "업무 현황"
    assert work["group_label"] == "업무"


def test_groups_are_consecutive_runs_not_collected_buckets() -> None:
    """So moving one entry out of a group is a move the person can see.

    Collecting by name would make an entry dragged elsewhere jump back into
    its old group, which reads as the screen refusing the edit.
    """
    arranged = menu.arrange(
        menu.declared_pages(HTML),
        [
            {"page_id": "work", "group_label": "업무", "position": 0},
            {"page_id": "gpu", "group_label": "정보", "position": 1},
            {"page_id": "roadmap", "group_label": "업무", "position": 2},
        ],
    )
    groups = menu.as_groups(arranged)
    assert [group["label"] for group in groups] == ["업무", "정보", "업무"]


def test_a_hidden_entry_leaves_the_menu_but_not_the_editor() -> None:
    """Hiding is arrangement, not deletion -- it has to be undoable here."""
    arranged = menu.arrange(
        menu.declared_pages(HTML),
        [{"page_id": "work", "hidden": True, "position": 0}],
    )
    assert any(item["page_id"] == "work" for item in arranged)
    assert "work" not in [
        entry["page_id"] for group in menu.as_groups(arranged) for entry in group["entries"]
    ]


# ------------------------------------------------------------------ routes


def test_anonymous_callers_are_refused(config) -> None:
    with pytest.raises(HTTPException) as error:
        menu_web.menu_route(FakeRequest())
    assert error.value.status_code == 401


def test_saving_is_refused_without_csrf(config, owner) -> None:
    request, _ = owner
    with pytest.raises(HTTPException) as error:
        menu_web.save_menu_route(
            request, menu_web.SaveMenu(entries=[menu_web.MenuEntry(page_id="work")])
        )
    assert error.value.status_code == 403


def test_reading_the_menu_falls_back_to_the_markup(config, owner, monkeypatch) -> None:
    """A backoffice that cannot draw its own navigation is unusable.

    Including -- especially -- when the reason is a database problem, which is
    the thing somebody would open the backoffice to look into. So a failed
    read degrades to the order in the markup and says which one it gave.
    """
    request, _ = owner
    monkeypatch.delenv("DATABASE_URL", raising=False)

    found = menu_web.menu_route(request)

    assert found["source"] == "markup"
    assert found["pages"], "the menu is still drawable"
    assert "DATABASE_URL" in found["reason"]


def test_a_broken_query_degrades_rather_than_failing(config, owner, monkeypatch) -> None:
    request, _ = owner
    monkeypatch.setenv("DATABASE_URL", "postgresql://example/none")
    monkeypatch.setattr(
        menu, "read", lambda url: (_ for _ in ()).throw(RuntimeError("connection refused"))
    )

    found = menu_web.menu_route(request)

    assert found["source"] == "markup"
    assert "connection refused" in found["reason"]
    assert found["pages"]


def test_an_arrangement_naming_an_unknown_screen_is_refused(
    config, owner, monkeypatch
) -> None:
    """A row that points at nothing can only ever become a dead menu entry."""
    request, csrf = owner
    request.headers["x-csrf-token"] = csrf
    monkeypatch.setenv("DATABASE_URL", "postgresql://example/none")

    with pytest.raises(HTTPException) as error:
        menu_web.save_menu_route(
            request,
            menu_web.SaveMenu(entries=[menu_web.MenuEntry(page_id="not-a-screen")]),
        )
    assert error.value.status_code == 400
    assert "not-a-screen" in error.value.detail


REQUIRES_DATABASE = pytest.mark.skipif(
    not os.environ.get("WORKLOG_TEST_DATABASE_URL"),
    reason="set WORKLOG_TEST_DATABASE_URL to a throwaway database",
)


@REQUIRES_DATABASE
def test_saving_replaces_the_arrangement_rather_than_merging() -> None:
    """The screen sends the whole menu as the person sees it.

    A merge would make "I moved this one up" depend on rows they never saw,
    and an entry removed from the list would quietly keep its old position.
    """
    from pathlib import Path

    from rlwrld_worklog.ledger.load import apply_migrations

    url = os.environ["WORKLOG_TEST_DATABASE_URL"]
    apply_migrations(
        database_url=url,
        migrations_dir=Path(__file__).resolve().parents[1] / "sql" / "migrations",
        dry_run=False,
    )

    menu.save(url, [{"page_id": "work"}, {"page_id": "roadmap"}], actor="hk")
    menu.save(url, [{"page_id": "roadmap", "label": "로드맵 보기"}], actor="hk")

    found = menu.read(url)
    arranged = [page for page in found["pages"] if page["arranged"]]
    assert [page["page_id"] for page in arranged] == ["roadmap"]
    assert arranged[0]["label"] == "로드맵 보기"
    # Everything else is still in the menu, at the end, from the markup.
    assert found["unarranged"] == len(found["pages"]) - 1


# ------------------------------------------------------------------- 공개 여부

"""HK, 2026-10-02: 공개여부를 어드민에서 수정할 수 있게 해줘.

0014 deliberately kept access out of this table, on the grounds that an
arrangement mistake is visible and undoable while an access mistake is
neither. HK overruled that, so the switch is built to fail closed in every
direction a mistake can come from, and each of those directions is a test
below. The one that matters most is the last: a row saying 공개 for a screen
whose routes do not consult it must not be stored, because the next person to
read that row would believe it.
"""


def test_a_screen_nobody_decided_about_is_shut() -> None:
    arranged = menu.arrange(menu.declared_pages(HTML), [])
    assert all(item["requires"] == menu.CLOSED for item in arranged)


def test_an_unrecognised_value_reads_as_shut() -> None:
    """A typo, an old spelling, a half-written migration -- all shut.

    The permissive value has to be spelled exactly; everything else is the
    safe side. This is why the column has a CHECK constraint as well: two
    independent places that can only err towards closed.
    """
    arranged = menu.arrange(
        menu.declared_pages(HTML),
        [{"page_id": "work", "position": 0, "requires": "everyone"}],
    )
    work = next(item for item in arranged if item["page_id"] == "work")
    assert work["requires"] == menu.CLOSED


def test_whether_the_switch_does_anything_is_a_fact_about_the_routes() -> None:
    """The editor shows a switch only where the server asks the question."""
    pages = {page["page_id"]: page for page in menu.declared_pages()}
    assert pages["gpu"]["togglable"] is True
    assert pages["security"]["togglable"] is False
    assert pages["audit"]["togglable"] is False, "감사 기록 is not openable"


def test_access_fails_closed_without_a_database() -> None:
    assert menu.access_for("gpu", None) == menu.CLOSED
    assert menu.access_for("security", None) == menu.CLOSED


def test_a_screen_that_is_open_today_stays_open_when_unarranged() -> None:
    """Wiring a screen to the rule must not narrow it on the way in.

    북마크 reading has been open to any signed-in reader. If the new rule
    defaulted it shut, the deploy that added the switch would have taken the
    screen away from the people already using it -- a regression introduced
    by a feature meant to grant access, which is the worst way to find out.
    """
    assert menu.access_for("bookmarks", None) == menu.OPEN


REQUIRES_DATABASE_ACCESS = pytest.mark.skipif(
    not os.environ.get("WORKLOG_TEST_DATABASE_URL"),
    reason="set WORKLOG_TEST_DATABASE_URL to a throwaway database",
)


def _migrated() -> str:
    from pathlib import Path

    from rlwrld_worklog.ledger.load import apply_migrations

    url = os.environ["WORKLOG_TEST_DATABASE_URL"]
    apply_migrations(
        database_url=url,
        migrations_dir=Path(__file__).resolve().parents[1] / "sql" / "migrations",
        dry_run=False,
    )
    return url


@REQUIRES_DATABASE_ACCESS
def test_opening_a_screen_is_read_back_by_the_guard() -> None:
    url = _migrated()
    menu.save(url, [{"page_id": "gpu", "requires": menu.OPEN}], actor="hk")
    assert menu.access_for("gpu", url) == menu.OPEN

    menu.save(url, [{"page_id": "gpu", "requires": menu.CLOSED}], actor="hk")
    assert menu.access_for("gpu", url) == menu.CLOSED


@REQUIRES_DATABASE_ACCESS
def test_a_screen_the_routes_ignore_cannot_be_stored_open() -> None:
    """The row would be a lie, and lies in this table are read as permission."""
    url = _migrated()
    menu.save(url, [{"page_id": "security", "requires": menu.OPEN}], actor="hk")

    found = menu.read(url)
    security = next(p for p in found["pages"] if p["page_id"] == "security")
    assert security["requires"] == menu.CLOSED
    assert menu.access_for("security", url) == menu.CLOSED


@REQUIRES_DATABASE_ACCESS
def test_the_database_refuses_a_value_the_code_never_writes() -> None:
    """Belt and braces: if a row is ever written by hand, it still cannot open."""
    import psycopg

    url = _migrated()
    with psycopg.connect(url) as connection:
        with connection.cursor() as cursor:
            with pytest.raises(psycopg.errors.CheckViolation):
                cursor.execute(
                    "INSERT INTO admin_menu (page_id, requires) VALUES (%s, %s)",
                    ("gpu", "everyone"),
                )


@REQUIRES_DATABASE_ACCESS
def test_the_guard_lets_a_colleague_in_only_while_the_screen_is_open(
    config, monkeypatch
) -> None:
    """The switch has to reach the request, not just the button.

    Hiding a menu entry only stops people who navigate by clicking. This is
    the test that says the server asks the same question the editor answers.
    """
    url = _migrated()
    monkeypatch.setenv("DATABASE_URL", url)
    token, _ = admin_web.store().create_session(
        subject="colleague",
        email="someone@rlwrld.ai",
        role="company_user",
        auth_method="google",
    )
    colleague = FakeRequest(cookies={SESSION_COOKIE: token})
    guard = admin_web.require_page_access("gpu")

    menu.save(url, [{"page_id": "gpu", "requires": menu.CLOSED}], actor="hk")
    with pytest.raises(HTTPException) as error:
        guard(colleague)
    assert error.value.status_code == 403

    menu.save(url, [{"page_id": "gpu", "requires": menu.OPEN}], actor="hk")
    assert guard(colleague)["role"] == "company_user"


@REQUIRES_DATABASE_ACCESS
def test_opening_a_screen_does_not_let_a_colleague_change_it(
    config, monkeypatch
) -> None:
    """공개 is about reading. Writing stays with the owner.

    Worth its own test because the two live side by side in the same module,
    and the natural mistake when wiring the read routes is to catch a write
    route in the same sweep.
    """
    from rlwrld_worklog import cloud_pricing_web

    url = _migrated()
    monkeypatch.setenv("DATABASE_URL", url)
    menu.save(url, [{"page_id": "gpu", "requires": menu.OPEN}], actor="hk")

    token, csrf = admin_web.store().create_session(
        subject="colleague",
        email="someone@rlwrld.ai",
        role="company_user",
        auth_method="google",
    )
    colleague = FakeRequest(
        cookies={SESSION_COOKIE: token}, headers={"x-csrf-token": csrf}
    )

    # The screen is open; the refresh button behind it is not.
    with pytest.raises(HTTPException) as error:
        cloud_pricing_web.refresh_route(colleague)
    assert error.value.status_code == 403

    # And the read it *is* allowed does not raise on the way in.
    admin_web.require_page_access("gpu")(colleague)
