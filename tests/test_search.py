"""Search over the collected text.

The snippet logic runs without a database, because it is where the Korean
handling actually shows. Everything that needs SQL runs only against
`WORKLOG_TEST_DATABASE_URL`, the same throwaway database the loader tests use,
and is skipped otherwise -- like every other test here, this suite touches no
network and no operational database by default.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rlwrld_worklog.search import MATCHERS, SNIPPET_CHARS, _snippet, search_text  # noqa: E402


def test_a_snippet_centres_on_what_matched(  ) -> None:
    text = "머리말 " * 200 + "회의에서 결정된 것은 수집 주기다" + " 꼬리말" * 200
    snippet = _snippet(text, "수집")
    assert "수집" in snippet
    assert len(snippet) <= SNIPPET_CHARS + 2  # the two ellipses
    assert snippet.startswith("…") and snippet.endswith("…")


def test_a_snippet_of_a_short_document_is_the_document(  ) -> None:
    assert _snippet("짧은 문서", "문서") == "짧은 문서"


def test_a_snippet_falls_back_to_the_head_when_the_query_is_not_literal(  ) -> None:
    """A word-matcher hit need not contain the query as a substring.

    `plainto_tsquery` matches tokens, so a two-word query can match a document
    that holds neither word adjacently. Returning nothing would be worse than
    returning the head of the document.
    """
    snippet = _snippet("가나다라마바사", "없는말")
    assert snippet.startswith("가나다라마바사")


def test_an_empty_query_is_refused_before_any_connection(  ) -> None:
    """Refused on the argument, not by the database.

    `search_text` opens a connection; a blank query must never get that far,
    or an empty search box becomes a database round trip that scans the corpus.
    """
    for blank in ("", "   ", "\n"):
        with pytest.raises(ValueError, match="needs a query"):
            search_text("postgresql://unreachable/nowhere", blank)


def test_an_unknown_matcher_is_refused_by_name(  ) -> None:
    with pytest.raises(ValueError, match="unknown matcher"):
        search_text("postgresql://unreachable/nowhere", "무엇", matcher="fuzzy")
    assert MATCHERS == ("auto", "words", "substring")


# ------------------------------------------------------- real database


REQUIRES_DATABASE = pytest.mark.skipif(
    not os.environ.get("WORKLOG_TEST_DATABASE_URL"),
    reason="set WORKLOG_TEST_DATABASE_URL to a throwaway database to run the search round trip",
)


def _seed(url: str) -> None:
    """A schema and three documents: two Korean, one English."""
    import psycopg

    from rlwrld_worklog.ledger.load import apply_migrations

    repository = Path(__file__).resolve().parents[1]
    with psycopg.connect(url) as connection:
        with connection.cursor() as cursor:
            cursor.execute((repository / "sql" / "schema.sql").read_text(encoding="utf-8"))
        connection.commit()
    apply_migrations(
        database_url=url, migrations_dir=repository / "sql" / "migrations", dry_run=False
    )
    with psycopg.connect(url) as connection:
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM ledger_extracted_text")
            for index, (source, text) in enumerate(
                (
                    ("notion", "회의에서 수집을 매일 돌리기로 했다"),
                    ("slack", "수집 주기를 하루로 정했습니다"),
                    ("notion", "the collection runs every night"),
                )
            ):
                cursor.execute(
                    """
                    INSERT INTO ledger_extracted_text
                        (artifact_id, schema_version, source, kind, text_content,
                         text_sha256, char_length, byte_length, extractor)
                    VALUES (gen_random_uuid(), '1.0', %s, 'test', %s,
                            %s, %s, %s, 'test')
                    """,
                    (source, text, f"sha{index}", len(text), len(text.encode()), ),
                )
        connection.commit()


@REQUIRES_DATABASE
def test_a_whole_word_search_finds_the_document_holding_that_word() -> None:
    url = os.environ["WORKLOG_TEST_DATABASE_URL"]
    _seed(url)
    result = search_text(url, "수집", matcher="words")
    # "수집 주기를" tokenises with 수집 as its own word; "수집을" does not.
    assert [hit["source"] for hit in result.hits] == ["slack"]
    assert all(hit["matcher"] == "words" for hit in result.hits)


@REQUIRES_DATABASE
def test_substring_search_reaches_inside_a_korean_eojeol() -> None:
    """The reason both matchers exist.

    Postgres's `simple` parser splits Korean on whitespace, so a token is an
    어절 -- stem plus particle. A search for the stem alone therefore misses
    every document that inflected it: "수집" does not match "수집을". The
    substring matcher is what closes that, and it is measured here rather than
    asserted, because the first version of this test assumed the opposite and
    was wrong.
    """
    url = os.environ["WORKLOG_TEST_DATABASE_URL"]
    _seed(url)
    words_only = search_text(url, "수집", matcher="words")
    assert {hit["source"] for hit in words_only.hits} == {"slack"}
    result = search_text(url, "수집", matcher="substring")
    assert {hit["source"] for hit in result.hits} == {"notion", "slack"}


@REQUIRES_DATABASE
def test_auto_falls_back_only_when_words_found_nothing_and_says_so() -> None:
    url = os.environ["WORKLOG_TEST_DATABASE_URL"]
    _seed(url)

    found = search_text(url, "수집", matcher="auto")
    assert found.fell_back is False, "words matched, so the loose matcher must not run"

    # `simple` splits Korean on whitespace, so an 어절 like "수집을" is itself a
    # token and the word matcher finds it. What it cannot find is a fragment
    # *inside* an 어절: no document holds "집을" as a token, two hold it inside
    # one. That is the case the loose matcher exists for.
    fallen = search_text(url, "집을", matcher="auto")
    assert fallen.fell_back is True
    assert fallen.hits and all(hit["matcher"] == "substring" for hit in fallen.hits)
    assert {hit["source"] for hit in fallen.hits} == {"notion"}


@REQUIRES_DATABASE
def test_a_source_filter_narrows_the_corpus() -> None:
    url = os.environ["WORKLOG_TEST_DATABASE_URL"]
    _seed(url)
    result = search_text(url, "수집", matcher="substring", sources=["notion"])
    assert {hit["source"] for hit in result.hits} == {"notion"}


@REQUIRES_DATABASE
def test_every_hit_carries_where_it_came_from() -> None:
    """A result without provenance is a claim, not a search result."""
    url = os.environ["WORKLOG_TEST_DATABASE_URL"]
    _seed(url)
    hit = search_text(url, "collection", matcher="words").hits[0]
    for field in ("artifact_id", "source", "kind", "snippet", "score", "matcher"):
        assert field in hit
