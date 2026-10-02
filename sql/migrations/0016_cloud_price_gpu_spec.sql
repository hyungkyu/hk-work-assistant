-- 0016_cloud_price_gpu_spec
--
-- Which GPU, how many, and how much memory each one has.
--
-- These were already arriving inside `cloud_price.spec`, but as whatever each
-- provider happened to call them: AWS says `GPU` and means a count, Naver says
-- `GPU` and means "1개 x 48GB", Kakao says `accelerator` and means "A100 x1".
-- Three columns with the same name holding three different facts is a column
-- nobody can compare across providers, and comparing across providers is the
-- entire purpose of the screen.
--
-- So the three facts get three columns, in one unit each:
--
--   gpu_model      the chip, as the industry names it: H100, A100, B300, L40S.
--   gpu_count      how many of them the machine has.
--   gpu_memory_gb  how much memory ONE of them has, in GB.
--
-- `gpu_memory_gb` is per GPU rather than per machine because that is the
-- number a model has to fit inside. AWS publishes the machine total, so its
-- adapter divides; everyone else publishes it per GPU already.
--
-- All three are nullable, and that is the point rather than an oversight.
-- Nebius and Kakao publish the model but not the memory, and a model name is
-- not enough to supply it: H100 is 80GB everywhere, but A100 ships as 40GB and
-- 80GB and V100 as 16GB and 32GB, so filling the blank from a lookup table
-- would put a specific wrong number on a comparison screen. Null renders as
-- "—", which is what we actually know.

ALTER TABLE cloud_price ADD COLUMN IF NOT EXISTS gpu_model     text;
ALTER TABLE cloud_price ADD COLUMN IF NOT EXISTS gpu_count     integer;
ALTER TABLE cloud_price ADD COLUMN IF NOT EXISTS gpu_memory_gb numeric(10, 2);

-- The screen's main question is "who sells an H100 cheapest", so the model is
-- what it groups and filters by.
CREATE INDEX IF NOT EXISTS cloud_price_gpu_model_idx
    ON cloud_price (gpu_model) WHERE gpu_model IS NOT NULL;

COMMENT ON COLUMN cloud_price.gpu_memory_gb IS
    'Memory of ONE GPU, in GB. AWS publishes the machine total; its adapter divides by the count so every provider means the same thing here.';
COMMENT ON COLUMN cloud_price.gpu_model IS
    'The chip as the industry names it (H100, A100, B300). Null where the provider does not say, never guessed from the instance name.';
