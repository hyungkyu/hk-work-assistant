-- 0023_access
--
-- 권한 체계: who may open which screen, and which part of a screen.
--
-- HK, 2026-10-15: 권한 체계를 나누고 싶어. 나 (전체) / 일반 (제한된 메뉴) /
-- 인사 (일반 + 민감 정보 취급자) ... 메뉴/기능/컨텐츠 영역을 각각 권한이 있는
-- 사람에게만 부여할 건데, 그러기 위해서는 권한 테이블이 필요해.
--
-- * The roles are declared in code (access.ROLES). 'owner' is the super
--   administrator and is never limited by these tables.
-- * admin_user_role: which signed-in email holds which role. Anybody signed in
--   from the company without a row is 'general'.
-- * admin_grant: role x resource. A resource is 'page:<page_id>' or
--   'area:<key>'. No row means no access. 일반's screens are the 공개 switch
--   in the menu editor, so they are not rows here: one switch, one answer.
-- * admin_access_meta: records that the first defaults were written, so
--   unchecking everything later is not undone by writing them again.
--
-- No rows are inserted here; the defaults are written by access.py the first
-- time the tables are read.

CREATE TABLE IF NOT EXISTS admin_user_role (
    email      text PRIMARY KEY CHECK (email = lower(email) AND email LIKE '%@%'),
    role_id    text NOT NULL CHECK (role_id IN ('sensitive', 'general')),
    granted_at timestamptz NOT NULL DEFAULT now(),
    granted_by text
);

CREATE TABLE IF NOT EXISTS admin_grant (
    role_id    text NOT NULL CHECK (role_id IN ('sensitive', 'general')),
    resource   text NOT NULL CHECK (resource ~ '^(page|area):[a-z0-9_]+$'),
    granted_at timestamptz NOT NULL DEFAULT now(),
    granted_by text,
    PRIMARY KEY (role_id, resource)
);

CREATE TABLE IF NOT EXISTS admin_access_meta (
    key        text PRIMARY KEY,
    written_at timestamptz NOT NULL DEFAULT now()
);
