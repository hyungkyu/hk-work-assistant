-- 0013_roadmap_translation
--
-- A refresh reads Korean from Notion. It cannot translate, so after a row's
-- Korean changes the English and Japanese beside it are stale -- and a screen
-- with a language toggle has no way to say so. Then the English reads as
-- current and is quietly wrong, which is worse than being absent.
--
-- `translated_hash` is the Korean hash the other two languages were made from.
-- Equal to `hash` means the translation is current; different means behind;
-- NULL means never translated, which is what a row born in a refresh is.

ALTER TABLE roadmap_item
    ADD COLUMN IF NOT EXISTS translated_hash text;

-- Everything present before this migration came from the first import, where
-- all three languages were written together.
UPDATE roadmap_item SET translated_hash = hash WHERE translated_hash IS NULL;

COMMENT ON COLUMN roadmap_item.translated_hash IS
    'The Korean hash the en/ja text was translated from; NULL means never translated.';
