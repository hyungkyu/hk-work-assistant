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
