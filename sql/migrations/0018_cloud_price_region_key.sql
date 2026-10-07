-- 0018_cloud_price_region_key
--
-- A price belongs to a vendor AND a region, and the key says so.
--
-- HK, 2026-10-07: vendor & region 두개의 필드를 두자. 키는 벤더&리전, 다른
-- 곳은 리전이 없을거야.
--
-- Until now the key was (snapshot, provider, category, sku), which cannot
-- hold `p5.48xlarge` in Seoul and `p5.48xlarge` in Virginia at the same time
-- -- the second insert collides with the first. That is why only one AWS
-- region was ever collected: not a decision anyone made, a shape the table
-- imposed. AWS charges differently by region, so one region is one quote out
-- of many and the screen had no way to say which.
--
-- **NULL is a real answer here, and it is the trap.** Four of the five
-- vendors publish no region at all -- Nebius, Kakao and VESSL quote one rate
-- card, and Naver quotes 한국. For them `region` is NULL, meaning "this
-- vendor does not divide its prices by region", which is different from "we
-- have not looked". Keeping NULL rather than writing '' is what preserves
-- that distinction on screen.
--
-- But Postgres treats NULLs inside a UNIQUE constraint as distinct from each
-- other, so a plain `UNIQUE (snapshot_id, provider, region, category, sku)`
-- would let a Kakao row be inserted twice over and silently double every
-- Kakao price in a comparison. The unique INDEX below folds NULL to '' for
-- the purpose of the key only; the column still reads NULL.

-- --------------------------------------------------------------- the prices

ALTER TABLE cloud_price
    DROP CONSTRAINT IF EXISTS cloud_price_snapshot_id_provider_category_sku_key;

CREATE UNIQUE INDEX IF NOT EXISTS cloud_price_identity_idx
    ON cloud_price (snapshot_id, provider, COALESCE(region, ''), category, sku);

-- --------------------------------------------------------- what each fetch did
--
-- A run is per vendor AND region too, for a reason beyond symmetry: AWS
-- publishes a version per region, and that version is what lets a refresh skip
-- a 202MB download. One run row per vendor could only remember one of them, so
-- with two regions collected the second would re-download every time.

ALTER TABLE cloud_price_run ADD COLUMN IF NOT EXISTS region text;

ALTER TABLE cloud_price_run
    DROP CONSTRAINT IF EXISTS cloud_price_run_snapshot_id_provider_key;

CREATE UNIQUE INDEX IF NOT EXISTS cloud_price_run_identity_idx
    ON cloud_price_run (snapshot_id, provider, COALESCE(region, ''));

-- --------------------------------------------------------------- what moved

ALTER TABLE cloud_price_change ADD COLUMN IF NOT EXISTS region text;

COMMENT ON COLUMN cloud_price.region IS
    'The region this price is for, or NULL where the vendor does not divide its prices by region. NULL is an answer, not a gap.';
COMMENT ON INDEX cloud_price_identity_idx IS
    'Identity is vendor + region + sku. COALESCE because Postgres counts NULLs in a UNIQUE as distinct, which would let a region-less vendor be stored twice.';
