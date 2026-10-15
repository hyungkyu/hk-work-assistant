"""권한 체계 (HK, 2026-10-15): 나 (전체) / 민감 정보 취급자 / 일반.

Three roles, a table of which email holds which, and a table of what each role
may reach -- a screen ('page:<id>') or a part of one ('area:<key>'). The owner
(the super administrator) is never limited by the table.

Fails closed everywhere: no database, a failed query, an email with no row and
a resource with no grant all answer "no" -- except that an email with no row is
'general', which is what every signed-in colleague was before this existed.
"""

from __future__ import annotations

import os
from typing import Any, Mapping

ROLES = (("owner", "나 (전체)"), ("sensitive", "민감 정보 취급자"), ("general", "일반"))

# The parts of a screen that can be granted. Adding one here is the whole
# declaration; the route that serves the part asks `allows(..., key)`.
AREAS: dict[str, str] = {
    "area:cv_detail": "인사정보 상세 (경력·학력·논문·스냅샷)",
    "area:cv_edit": "인사정보 편집 · 이름 합치기",
    "area:roster_accounts": "로스터 계정 (사번·이메일·Slack·GitHub ...)",
}


def _database_url() -> str | None:
    return os.environ.get("DATABASE_URL") or None


def role_of(current: Mapping[str, Any] | None, database_url: str | None = None) -> str | None:
    if not current:
        return None
    if current.get("role") == "super_admin":
        return "owner"
    if current.get("role") != "company_user":
        return None
    email = str(current.get("email") or "").strip().lower()
    url = database_url or _database_url()
    if not email or not url:
        return "general"
    import psycopg

    try:
        with psycopg.connect(url) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT role_id FROM admin_user_role WHERE email = %s", (email,))
            row = cursor.fetchone()
    except Exception:
        return "general"
    return row[0] if row else "general"


def grants(role: str, database_url: str | None = None) -> set[str]:
    url = database_url or _database_url()
    if not url or role == "owner":
        return set()
    import psycopg

    try:
        with psycopg.connect(url) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT resource FROM admin_grant WHERE role_id = %s", (role,))
            return {row[0] for row in cursor.fetchall()}
    except Exception:
        return set()


def allows(current: Mapping[str, Any] | None, resource: str, database_url: str | None = None) -> bool:
    role = role_of(current, database_url)
    if role is None:
        return False
    if role == "owner":
        return True
    return resource in grants(role, database_url)


def overview(database_url: str, pages: list[dict[str, Any]]) -> dict[str, Any]:
    """Everything the 권한 screen shows: roles, people, and the grant matrix."""
    import psycopg

    from . import menu

    with psycopg.connect(database_url) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT email, role_id, granted_at, granted_by FROM admin_user_role ORDER BY role_id, email")
        users = [
            {"email": e, "role": r, "granted_at": t.isoformat() if t else None, "granted_by": b}
            for e, r, t, b in cursor.fetchall()
        ]
        cursor.execute("SELECT role_id, resource FROM admin_grant")
        rows = cursor.fetchall()
    matrix: dict[str, list[str]] = {role: [] for role, _ in ROLES if role != "owner"}
    for role, resource in rows:
        matrix.setdefault(role, []).append(resource)
    resources = [
        {
            "resource": f"page:{page['page_id']}",
            "label": page.get("label") or page["page_id"],
            "kind": "메뉴",
            # Only screens whose routes ask can be granted; the others would
            # show a menu entry that opens onto a refusal.
            "grantable": page["page_id"] in menu.TOGGLABLE,
        }
        for page in pages
    ] + [{"resource": key, "label": label, "kind": "영역", "grantable": True} for key, label in AREAS.items()]
    return {
        "roles": [{"role": role, "label": label} for role, label in ROLES],
        "users": users,
        "resources": resources,
        "grants": {role: sorted(items) for role, items in matrix.items()},
    }


def set_user_role(database_url: str, email: str, role: str | None, actor: str) -> None:
    import psycopg

    email = email.strip().lower()
    if "@" not in email:
        raise ValueError("이메일 형식이 아닙니다")
    if role not in (None, "general", "sensitive"):
        raise ValueError("역할은 general / sensitive 중 하나")
    with psycopg.connect(database_url) as connection, connection.cursor() as cursor:
        if role is None:
            cursor.execute("DELETE FROM admin_user_role WHERE email = %s", (email,))
        else:
            cursor.execute(
                """
                INSERT INTO admin_user_role (email, role_id, granted_by) VALUES (%s, %s, %s)
                ON CONFLICT (email) DO UPDATE SET role_id = EXCLUDED.role_id,
                       granted_at = now(), granted_by = EXCLUDED.granted_by
                """,
                (email, role, actor),
            )


def set_grant(database_url: str, role: str, resource: str, on: bool, actor: str) -> None:
    import psycopg

    from . import menu

    if role not in ("general", "sensitive"):
        raise ValueError("역할은 general / sensitive 중 하나")
    if resource.startswith("page:"):
        if resource[5:] not in menu.TOGGLABLE:
            raise ValueError("이 화면은 서버가 권한을 묻지 않아 줄 수 없습니다")
    elif resource not in AREAS:
        raise ValueError(f"모르는 영역: {resource}")
    with psycopg.connect(database_url) as connection, connection.cursor() as cursor:
        if on:
            cursor.execute(
                "INSERT INTO admin_grant (role_id, resource, granted_by) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                (role, resource, actor),
            )
        else:
            cursor.execute("DELETE FROM admin_grant WHERE role_id = %s AND resource = %s", (role, resource))
