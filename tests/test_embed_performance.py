"""Cost measured here, not on HK's machine.

HK, 2026-09-21: "이런거, 너무 빈번한거 같아. 미리 성능 테스트 해보고 실행하라고
하면 안돼?" Fair, and it had happened four times in a day: meeting notes at
49.9s, a mention join that cross-multiplied the roster, a first embedding scope
that meant ten hours of CPU, and a candidate query so exact it took longer than
the work it was narrowing.

Every one of those was discovered by him running it. This file is where that
moves: a corpus large enough for the shape of a query to matter, and a ceiling
that fails the suite rather than his evening.

The numbers are deliberately loose. The point is not to measure this machine,
which is not his machine; it is to catch a query whose cost grows with the
archive -- those miss by a factor of hundreds, not by 20%.
"""

from __future__ import annotations

import os
import time
import uuid

import pytest

REQUIRES_DATABASE = pytest.mark.skipif(
    not os.environ.get("WORKLOG_TEST_DATABASE_URL"),
    reason="set WORKLOG_TEST_DATABASE_URL to a throwaway database",
)

# Enough rows that a per-row correlated subquery separates from a set-based
# one. The real table held 581,315 documents on 2026-09-21; this is a fiftieth
# of that, and the difference it exposes is already seconds against minutes.
CORPUS = 12000

# What the batch is allowed to spend deciding *what* to embed, before it
# embeds anything. A scoping query is preparation; when preparation costs more
# than the work, the narrowing is not worth having.
SCOPE_BUDGET_SECONDS = 5.0


@pytest.fixture()
def corpus():
    import psycopg

    from rlwrld_worklog.ledger.load import apply_migrations

    url = os.environ["WORKLOG_TEST_DATABASE_URL"]
    apply_migrations(
        database_url=url,
        migrations_dir=__import__("pathlib").Path(__file__).resolve().parents[1]
        / "sql"
        / "migrations",
        dry_run=False,
    )
    person = uuid.uuid4()
    with psycopg.connect(url) as connection:
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM search_documents WHERE extractor = 'perf'")
            cursor.execute("DELETE FROM ledger_records WHERE source_file = 'perf'")
            cursor.execute("DELETE FROM org_identity WHERE value = 'UPERF'")
            cursor.execute("DELETE FROM org_person WHERE name = '성능테스트'")
            cursor.execute(
                "INSERT INTO roster_observation (observed_at, source, row_count) "
                "VALUES (now(), 'roster_seed_2', 1) RETURNING observation_id"
            )
            observation = cursor.fetchone()[0]
            cursor.execute(
                "INSERT INTO org_person (person_id, name, first_seen, last_seen) "
                "VALUES (%s, '성능테스트', %s, %s)",
                (person, observation, observation),
            )
            cursor.execute(
                "INSERT INTO org_identity (person_id, kind, value, first_seen, last_seen, "
                "origin) VALUES (%s, 'slack', 'UPERF', %s, %s, 'roster')",
                (person, observation, observation),
            )
            # One message in 37 is his. 37 and the 100 channels are coprime on
            # purpose: a stride that divides the channel count would put all
            # his messages in two rooms and make the scope look far narrower
            # than it is -- which is what the first version of this fixture
            # did, and the test caught it.
            cursor.execute(
                """
                INSERT INTO ledger_records (
                    ledger_id, schema_version, capture_profile, source, entity_type,
                    tenant_workspace_id, tenant_status, scope, source_entity_id,
                    source_updated_at_status, deleted_status, raw_payload,
                    content_hash, relations, source_file, source_file_sha256,
                    record_pointer, legacy_layout_version, converter_version,
                    observation_role, capture_completeness_status,
                    source_created_at, collected_at
                )
                SELECT gen_random_uuid(), 'v1', 'p', 'slack', 'message', 'T',
                       'observed',
                       jsonb_build_object('channel_id', 'C' || (index %% 100)),
                       -- The production id shape: the converters write
                       -- "{workspace}:{channel}:{ts}" while a reply points at
                       -- its parent by the bare ts. A fixture that wrote the
                       -- bare ts in both places is exactly why the thread
                       -- route shipped matching nothing.
                       'T:C' || (index %% 100) || ':perf-' || index,
                       'observed', 'observed',
                       jsonb_build_object('text', '메시지 ' || index),
                       'perf-' || index,
                       jsonb_build_object(
                           'author_user_id',
                           CASE WHEN index %% 37 = 0 THEN 'UPERF'
                                ELSE 'U' || (index %% 37) END,
                           -- One message in five is a thread reply, pointing
                           -- three messages back by bare ts.
                           'thread_id',
                           CASE WHEN index %% 5 = 0 AND index > 300
                                THEN 'perf-' || (index - 300) END,
                           'is_thread_reply', index %% 5 = 0 AND index > 300
                       ),
                       'perf', 'h', 'p', 'v', 'v', 'current_head', 'recorded',
                       -- Divided by the channel count, so messages *within*
                       -- a channel land about a minute apart and form real
                       -- conversations. The first version spaced them 100
                       -- minutes apart inside each channel, which made every
                       -- message its own block -- a fixture that measured
                       -- speed correctly and content not at all.
                       now() - make_interval(mins => index / 100),
                       now()
                  FROM generate_series(1, %s) AS index
                """,
                (CORPUS,),
            )
            cursor.execute(
                """
                INSERT INTO search_documents
                    (doc_id, ledger_id, source, entity_type, text_content,
                     text_sha256, extractor)
                SELECT gen_random_uuid(), ledger_id, 'slack', 'message',
                       raw_payload->>'text', content_hash, 'perf'
                  FROM ledger_records WHERE source_file = 'perf'
                """
            )
            # Statistics, because without them the planner is guessing and the
            # measurement is of the guess. A real database has been analysed;
            # a fixture that skips it produced 15s on one run and 0.6s on the
            # next, which measures nothing about the query.
            cursor.execute("ANALYZE ledger_records")
            cursor.execute("ANALYZE search_documents")
        connection.commit()
    yield url, str(person)
    with psycopg.connect(url) as connection:
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM search_documents WHERE extractor = 'perf'")
            cursor.execute("DELETE FROM ledger_records WHERE source_file = 'perf'")
        connection.commit()


