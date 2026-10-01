-- 0014_admin_menu
--
-- Where the left menu is arranged, so that it is not arranged in markup.
--
-- HK, 2026-10-01: LEFT 메뉴를 편집할 수 있게 해주면 좋겠어.
--
-- The reason it is worth a table rather than an edit to the HTML: four
-- sessions share this repository and every backoffice screen lives in one
-- file. On 2026-09-30 two of them edited one file without either being able
-- to see the other, and one reverted the other's work; the menu is the part
-- of admin.html most likely to repeat that, because a session adding any
-- screen wants a line in it, and the test compares the whole list by
-- equality. Two sessions, two rewrites of the same assertion, and whichever
-- lands second turns main red.
--
-- With the arrangement here, a session adding a screen writes its own section
-- and nothing else. Nobody edits the menu.
--
-- What this table does NOT decide: which screens exist. That stays in
-- admin.html, which is the only place that can be true about it -- a row here
-- naming a page with no section would be a menu entry leading nowhere, and a
-- new section with no row would vanish. So the page list comes from the
-- markup, and this table only orders, renames, groups and hides. A screen
-- somebody adds tomorrow appears at the end of the menu on its own.

CREATE TABLE IF NOT EXISTS admin_menu (
    -- Matches `data-page` in admin.html. Not a foreign key to anything,
    -- because the thing it refers to is markup; an orphan row is possible and
    -- is simply ignored when the menu is built, which is the behaviour that
    -- keeps a stale row from hiding a working screen.
    page_id text PRIMARY KEY,
    -- Null means "use the label in the markup". Storing a copy of every label
    -- would make this table the place to fix a typo in Korean, and then two
    -- places would disagree about what a screen is called.
    label text,
    group_label text,
    position integer NOT NULL DEFAULT 0,
    hidden boolean NOT NULL DEFAULT false,
    updated_at timestamptz NOT NULL DEFAULT now(),
    updated_by text
);

COMMENT ON TABLE admin_menu IS
    'Order, grouping, naming and visibility of the backoffice left menu. '
    'Which screens exist is decided by admin.html; this table only arranges '
    'them, so a session that adds a screen never edits the menu.';

-- Reading the menu is one query on every page load, in menu order.
CREATE INDEX IF NOT EXISTS admin_menu_order_idx ON admin_menu (position, page_id);
