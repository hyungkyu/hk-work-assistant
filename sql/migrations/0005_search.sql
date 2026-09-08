-- 0005_search
-- Full-text search over collected text, inside the database that already holds it.
--
-- Why this migration exists
--   * An OpenSearch container had been running since the first compose file
--     and no code ever read it. It was removed on 2026-09-08 rather than
--     wired up: a second store has to be kept in sync with this one, and
--     every quiet failure this system has had was of the form "two things
--     disagree and nobody is looking". Search lives here instead.
--   * `ledger_extracted_text` already holds the text of everything collected,
--     one row per artifact, with provenance back to the ledger record. It is
--     the natural search corpus and needed only an index.
--
-- Korean, and why the configuration is `simple`
--   Postgres ships no Korean stemmer. `english` would stem Korean tokens
--   wrongly and drop English stopwords that matter here (a channel named
--   "the-board" loses half its name). `simple` lowercases and does nothing
--   else, which is honest: it matches whole words, and Korean agglutination
--   means "수집을" will not match a search for "수집".
--
--   That is what the trigram index is for. `pg_trgm` matches substrings, so
--   "수집" finds "수집을" and "수집기" the way a person expects. The two
--   indexes answer different questions and the query planner picks per query;
--   neither is a fallback for the other.
--
--   If measurement shows recall is still poor, the next step is embeddings in
--   the vector column below -- not a separate search server.
--
-- Cost
--   One generated column and three indexes on a table that is append-only.
--   The tsvector is `STORED`, so it is computed on insert and never on read.

-- Substring matching. Required by the trigram indexes below.
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- Embeddings, for the step after full-text search. Declared here so the
-- database is touched once: the extension and the column cost nothing while
-- they are unused, and adding them later would mean another migration against
-- a table that by then holds millions of rows.
--
-- The image is `pgvector/pgvector:pg17`, which is the official postgres image
-- plus this extension. On a database whose image does not carry it, this
-- statement fails and the migration stops here -- which is the correct
-- outcome, because the alternative is a schema that claims a capability the
-- server does not have.
CREATE EXTENSION IF NOT EXISTS vector;

ALTER TABLE ledger_extracted_text
    ADD COLUMN IF NOT EXISTS search_tsv tsvector
    GENERATED ALWAYS AS (to_tsvector('simple', text_content)) STORED;

CREATE INDEX IF NOT EXISTS ledger_extracted_text_tsv_idx
    ON ledger_extracted_text USING gin (search_tsv);

-- Substring search over the same text. GIN rather than GiST: this table is
-- append-only and read far more often than written, which is the case GIN is
-- built for.
CREATE INDEX IF NOT EXISTS ledger_extracted_text_trgm_idx
    ON ledger_extracted_text USING gin (text_content gin_trgm_ops);

-- Search is almost always scoped -- one source, one kind, a date range -- and
-- the scope is what makes a result trustworthy enough to act on.
CREATE INDEX IF NOT EXISTS ledger_extracted_text_source_inserted_idx
    ON ledger_extracted_text (source, inserted_at DESC);

-- The embedding itself. Nullable and unfilled: a row is searchable by text the
-- moment it lands, and gains an embedding whenever the embedding batch next
-- runs. Nothing waits on a model being up.
--
-- 1024 dimensions matches the local embedding models worth running on one
-- machine (bge-m3 among them, which handles Korean and English in one model).
-- A different model means a different width, which means a new column and a
-- re-embed -- so the model that fills this is recorded per row rather than
-- assumed.
ALTER TABLE ledger_extracted_text
    ADD COLUMN IF NOT EXISTS embedding vector(1024);
ALTER TABLE ledger_extracted_text
    ADD COLUMN IF NOT EXISTS embedding_model text;
ALTER TABLE ledger_extracted_text
    ADD COLUMN IF NOT EXISTS embedded_at timestamptz;

-- Which rows still need embedding. A partial index, because the query that
-- uses it is "give me the next batch of un-embedded rows" and that set shrinks
-- to nothing once the backlog is done.
CREATE INDEX IF NOT EXISTS ledger_extracted_text_unembedded_idx
    ON ledger_extracted_text (inserted_at)
    WHERE embedding IS NULL;

-- No vector index yet, deliberately. An HNSW index on an empty column costs
-- build time and helps nothing, and pgvector builds a better one from
-- populated data. It is created by the embedding batch once the backlog is
-- filled, and that decision is recorded there rather than guessed here.

COMMENT ON COLUMN ledger_extracted_text.search_tsv IS
    'Generated tsvector over text_content using the simple configuration. '
    'Postgres has no Korean stemmer; simple matches whole words and the '
    'trigram index covers substrings.';
COMMENT ON COLUMN ledger_extracted_text.embedding IS
    'Filled by the embedding batch from a local model. NULL means not embedded '
    'yet, never means not embeddable.';
