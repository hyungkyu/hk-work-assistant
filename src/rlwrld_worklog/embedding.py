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

from .voice import ANSWER_WINDOW_MINUTES  # noqa: E402  (kept beside its use)

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


# The corpus the precedent search reads: the conversations he is part of.
#
# Two earlier shapes were wrong in opposite directions. The first embedded only
# his own messages, and then the search was inverted to match what *other*
# people said -- so every question fell back to characters and found nothing.
# The second fixed the scope with a correlated "did he speak here within ten
# minutes" test per row, which is exact and, against 581,315 documents with no
# index to support it, slower than embedding would have been. Narrowing cost
# more than it saved.
#
# So the scope is the channels he has spoken in at all. Wider than the minutes
# around his messages and far narrower than the archive, and it is two cheap
# queries: the channel list, then a plain IN. What it lets in is other people's
# conversation in rooms he is part of, which is exactly the material a
# situation comes from.
_CHANNELS_SQL = """
    SELECT DISTINCT coalesce(scope->>'channel_id', scope->>'container') AS channel
      FROM ledger_records
     WHERE source = 'slack'
       AND relations->>'author_user_id' = ANY(%(handles)s)
       AND coalesce(scope->>'channel_id', scope->>'container') IS NOT NULL
"""

_PERSON_FILTER = """
       AND EXISTS (
           SELECT 1
             FROM ledger_records rec
            WHERE rec.ledger_id = doc.ledger_id
              AND rec.source = 'slack'
              AND coalesce(rec.scope->>'channel_id', rec.scope->>'container')
                  = ANY(%(channels)s)
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

# The block corpus. Same batch, same width check, same per-row model name --
# a different table because a block is a different object from a message, and
# because clearing one does not disturb the other.
_BLOCK_COUNT_SQL = """
    SELECT count(*)
      FROM conversation_blocks
     WHERE situation_text <> ''
       AND (embedding IS NULL OR embedding_model IS DISTINCT FROM %(model)s)
       AND (%(person_id)s::text IS NULL OR person_id = %(person_id)s::text)
"""

_BLOCK_CANDIDATES_SQL = """
    SELECT block_id, situation_text
      FROM conversation_blocks
     WHERE situation_text <> ''
       AND (embedding IS NULL OR embedding_model IS DISTINCT FROM %(model)s)
       AND (%(person_id)s::text IS NULL OR person_id = %(person_id)s::text)
     ORDER BY started_at DESC
     LIMIT %(limit)s
"""

_BLOCK_WRITE_SQL = """
    UPDATE conversation_blocks
       SET embedding = %(vector)s::vector,
           embedding_model = %(model)s,
           embedded_at = now()
     WHERE block_id = %(doc_id)s
"""

_WRITE_SQL = """
    UPDATE search_documents
       SET embedding = %(vector)s::vector,
           embedding_model = %(model)s,
           embedded_at = now()
     WHERE doc_id = %(doc_id)s
"""


def _embed_blocks(
    database_url: str,
    embedder: Embedder,
    *,
    limit: int,
    person_id: str | None,
    apply: bool,
    result: EmbedResult,
    began,
) -> EmbedResult:
    """The conversation blocks, which are the corpus the search now reads."""
    from datetime import datetime, timezone

    import psycopg

    result.scope = "conversation blocks"
    parameters = {
        "model": embedder.name,
        "limit": max(1, int(limit)),
        "person_id": person_id,
    }
    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(_BLOCK_COUNT_SQL, parameters)
            result.candidates = int(cursor.fetchone()[0])
            if not apply:
                return result
            cursor.execute(_BLOCK_CANDIDATES_SQL, parameters)
            rows = cursor.fetchall()

        _write_vectors(
            connection, embedder, rows, result=result, statement=_BLOCK_WRITE_SQL
        )
    result.seconds = round((datetime.now(timezone.utc) - began).total_seconds(), 1)
    return result


def _write_vectors(connection, embedder, rows, *, result, statement) -> None:
    """Embed in batches and write, committing each batch as it lands."""
    import sys

    for start in range(0, len(rows), BATCH):
        chunk = rows[start : start + BATCH]
        texts = [str(row[1]) for row in chunk]
        try:
            vectors = embedder.embed(texts)
            check_width(vectors, model=embedder.name)
        except ValueError:
            raise
        except Exception as error:  # pragma: no cover - model runtime
            result.errors.append(f"batch at {start}: {type(error).__name__}: {error}")
            continue
        with connection.cursor() as cursor:
            for (doc_id, _text), vector in zip(chunk, vectors):
                cursor.execute(
                    statement,
                    {
                        "vector": "[" + ",".join(repr(value) for value in vector) + "]",
                        "model": embedder.name,
                        "doc_id": doc_id,
                    },
                )
        connection.commit()
        result.batches += 1
        result.embedded += len(chunk)
        if result.batches % 10 == 0:
            print(
                f"embedding… {result.embedded}/{len(rows)}", file=sys.stderr, flush=True
            )


def embed_corpus(
    database_url: str,
    embedder: Embedder,
    *,
    limit: int = 2000,
    sources: Sequence[str] = (),
    person_id: str | None = None,
    blocks: bool = False,
    apply: bool = False,
) -> EmbedResult:
    """Give the searchable text its vectors, in batches, resumably.

    Dry run by default, like every other write in this project: the first
    thing a person wants from this command is how much there is to do.
    """
    from datetime import datetime, timezone

    import psycopg

    began = datetime.now(timezone.utc)
    result = EmbedResult(dry_run=not apply, model=embedder.name)
    source_filter = "AND source = ANY(%(sources)s)" if sources else ""
    parameters: dict[str, Any] = {
        "model": embedder.name,
        "limit": max(1, int(limit)),
        "sources": list(sources),
        # Kept in step with voice.ANSWER_WINDOW_MINUTES on purpose: the batch
        # must embed exactly what the search will look at, and two numbers
        # that drift apart would leave a band of situations unsearchable and
        # nothing to show for it.
        "window": ANSWER_WINDOW_MINUTES,
    }

    if blocks:
        return _embed_blocks(
            database_url, embedder, limit=limit, person_id=person_id, apply=apply,
            result=result, began=began,
        )

    with psycopg.connect(database_url) as connection:
        person_filter = ""
        if person_id:
            from .reconcile import person_handles

            with connection.cursor() as cursor:
                handles = person_handles(cursor, person_id)
                cursor.execute(_CHANNELS_SQL, {"handles": handles})
                channels = [str(row[0]) for row in cursor.fetchall()]
            parameters["handles"] = handles
            parameters["channels"] = channels
            result.scope = f"{len(channels)} conversations he takes part in"
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
            # Printed to stderr as it goes. A run with no output is a run
            # nobody can tell from a hang, and this one takes minutes.
            if result.batches % 10 == 0:
                print(
                    f"embedding… {result.embedded}/{len(rows)}",
                    file=__import__("sys").stderr,
                    flush=True,
                )

    result.seconds = round((datetime.now(timezone.utc) - began).total_seconds(), 1)
    return result
