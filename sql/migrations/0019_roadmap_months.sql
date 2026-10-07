-- 0019_roadmap_months
--
-- The roadmap was read from the snapshot table at the top of the Notion page,
-- whose columns are relative: 이번달, 다음달, 장기. Nobody kept that table
-- current after 2026-09-02. The teams write dated sections underneath instead
-- ("20260930 HW", "20261001 Infra"), newest on top, each with its own 이번달
-- and 다음달 -- so "이번달" in one section is September and in the next is
-- October.
--
-- A relative column cannot accumulate. So `horizon` becomes absolute: a month
-- as YYYY-MM, or `later` (the newest 장기계획), or `undated` (the source says
-- the schedule is not decided). The screen grows a column when a month appears.
--
-- The rows already here came from the 202609 snapshot table, which is what
-- fixes their months: 이번달 there was 2026-09 and 다음달 2026-10. The next
-- refresh re-files every row it can from the dated sections anyway.

ALTER TABLE roadmap_item DROP CONSTRAINT IF EXISTS roadmap_item_horizon_check;

UPDATE roadmap_item SET horizon = CASE horizon
    WHEN 'now' THEN '2026-09'
    WHEN 'next' THEN '2026-10'
    WHEN 'soon' THEN 'later'
    WHEN 'someday' THEN 'undated'
    ELSE horizon
END;

ALTER TABLE roadmap_item ADD CONSTRAINT roadmap_item_horizon_check
    CHECK (horizon ~ '^[0-9]{4}-(0[1-9]|1[0-2])$' OR horizon IN ('later', 'undated'));

-- The page asks teams to strike a line through what is finished. That is the
-- only completion signal the source has, and it is kept rather than read past.
ALTER TABLE roadmap_item
    ADD COLUMN IF NOT EXISTS done boolean NOT NULL DEFAULT false;

-- A strike added or removed is a change worth a line in the history, and it is
-- not a change of words.
ALTER TABLE roadmap_change DROP CONSTRAINT IF EXISTS roadmap_change_type_check;
ALTER TABLE roadmap_change ADD CONSTRAINT roadmap_change_type_check
    CHECK (type IN ('added', 'changed', 'removed', 'completed', 'reopened'));

COMMENT ON COLUMN roadmap_item.horizon IS
    'YYYY-MM, or later (newest long-term plan), or undated (source says not scheduled).';
COMMENT ON COLUMN roadmap_item.done IS
    'Struck through on the source page.';