@REQUIRES_DATABASE
def test_choosing_what_to_embed_costs_less_than_embedding_it(corpus):
    """The dry run is a count. It must answer in seconds, at any size.

    The version this replaced asked, for every document, whether he had spoken
    in that channel within ten minutes. Correct, unindexable, and the reason a
    command that should report a number instead looked like a hang.
    """
    url, person = corpus

    class NeverCalled:
        name = "perf/none"

        def embed(self, texts):  # pragma: no cover - must not run
            raise AssertionError("a dry run must not reach the model")

    from rlwrld_worklog.embedding import embed_corpus

    started = time.monotonic()
    result = embed_corpus(url, NeverCalled(), person_id=person, apply=False)
    took = time.monotonic() - started

    assert result.candidates > 0, "the scope found the conversations he is in"
    assert took < SCOPE_BUDGET_SECONDS, (
        f"deciding what to embed took {took:.1f}s over {CORPUS} documents. "
        "At the real archive's size that is minutes of waiting before any "
        "work starts -- narrow it differently, or do not narrow it"
    )


@REQUIRES_DATABASE
def test_the_scope_is_smaller_than_the_archive_and_larger_than_his_own_words(corpus):
    """Both failure modes, measured rather than argued.

    Too wide and the run is hours of text nothing will search; too narrow and
    the search has nothing to compare a situation against, which is how every
    question came back empty.
    """
    from rlwrld_worklog.embedding import embed_corpus

    url, person = corpus

    class NeverCalled:
        name = "perf/none"

        def embed(self, texts):  # pragma: no cover
            raise AssertionError("a dry run must not reach the model")

    scoped = embed_corpus(url, NeverCalled(), person_id=person, apply=False)
    his_own = CORPUS // 37

    assert scoped.candidates > his_own * 2, "situations, not only his replies"
    assert scoped.candidates <= CORPUS, "never more than the corpus"


