-- 0015_cloud_pricing
--
-- What one GPU-hour and one GiB-month cost at each cloud we might buy from,
-- with the exchange rate that was true when the number was taken.
--
-- Three decisions shape these tables.
--
-- **The rate card and the reading of it are different facts.** A price list
-- that has not changed in three weeks is still a current price list, and
-- saying so is not the same as saying nobody looked. So a snapshot carries
-- both `taken_at` (when this set of numbers first appeared) and
-- `refreshed_at` (when we last confirmed it is still what the provider
-- publishes). A refresh that finds nothing changed moves `refreshed_at` and
-- the exchange rate and makes no new snapshot -- otherwise the history would
-- fill with identical rows and stop being a history of price changes.
--
-- **The exchange rate belongs to the reading, not to the price.** The provider
-- publishes $2.98/hour; it does not publish ₩4,180/hour. The converted number
-- is ours, it is only as good as the rate beside it, and a converted price
-- with no rate and no timestamp next to it is a number nobody can check. So
-- the rate lives on the snapshot, and the row stores only what the provider
-- actually said.
--
-- **A provider that did not answer must not look like a provider with no
-- GPUs.** Four of these five rate cards are read out of a web page, and a web
-- page can be redesigned between one refresh and the next. `cloud_price_run`
-- records per provider whether this refresh got an answer and what went wrong
-- if it did not, so the screen can say "Naver: 가져오지 못함" instead of
-- silently showing a shorter table.

CREATE TABLE IF NOT EXISTS cloud_price_snapshot (
    id           bigserial PRIMARY KEY,
    -- When this set of numbers first appeared. Does not move on a refresh
    -- that finds them unchanged.
    taken_at     timestamptz NOT NULL DEFAULT now(),
    -- When a refresh last confirmed these numbers are still published. This
    -- is the "가져온 시각" the screen shows.
    refreshed_at timestamptz NOT NULL DEFAULT now(),
    -- The rate used to convert every row below, as at `refreshed_at`.
    fx_base      text NOT NULL DEFAULT 'USD',
    fx_quote     text NOT NULL DEFAULT 'KRW',
    fx_rate      numeric(18, 6),
    -- The rate provider's own timestamp for the rate, which is not the same
    -- as when we asked: a rate quoted at 00:00 UTC and read at 09:00 KST is
    -- a nine-hour-old rate, and the screen should be able to say so.
    fx_as_of     timestamptz,
    fx_source    text,
    -- SHA-1 of every (provider, sku, amount, currency, unit) in this snapshot.
    -- The one value a refresh compares to decide whether anything moved.
    content_hash text NOT NULL,
    is_current   boolean NOT NULL DEFAULT false
);

-- Exactly one snapshot is the one the screen opens on.
CREATE UNIQUE INDEX IF NOT EXISTS cloud_price_snapshot_current_idx
    ON cloud_price_snapshot (is_current) WHERE is_current;

CREATE TABLE IF NOT EXISTS cloud_price (
    id          bigserial PRIMARY KEY,
    snapshot_id bigint NOT NULL REFERENCES cloud_price_snapshot (id) ON DELETE CASCADE,
    provider    text NOT NULL,
    category    text NOT NULL CHECK (category IN ('gpu', 'storage')),
    region      text,
    -- The provider's own name for the thing, and what it calls it on screen.
    sku         text NOT NULL,
    label       text NOT NULL,
    -- GPU model, count, vCPU, memory -- whatever this provider publishes.
    -- Free-form on purpose: five providers do not describe a machine the
    -- same way, and forcing them into one set of columns would mean
    -- inventing values none of them stated.
    spec        jsonb NOT NULL DEFAULT '{}'::jsonb,
    -- What one of `unit` costs, in `currency`. The 원가: exactly what the
    -- provider published, never converted.
    amount      numeric(18, 10) NOT NULL,
    currency    text NOT NULL,
    unit        text NOT NULL,
    sort        integer NOT NULL DEFAULT 0,
    UNIQUE (snapshot_id, provider, category, sku)
);

CREATE INDEX IF NOT EXISTS cloud_price_snapshot_idx
    ON cloud_price (snapshot_id, provider, category);

CREATE TABLE IF NOT EXISTS cloud_price_run (
    id          bigserial PRIMARY KEY,
    snapshot_id bigint NOT NULL REFERENCES cloud_price_snapshot (id) ON DELETE CASCADE,
    provider    text NOT NULL,
    outcome     text NOT NULL CHECK (outcome IN ('ok', 'failed')),
    -- On 'failed', why -- in a sentence a person can act on. On 'ok', the
    -- source version where the provider publishes one.
    detail      text,
    source_url  text,
    -- What the provider called this edition of its rate card, where it says.
    -- AWS does; it lets the next refresh skip a 202MB download.
    source_version text,
    row_count   integer NOT NULL DEFAULT 0,
    fetched_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (snapshot_id, provider)
);

CREATE TABLE IF NOT EXISTS cloud_price_change (
    id              bigserial PRIMARY KEY,
    snapshot_id     bigint NOT NULL REFERENCES cloud_price_snapshot (id) ON DELETE CASCADE,
    provider        text NOT NULL,
    category        text NOT NULL,
    sku             text NOT NULL,
    label           text NOT NULL,
    type            text NOT NULL CHECK (type IN ('added', 'changed', 'removed')),
    before_amount   numeric(18, 10),
    after_amount    numeric(18, 10),
    currency        text,
    unit            text,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS cloud_price_change_snapshot_idx
    ON cloud_price_change (snapshot_id);

COMMENT ON COLUMN cloud_price_snapshot.refreshed_at IS
    'Last time a refresh confirmed these numbers are still published. Moves without a new snapshot when nothing changed.';
COMMENT ON COLUMN cloud_price_snapshot.fx_rate IS
    'The rate at refreshed_at. Belongs to the reading, not to the price: the row stores only what the provider published.';
COMMENT ON COLUMN cloud_price.amount IS
    'The 원가 -- exactly what the provider published, in its own currency. Never the converted number.';
COMMENT ON TABLE cloud_price_run IS
    'Per provider, per refresh: did it answer. A provider that failed must not look like a provider with nothing to sell.';
