-- 0006_search_documents
-- The search corpus 0005 believed it had.
--
-- 0005 indexed `ledger_extracted_text` as "the text of everything collected".
-- That table's own comment says otherwise: it preserves text the live API
-- cannot return again -- 938 legacy Notion documents, growing by zero rows a
-- night. The text of what the collectors actually collect lives in
-- `ledger_records.raw_payload`, in five different API shapes, and nothing
-- read it. Measured on the 2026-09-10 KST nightly load: extracted_text 938
-- for notion, 0 for every other source, against 175k+ notion and 111k+ slack
-- ledger records.
--
-- `search_documents` is that corpus, filled by the load-time extractor
-- (ledger/extract_text.py). It is a separate table rather than a widening of
-- `ledger_extracted_text` because the two hold different kinds of thing:
-- preserved text is evidence and is never rewritten; extracted text is
-- derived, and a better extractor rewrites it. One table cannot honestly
-- promise both.
--
-- One row per collected entity, not per observation: doc_id is
-- md5(source || ':' || source_entity_id)::uuid, computed the same way in SQL
-- and in Python with no extension needed, so a message seen by a backfill and
-- again as a current head is one document, carrying the newest text.
--
-- The indexes and the embedding columns mirror 0005's, for the same reasons
-- 0005 states them (simple tsvector because Postgres has no Korean stemmer,
-- trigram for substrings inside an 어절, no HNSW until there is data). 0005's
-- columns on ledger_extracted_text stay where they are -- the 938 preserved
-- documents remain searchable through the rows copied below, and dropping
-- schema is riskier than leaving it idle.

CREATE TABLE IF NOT EXISTS search_documents (
    doc_id uuid PRIMARY KEY,
    ledger_id uuid REFERENCES ledger_records(ledger_id) ON DELETE SET NULL,
    source text NOT NULL,
    entity_type text NOT NULL,
    external_id text,
    occurred_at timestamptz,
    text_content text NOT NULL,
    text_sha256 text NOT NULL,
    extractor text NOT NULL,
    indexed_at timestamptz NOT NULL DEFAULT now(),
    embedding vector(1024),
    embedding_model text,
    embedded_at timestamptz,
    search_tsv tsvector GENERATED ALWAYS AS (to_tsvector('simple', text_content)) STORED
);

COMMENT ON TABLE search_documents IS
    'Search corpus: text derived from ledger_records.raw_payload by '
    'ledger/extract_text.py, one row per collected entity. Derived, not '
    'preserved -- a better extractor rewrites rows; the raw payload is the '
    'record. NULL embedding means not embedded yet, never not embeddable.';

CREATE INDEX IF NOT EXISTS search_documents_tsv_idx
    ON search_documents USING gin (search_tsv);
CREATE INDEX IF NOT EXISTS search_documents_trgm_idx
    ON search_documents USING gin (text_content gin_trgm_ops);
CREATE INDEX IF NOT EXISTS search_documents_source_occurred_idx
    ON search_documents (source, occurred_at DESC);
CREATE INDEX IF NOT EXISTS search_documents_unembedded_idx
    ON search_documents (indexed_at)
    WHERE embedding IS NULL;

-- The 938 preserved documents join the corpus as documents in their own
-- right: their text exists nowhere else (that is the whole point of that
-- table), so the extractor can never produce them from raw_payload. Their
-- doc_id is their artifact_id -- a different id space from the entity-keyed
-- documents, which is correct: they are not observations of a live entity.
INSERT INTO search_documents
    (doc_id, ledger_id, source, entity_type, external_id,
     occurred_at, text_content, text_sha256, extractor, indexed_at)
SELECT artifact_id, ledger_id, source, kind, NULL,
       inserted_at, text_content, text_sha256, extractor, now()
  FROM ledger_extracted_text
ON CONFLICT (doc_id) DO NOTHING;
