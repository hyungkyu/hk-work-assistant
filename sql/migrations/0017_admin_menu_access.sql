-- 0017_admin_menu_access
--
-- Who may open a screen, next to how the menu is arranged.
--
-- HK, 2026-10-02: 공개여부를 어드민에서 수정할 수 있게 해줘.
--
-- 0014 was deliberate that this table arranges and nothing more, because
-- getting an arrangement wrong is visible and undoable while getting access
-- wrong is neither. This column is the exception HK asked for, so it is
-- built to fail closed:
--
-- * the default is super_admin, so a screen that nobody has decided about
--   is shut, including every screen added after this migration;
-- * the check constraint means a typo cannot widen access -- an unknown
--   value is rejected by the database rather than read as "not locked";
-- * a row only has an effect for the screens whose routes consult it
--   (menu.TOGGLABLE). For every other screen the server keeps asking for
--   super_admin whatever this column says, so a mistaken row grants
--   nothing. The editor marks those entries as fixed rather than offering
--   a switch that would not be honoured.
--
-- 공개 here means a signed-in company_user, not the open internet. There is
-- no role below company_user, and no route serves an anonymous caller.

ALTER TABLE admin_menu
    ADD COLUMN IF NOT EXISTS requires text NOT NULL DEFAULT 'super_admin';

ALTER TABLE admin_menu DROP CONSTRAINT IF EXISTS admin_menu_requires_check;
ALTER TABLE admin_menu
    ADD CONSTRAINT admin_menu_requires_check
    CHECK (requires IN ('super_admin', 'company_user'));

COMMENT ON COLUMN admin_menu.requires IS
    'Lowest role that may open this screen. Defaults to super_admin, so a '
    'screen with no row, or a row nobody has decided about, stays shut.';
