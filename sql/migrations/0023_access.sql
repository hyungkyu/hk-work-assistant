-- 0023_access
--
-- 권한 체계: who may open which screen, and which part of a screen.
--
-- HK, 2026-10-15: 권한 체계를 나누고 싶어. 나 (전체) / 일반 (제한된 메뉴) /
-- 인사 (일반 + 민감 정보 취급자) ... 메뉴/기능/컨텐츠 영역을 각각 권한이 있는
-- 사람에게만 부여할 건데, 그러기 위해서는 권한 테이블이 필요해.
--
-- * admin_role: the three roles. 'owner' is the super administrator and is
--   never limited by the grant table -- it is listed so the screen can name it.
-- * admin_user_role: which signed-in email holds which role. Anybody signed in
--   from the company without a row is 'general'.
-- * admin_grant: role x resource. A resource is 'page:<page_id>' (a menu
--   screen) or 'area:<key>' (a part of a screen: 인사정보 상세, 로스터 계정 ...).
--   No row means no access: a grant nobody made opens nothing.
--
-- 일반 starts with exactly the screens that are 공개 today, and 민감 정보
-- 취급자 starts with those plus the personnel areas, so the first deploy changes
-- nobody's view.

CREATE TABLE IF NOT EXISTS admin_role (
    role_id text PRIMARY KEY CHECK (role_id IN ('owner', 'sensitive', 'general')),
    label   text NOT NULL,
    rank    int  NOT NULL
);

INSERT INTO admin_role (role_id, label, rank) VALUES
    ('owner', '나 (전체)', 0),
    ('sensitive', '민감 정보 취급자', 1),
    ('general', '일반', 2)
ON CONFLICT (role_id) DO NOTHING;

CREATE TABLE IF NOT EXISTS admin_user_role (
    email      text PRIMARY KEY CHECK (email = lower(email) AND email LIKE '%@%'),
    role_id    text NOT NULL REFERENCES admin_role (role_id) CHECK (role_id <> 'owner'),
    granted_at timestamptz NOT NULL DEFAULT now(),
    granted_by text
);

CREATE TABLE IF NOT EXISTS admin_grant (
    role_id    text NOT NULL REFERENCES admin_role (role_id) CHECK (role_id <> 'owner'),
    resource   text NOT NULL CHECK (resource ~ '^(page|area):[a-z0-9_]+$'),
    granted_at timestamptz NOT NULL DEFAULT now(),
    granted_by text,
    PRIMARY KEY (role_id, resource)
);

INSERT INTO admin_grant (role_id, resource, granted_by)
SELECT role_id, 'page:' || page_id, 'migration 0023'
  FROM admin_menu, (VALUES ('general'), ('sensitive')) AS roles (role_id)
 WHERE requires = 'company_user'
ON CONFLICT DO NOTHING;

INSERT INTO admin_grant (role_id, resource, granted_by) VALUES
    ('general', 'page:bookmarks', 'migration 0023'),
    ('sensitive', 'page:bookmarks', 'migration 0023'),
    ('sensitive', 'area:cv_detail', 'migration 0023'),
    ('sensitive', 'area:roster_accounts', 'migration 0023')
ON CONFLICT DO NOTHING;
