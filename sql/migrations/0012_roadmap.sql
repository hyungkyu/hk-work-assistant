-- 0012_roadmap
--
-- The roadmap screen was a generated HTML file with the whole dataset inlined
-- in it. At this size -- five teams, seven families, thirty-odd products, a
-- hundred rows -- generating a file buys no speed, and it costs the thing that
-- matters: the file has no way to say when it was last true, so a stale one
-- and a current one look identical.
--
-- Two facts shape these tables.
--
-- A product does not belong to one team. `Simulation` is Robotics Platform and
-- HW, `Data Contract` is Robotics Platform and Infra, `Dataset Capture` is LOOP
-- and Robotics Platform. So the team lives on the ITEM, which is where the
-- source roadmap puts it, and the product carries only `owner_team_id` as the
-- default a per-product roll-up uses. A team x product join table would be a
-- second place for the same fact to be written, and the two would drift.
--
-- The `*_override` flags are what lets a refresh run without undoing somebody.
-- A refresh owns the text, the team and the hash, because those come from the
-- source page. Product, kind and horizon may be corrected by hand in the
-- backoffice, and once corrected the refresh must leave them alone.

CREATE TABLE IF NOT EXISTS roadmap_team (
    id        text PRIMARY KEY,
    label_ko  text NOT NULL,
    label_en  text NOT NULL,
    label_ja  text NOT NULL,
    sort      integer NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS roadmap_family (
    id        text PRIMARY KEY,
    label_ko  text NOT NULL,
    label_en  text NOT NULL,
    label_ja  text NOT NULL,
    color     text NOT NULL,
    sort      integer NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS roadmap_product (
    id            bigserial PRIMARY KEY,
    name          text NOT NULL UNIQUE,
    family_id     text NOT NULL REFERENCES roadmap_family (id),
    -- The default team a per-product roll-up is filed under. Not a claim that
    -- only this team touches the product; see the note at the top.
    owner_team_id text NOT NULL REFERENCES roadmap_team (id),
    detail_url    text,
    sort          integer NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS roadmap_snapshot (
    id            bigserial PRIMARY KEY,
    taken_at      timestamptz NOT NULL DEFAULT now(),
    label         text NOT NULL,
    source_url    text,
    -- Where the version this one replaced can still be read.
    prev_html_url text
);

CREATE TABLE IF NOT EXISTS roadmap_item (
    id               bigserial PRIMARY KEY,
    -- Identity across refreshes. Today an ordinal carried over from the first
    -- import; once the collector records Notion block ids it becomes one, and
    -- then a reordering of the source page stops reading as delete + add.
    item_key         text NOT NULL UNIQUE,
    notion_block_id  text,
    team_id          text NOT NULL REFERENCES roadmap_team (id),
    product_id       bigint NOT NULL REFERENCES roadmap_product (id),
    horizon          text NOT NULL CHECK (horizon IN ('now', 'next', 'soon', 'someday')),
    kind             text NOT NULL CHECK (kind IN ('dev', 'ops')),
    text_ko          text NOT NULL,
    text_en          text NOT NULL,
    text_ja          text NOT NULL,
    source_url       text,
    -- SHA-1 of the Korean original, first ten characters. The unit a refresh
    -- compares; a changed hash is a changed row.
    hash             text NOT NULL,
    snapshot_id      bigint REFERENCES roadmap_snapshot (id),
    horizon_override boolean NOT NULL DEFAULT false,
    product_override boolean NOT NULL DEFAULT false,
    kind_override    boolean NOT NULL DEFAULT false,
    sort             integer NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS roadmap_item_team_idx ON roadmap_item (team_id, horizon);
CREATE INDEX IF NOT EXISTS roadmap_item_product_idx ON roadmap_item (product_id);

CREATE TABLE IF NOT EXISTS roadmap_change (
    id          bigserial PRIMARY KEY,
    snapshot_id bigint NOT NULL REFERENCES roadmap_snapshot (id) ON DELETE CASCADE,
    item_key    text NOT NULL,
    team_id     text,
    type        text NOT NULL CHECK (type IN ('added', 'changed', 'removed')),
    before_text text,
    after_text  text,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS roadmap_change_snapshot_idx ON roadmap_change (snapshot_id);

COMMENT ON TABLE roadmap_item IS
    'One row of the platform roadmap. Team lives here, not on the product.';
COMMENT ON COLUMN roadmap_item.horizon_override IS
    'Set by hand in the backoffice; a refresh must not overwrite the column it guards.';
