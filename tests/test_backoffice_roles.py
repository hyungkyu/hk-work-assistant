"""What the owner sees that nobody else does, and how the page says so.

There is no browser here, so these read the markup and the script the way the
other backoffice tests do: they pin the structure a regression would break.
The behaviour under it — that a staff session really is refused — is pinned
against the routes in `test_staff_login.py`.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "src" / "rlwrld_worklog" / "static"


@pytest.fixture(scope="module")
def html() -> str:
    return (STATIC / "admin.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def script() -> str:
    return (STATIC / "admin.js").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def css() -> str:
    return (STATIC / "admin.css").read_text(encoding="utf-8")


def nav_entries(html: str) -> list[tuple[str, bool]]:
    nav = html.split("</nav>")[0]
    return [
        (m.group(1), 'data-requires="super_admin"' in m.group(0))
        for m in re.finditer(r'<button data-page="([a-z]+)"[^>]*>', nav)
    ]


def test_every_menu_entry_declares_whether_it_is_owner_only(html: str) -> None:
    """A new screen that forgets to declare itself is visible to everybody.

    So the rule is that the declaration is not optional: today every entry but
    로드맵 is the owner's, and 로드맵 is the first screen meant for the company.
    """
    entries = dict(nav_entries(html))
    assert entries, "the menu has no entries"
    assert entries.pop("roadmap") is False
    assert all(entries.values()), sorted(page for page, owned in entries.items() if not owned)


def test_the_padlock_is_drawn_for_the_owner_and_the_entry_removed_for_everyone_else(
    script: str,
) -> None:
    body = re.search(r"function applyRoleVisibility\(\)\s*\{(.*?)\n    \}", script, re.S)
    assert body, "applyRoleVisibility is missing"
    source = body.group(1)
    assert '[data-requires="super_admin"]' in source
    assert "classList.toggle('hidden', !owner)" in source
    assert "🔒" in source


def test_the_padlock_is_a_label_and_the_script_says_so(script: str) -> None:
    """If this comment goes, the next reader will take the icon for the lock."""
    assert "The padlock is a label, not a lock" in script
    assert "require_super_admin_session" in script


def test_a_company_session_is_let_in_rather_than_bounced_to_the_login(script: str) -> None:
    """The second door is pointless if the app still gates on `authorized`."""
    assert "if (!session.authenticated) return showAuth(session);" in script
    assert len(re.findall(r"if \(!session\.authorized\) return showAuth", script)) == 0
    # Settings are the owner's, and asking for them as staff would 403 on boot.
    assert len(re.findall(r"if \(isOwner\(\)\) await loadSettings\(\);", script)) == 2


def test_a_page_the_session_may_not_open_is_not_a_page_it_lands_on(script: str) -> None:
    assert "const allowed = visiblePages();" in script
    assert "if (!allowed.includes(page)) page = allowed.length ? allowed[0] : 'restricted';" in script


def test_a_session_with_nothing_to_open_gets_a_notice_not_an_empty_shell(
    html: str, script: str
) -> None:
    assert 'id="restricted-page"' in html
    assert "$('restricted-page')" in script
    assert 'id="restricted-logout"' in html
    # And it is deliberately not named `page-…`: it has no menu entry, and the
    # menu/section invariant would read it as an unreachable screen.
    assert 'id="page-restricted"' not in html


def test_the_staff_password_field_lives_on_the_security_screen(html: str) -> None:
    security = html.split('id="page-security"')[1].split("</section>")[0]
    assert 'id="staff-password"' in security
    assert 'id="staff-password-save"' in security
    assert 'id="staff-password-status"' in security
    assert 'type="password"' in security


def test_the_staff_password_is_written_through_its_own_route(script: str) -> None:
    assert "'/api/v1/admin/staff-password'" in script
    assert "method: 'PUT'" in script


def test_emptying_the_field_is_how_the_door_is_closed(html: str, script: str) -> None:
    """One control, two meanings, and the screen says which is which."""
    assert "비우면" in html
    assert "해제했습니다" in script


def test_the_padlock_has_a_style(css: str) -> None:
    assert ".lock {" in css


def test_signing_out_forgets_the_role(script: str) -> None:
    """A stale role would leave the previous session's screens on the page."""
    body = re.search(r"async function signOut\(\)\s*\{(.*?)\n    \}", script, re.S)
    assert body, "signOut is missing"
    assert "state.role = null" in body.group(1)
    assert "applyRoleVisibility()" in body.group(1)