@REQUIRES_DATABASE
def test_building_the_blocks_costs_less_than_embedding_them(corpus):
    """The rule I wrote for myself on 2026-09-21, applied to my own next step.

    Block building reads every message in every channel he speaks in. That is
    the shape that has bitten four times today, so it is measured here before
    it is handed over -- with the number, not with a promise.
    """
    from rlwrld_worklog.blocks import build_blocks

    url, person = corpus

    started = time.monotonic()
    result = build_blocks(url, person, apply=True)
    took = time.monotonic() - started

    assert result.blocks > 0
    assert result.blocks_with_him > 0
    assert result.messages > 0
    # Loose, and about the shape: 12,000 messages over 100 channels is a
    # fiftieth of the real corpus. Minutes here would mean hours there.
    assert took < 20.0, (
        f"building blocks took {took:.1f}s over {result.messages} messages in "
        f"{result.channels} channels -- measure again before handing it over"
    )
    print(
        f"blocks: {result.blocks_with_him} kept of {result.blocks} from "
        f"{result.messages} messages in {took:.1f}s"
    )


# What proposing the candidate questions is allowed to cost. Measured on HK's
# machine on 2026-09-22: 5,120 of his answers took 1,408 seconds, a quarter of
# a second each, because the nearby-messages query scanned the channel once per
# answer. This corpus is a fiftieth of his archive, so the budget is set where
# a per-answer scan fails and an index lookup passes.
PAIR_BUDGET_SECONDS = 20.0


@REQUIRES_DATABASE
def test_proposing_the_questions_costs_less_than_reviewing_them(corpus):
    """A review queue he waits 23 minutes for is a queue he stops rebuilding.

    The rule from 2026-09-21 -- measure the cost before handing the command
    over -- applied to the command I had already handed over once.
    """
    from rlwrld_worklog.blocks import build_blocks, propose_pairs

    url, person = corpus
    build_blocks(url, person, names={"UPERF": "HK"}, apply=True)

    started = time.monotonic()
    result = propose_pairs(url, person, names={"UPERF": "HK"}, apply=True)
    took = time.monotonic() - started

    assert result.answers > 0, "his messages are in the corpus"
    assert took < PAIR_BUDGET_SECONDS, (
        f"proposing candidates for {result.answers} answers took {took:.1f}s. "
        "At the real archive's size that is the 23 minutes it already cost "
        "once -- the per-answer query is scanning, not looking up"
    )


@REQUIRES_DATABASE
def test_the_per_answer_query_looks_up_rather_than_scans(corpus):
    """The wall clock cannot see this one, so ask the planner.

    A fiftieth of the archive is not enough for a sequential scan to separate
    from an index lookup by time -- both finish. The difference only shows at
    his size, which is where it cost 23 minutes. So the assertion is about the
    plan, which does not depend on how big this fixture happens to be.
    """
    import psycopg

    from rlwrld_worklog.blocks import _NEARBY_SQL

    url, _person = corpus
    with psycopg.connect(url) as connection:
        with connection.cursor() as cursor:
            cursor.execute("ANALYZE ledger_records")
            cursor.execute(
                "EXPLAIN " + _NEARBY_SQL,
                {
                    "channel": "C7",
                    "handles": ["UPERF"],
                    "before": "2026-09-22T00:00:00Z",
                    "window": 120,
                    "nearby": 3,
                },
            )
            plan = "\n".join(row[0] for row in cursor.fetchall())

    assert "Seq Scan" not in plan, (
        "the messages before one answer are found by scanning every Slack row:\n"
        f"{plan}\n"
        "This runs once per answer -- 5,120 times on 2026-09-22 -- which is "
        "what made proposing the queue take 1,408 seconds."
    )


@REQUIRES_DATABASE
def test_a_thread_reply_finds_its_parent_at_production_id_shape(corpus):
    """The bug this whole file could not have caught before.

    Every fixture wrote bare timestamps into `source_entity_id`, so the join
    between a reply's `parent_ts` and its parent's id matched in every test and
    in nothing else. On the real ledger it built 0 thread blocks out of 1,674
    threads and reported all 1,674 as having no parent -- a count that reads
    like a finding about the data and was a finding about the query.
    """
    from rlwrld_worklog.blocks import build_blocks
    from rlwrld_worklog.slack_sweep import orphan_thread_parents

    url, person = corpus
    result = build_blocks(url, person, names={"UPERF": "HK"}, apply=True)

    assert result.thread_blocks > 0, (
        "replies point at parents that are present in this corpus; zero here "
        "means the id spellings are being compared directly again"
    )
    # And the other side of the same join: nothing in this corpus is an orphan,
    # so a sweep seeded from it should have nothing to fetch.
    assert orphan_thread_parents(url) == {}, (
        "every parent is present; a non-empty sweep seed means the nightly "
        "sweep is re-fetching threads the ledger already holds"
    )
