"""Filling `search_documents.embedding`, which has been declared and empty.

The column, its width and an index on the unembedded rows were all put there
by 0006_search_documents and never filled -- "NULL embedding means not
embedded yet, never not embeddable", says the table's own comment. This is the
batch that makes that true.

Why this and not more full-text: Postgres ships no Korean analyser, so its
`simple` dictionary splits on whitespace and `배포` does not match `배포는` --
the particle is part of the token. Trigrams paper over it and stop working as
soon as the words differ ("배치가 해야지" against "자동으로 돌게"). A sentence
embedding matches meaning, which is what retrieving a precedent needs: the
situation will never be worded the way the old one was.

The model runs on HK's own machine. Two reasons, in order: the corpus includes
DMs and private channels, which should not leave it; and a 1--2GB embedding
model is small enough that this needs no GPU, no fine-tune and no ongoing cost.
Generation is a separate question -- nothing here writes a sentence.

The column is `vector(1024)`, which is bge-m3's width. A model of another
width is refused rather than truncated: a vector silently cut to fit compares
wrongly and there is no error to notice afterwards. The model that produced
each row is stored per row, so a change of model is visible and re-embeddable
rather than assumed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence

# The width of the column in 0006_search_documents.sql, and of bge-m3.
EMBED_DIM = 1024

DEFAULT_MODEL = os.environ.get("WORKLOG_EMBED_MODEL", "BAAI/bge-m3")

# How many texts go to the model at once. Small enough that a failure costs
# little and progress is banked often; large enough that the per-call overhead
# is not the cost. Batches are committed as they finish, so an interrupted run
# keeps everything it had already embedded.
BATCH = 32

# bge-m3 accepts 8192 tokens, and sentence-transformers sizes its buffers for
# whatever the model allows. On a 16GB card that asked for 4.3GB against 0.8GB
# free and died: the first real run, on 2026-09-21. Chat messages are short, so
# the length that matters here is a fraction of that, and capping it is the
# difference between fitting and not.
MAX_TOKENS = int(os.environ.get("WORKLOG_EMBED_MAX_TOKENS", "512"))


class Embedder(Protocol):
    """Anything that turns text into a fixed-width vector.

    A protocol rather than the model class, so the batch and the retrieval can
    be tested without downloading two gigabytes -- and so a different local
    model is a different object, not an edit to this file.
    """

    @property
    def name(self) -> str: ...

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class LocalEmbedder:
    """bge-m3 (or whatever `WORKLOG_EMBED_MODEL` names) on this machine.

    Imported lazily and held for the life of the object: loading the model is
    seconds, and doing it per batch would dominate the run.
    """

    def __init__(self, model_name: str = DEFAULT_MODEL) -> None:
        self._model_name = model_name
        self._model: Any = None

    @property
    def name(self) -> str:
        return self._model_name

    @property
    def model(self) -> Any:
        if self._model is None:
            # A missing dependency is reported as the one command that fixes
            # it, here, rather than as an ImportError three frames deep.
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as error:  # pragma: no cover - environment
                raise RuntimeError(
                    "sentence-transformers is not installed in this "
                    "environment. install it: .venv/bin/pip install "
                    "sentence-transformers"
                ) from error
            self._model = SentenceTransformer(self._model_name)
            # Set after construction: the model reports its own maximum, and
            # this lowers it rather than raising it past what it supports.
            try:
                self._model.max_seq_length = min(
                    MAX_TOKENS, int(self._model.max_seq_length)
                )
            except (AttributeError, TypeError, ValueError):
                pass
        return self._model

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            return self._encode(texts)
        except Exception as error:  # pragma: no cover - needs a real GPU
            if "out of memory" not in str(error).lower():
                raise
            # Halve and retry, then fall back to one at a time. A run that
            # finishes slowly is worth more than a run that dies at batch 40
            # with nothing said about why.
            import gc

            gc.collect()
            try:
                import torch

                torch.cuda.empty_cache()
            except Exception:
                pass
            if len(texts) == 1:
                raise
            middle = len(texts) // 2
            return self._encode(list(texts[:middle])) + self.embed(list(texts[middle:]))

    def _encode(self, texts: Sequence[str]) -> list[list[float]]:
        # Normalised, so cosine distance and inner product agree and the
        # pgvector `<=>` operator means what the ranking assumes it means.
        found = self.model.encode(
            list(texts), normalize_embeddings=True, show_progress_bar=False
        )
        return [[float(value) for value in row] for row in found]


def check_width(vectors: Sequence[Sequence[float]], *, model: str) -> None:
    """Refuse a model whose width is not the column's.

    Loudly, once, before anything is written. A vector cut or padded to fit
    still inserts and still compares, and nothing downstream can tell that its
    neighbours are nonsense.
    """
    for vector in vectors:
        if len(vector) != EMBED_DIM:
            raise ValueError(
                f"{model} returns {len(vector)}-dimensional vectors; the column "
                f"is vector({EMBED_DIM}). Use a model of that width, or add a "
                "column and a migration for the new one -- never truncate."
            )


@dataclass
class EmbedResult:
    dry_run: bool = True
    model: str = ""
    scope: str = "everything"
    candidates: int = 0
    embedded: int = 0
    batches: int = 0
    skipped_empty: int = 0
    seconds: float = 0.0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "model": self.model,
            "scope": self.scope,
            # Rows with no embedding for this model, before anything ran. The
            # number a second run should report lower.
            "candidates": self.candidates,
            "embedded": self.embedded,
            "batches": self.batches,
            "skipped_empty": self.skipped_empty,
            "seconds": self.seconds,
            "errors": self.errors[:20],
        }


# One person's own messages, and the messages those replied to. This is the
# corpus the precedent search actually reads, and it is three orders of
# magnitude smaller than everything: 581,315 documents on 2026-09-18, against a
# few thousand here. Embedding the whole archive is hours of CPU for text no
# question in this path ever looks at; this is minutes, and the rest can follow
# whenever it is worth it.
_PERSON_FILTER = """
       AND (
           doc.ledger_id IN (
               SELECT mine.ledger_id
                 FROM ledger_records mine
                WHERE mine.source = 'slack'
                  AND mine.relations->>'author_user_id' = ANY(%(handles)s)
           )
           OR doc.ledger_id IN (
               SELECT parent.ledger_id
                 FROM ledger_records parent
                 JOIN ledger_records reply
                   ON reply.source = 'slack'
                  AND reply.relations->>'author_user_id' = ANY(%(handles)s)
                  AND parent.source_entity_id = coalesce(
                      reply.relations->>'thread_id', reply.relations->>'parent_ts'
                  )
                WHERE parent.source = 'slack'
           )
       )
"""

# Rows that have no embedding from this model yet. Ordered newest first: a
# bounded first run should cover the text most likely to be searched, not the
# oldest rows in the archive.
_CANDIDATES_SQL = """
    SELECT doc_id, text_content
      FROM search_documents doc
     WHERE text_content <> ''
       AND (embedding IS NULL OR embedding_model IS DISTINCT FROM %(model)s)
       {source_filter}
       {person_filter}
     ORDER BY occurred_at DESC NULLS LAST
     LIMIT %(limit)s
"""

_COUNT_SQL = """
    SELECT count(*)
      FROM search_documents doc
     WHERE text_content <> ''
       AND (embedding IS NULL OR embedding_model IS DISTINCT FROM %(model)s)
       {source_filter}
       {person_filter}
"""

_WRITE_SQL = """
    UPDATE search_documents
       SET embedding = %(vector)s::vector,
           embedding_model = %(model)s,
           embedded_at = now()
     WHERE doc_id = %(doc_id)s
"""


def embed_corpus(
    database_url: str,
    embedder: Embedder,
    *,
    limit: int = 2000,
    sources: Sequence[str] = (),
    person_id: str | None = None,
    apply: bool = False,
) -> EmbedResult:
    """Give the searchable text its vectors, in batches, resumably.

    Dry run by default, like every other write in this project: the first
    thing a person wants from this command is how much there is to do.
    """
    from datetime import datetime, timezone

    import psycopg

    began = datetime.now(timezone.utc)
    result = EmbedResult(
        dry_run=not apply,
        model=embedder.name,
        scope="one person and the messages they answered" if person_id else "everything",
    )
    source_filter = "AND source = ANY(%(sources)s)" if sources else ""
    parameters: dict[str, Any] = {
        "model": embedder.name,
        "limit": max(1, int(limit)),
        "sources": list(sources),
    }

    with psycopg.connect(database_url) as connection:
        person_filter = ""
        if person_id:
            from .reconcile import person_handles

            with connection.cursor() as cursor:
                parameters["handles"] = person_handles(cursor, person_id)
            person_filter = _PERSON_FILTER
        with connection.cursor() as cursor:
            cursor.execute(
                _COUNT_SQL.format(
                    source_filter=source_filter, person_filter=person_filter
                ),
                parameters,
            )
            result.candidates = int(cursor.fetchone()[0])
            if not apply:
                return result
            cursor.execute(
                _CANDIDATES_SQL.format(
                    source_filter=source_filter, person_filter=person_filter
                ),
                parameters,
            )
            rows = cursor.fetchall()

        for start in range(0, len(rows), BATCH):
            chunk = rows[start : start + BATCH]
            texts = [str(row[1]) for row in chunk]
            try:
                vectors = embedder.embed(texts)
                check_width(vectors, model=embedder.name)
            except ValueError:
                # A width mismatch is not a bad batch, it is the wrong model.
                raise
            except Exception as error:  # pragma: no cover - model runtime
                result.errors.append(f"batch at {start}: {type(error).__name__}: {error}")
                continue
            with connection.cursor() as cursor:
                for (doc_id, _text), vector in zip(chunk, vectors):
                    cursor.execute(
                        _WRITE_SQL,
                        {
                            # pgvector accepts the literal '[a,b,c]' form, and
                            # this avoids depending on the psycopg adapter
                            # being registered in every entry point.
                            "vector": "[" + ",".join(repr(value) for value in vector) + "]",
                            "model": embedder.name,
                            "doc_id": doc_id,
                        },
                    )
            # Committed per batch: an interrupted run keeps what it finished.
            connection.commit()
            result.batches += 1
            result.embedded += len(chunk)

    result.seconds = round((datetime.now(timezone.utc) - began).total_seconds(), 1)
    return result
