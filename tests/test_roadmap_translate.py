"""Translating the roadmap as it arrives, without letting a bad translation pass.

The model is replaced by a fake: what is tested is what gets written and what
does not, which is the part that can lie to the screen.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Sequence

import pytest

from rlwrld_worklog import roadmap, roadmap_translate as rt

DATABASE_URL = os.environ.get("ROADMAP_TEST_DATABASE_URL") or os.environ.get("WORKLOG_TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not DATABASE_URL, reason="ROADMAP_TEST_DATABASE_URL / WORKLOG_TEST_DATABASE_URL is not set")


class Fake:
    """Answers from a table; a second, sampled try can answer differently."""

    def __init__(self, answers: dict[tuple[str, str], str], retry: dict[tuple[str, str], str] | None = None) -> None:
        self.answers = answers
        self.retry_answers = retry or {}
        self.calls: list[tuple[str, int, bool]] = []

    @property
    def name(self) -> str:
        return "fake"

    def translate(self, texts: Sequence[str], language: str, *, retry: bool = False) -> list[str]:
        self.calls.append((language, len(texts), retry))
        table = self.retry_answers if retry else self.answers
        # The default answer carries no Hangul, so only a test's own answers can be unfit.
        return [table.get((text, language), f"{language}:{roadmap.text_hash(text)}") for text in texts]


def row(key: str, text: str) -> dict[str, Any]:
    return {"item_key": key, "text_ko": text, "hash": roadmap.text_hash(text)}


# --- what counts as a translation -----------------------------------------


def test_the_model_s_wrapping_is_taken_off() -> None:
    assert rt.clean('  "Build HRDexDB"\n\nNote: kept the name.') == "Build HRDexDB"
    assert rt.clean("「HRDexDB構築」") == "HRDexDB構築"


@pytest.mark.parametrize("text, language", [
    ("", "en"),
    ("Operations — 실험 재현성", "en"),
    ("運用 — 实験の再現性", "ja"),
])
def test_empty_korean_or_chinese_is_not_a_translation(text: str, language: str) -> None:
    assert not rt.acceptable(text, language)


def test_a_simplified_form_is_fine_in_english_text_only_when_it_is_not_there() -> None:
    assert rt.acceptable("運用 — 実験の再現性", "ja")
    assert rt.acceptable("Operations — reproducibility", "en")


# --- translating rows -------------------------------------------------------


def test_each_row_comes_back_in_both_languages() -> None:
    fake = Fake({("가", "en"): "A", ("가", "ja"): "ア"})
    writes, rejected = rt.translate_rows([row("1", "가")], fake)
    assert writes == [("A", "ア", "1", roadmap.text_hash("가"))]
    assert rejected == []


def test_an_unfit_answer_is_retried_once_by_sampling() -> None:
    fake = Fake({("실험", "ja"): "实験"}, retry={("실험", "ja"): "実験"})
    writes, rejected = rt.translate_rows([row("1", "실험")], fake)
    assert [w[1] for w in writes] == ["実験"]
    assert ("ja", 1, True) in fake.calls


def test_a_row_still_unfit_after_the_retry_is_refused_not_written() -> None:
    """English that reads as current and is not is worse than English that says it is behind."""
    fake = Fake({("실험", "en"): "실험"}, retry={("실험", "en"): "still 실험"})
    writes, rejected = rt.translate_rows([row("1", "실험")], fake)
    assert writes == []
    assert rejected == [("1", roadmap.text_hash("실험"))]


def test_a_retry_is_asked_only_for_what_failed() -> None:
    fake = Fake({("나", "en"): ""}, retry={("나", "en"): "B"})
    rt.translate_rows([row("1", "가"), row("2", "나")], fake)
    assert ("en", 1, True) in fake.calls
    assert not any(retry and n > 1 for _, n, retry in fake.calls)


# --- against the database ----------------------------------------------------


@pytest.fixture()
def seeded() -> str:
    source = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "roadmap_seed.json"
    roadmap.seed_from_payload(DATABASE_URL, json.loads(source.read_text(encoding="utf-8")))
    return DATABASE_URL


def _make_pending(database_url: str, key: str, text: str) -> None:
    import psycopg

    with psycopg.connect(database_url) as connection:
        connection.execute(
            "UPDATE roadmap_item SET text_ko = %s, hash = %s WHERE item_key = %s",
            (text, roadmap.text_hash(text), key),
        )


@needs_db
def test_a_seeded_roadmap_has_nothing_to_translate_and_loads_no_model(seeded: str) -> None:
    class Unloadable(Fake):
        def translate(self, *a: Any, **k: Any) -> list[str]:
            raise AssertionError("nothing is pending, so nothing may be translated")

    result = rt.translate_pending(seeded, Unloadable({}))
    assert result.pending == 0 and result.translated == 0


@needs_db
def test_a_reworded_row_is_translated_and_stops_reading_as_behind(seeded: str) -> None:
    key = roadmap.read_mapping(seeded)["items"][0]["key"]
    _make_pending(seeded, key, "새 문장")
    fake = Fake({("새 문장", "en"): "New line", ("새 문장", "ja"): "新しい行"})
    result = rt.translate_pending(seeded, fake)
    assert (result.pending, result.translated) == (1, 1)
    row_ = next(r for r in roadmap.read_roadmap(seeded)["items"] if r["id"] == key)
    assert row_["t"] == {"ko": "새 문장", "en": "New line", "ja": "新しい行"}
    assert row_["stale"] is False


@needs_db
def test_counting_without_apply_writes_nothing(seeded: str) -> None:
    key = roadmap.read_mapping(seeded)["items"][0]["key"]
    _make_pending(seeded, key, "새 문장")
    result = rt.translate_pending(seeded, Fake({}), apply=False)
    assert result.pending == 1 and result.translated == 0
    assert rt.pending_rows(seeded)[0]["item_key"] == key


@needs_db
def test_a_row_refused_at_this_korean_is_skipped_until_it_changes(seeded: str) -> None:
    key = roadmap.read_mapping(seeded)["items"][0]["key"]
    _make_pending(seeded, key, "어려운 줄")
    skip = {key: roadmap.text_hash("어려운 줄")}
    assert rt.pending_rows(seeded, skip=skip) == []
    _make_pending(seeded, key, "고친 줄")
    assert [r["item_key"] for r in rt.pending_rows(seeded, skip=skip)] == [key]


@needs_db
def test_a_row_whose_korean_moved_mid_translation_is_not_stamped_current(seeded: str) -> None:
    key = roadmap.read_mapping(seeded)["items"][0]["key"]
    _make_pending(seeded, key, "첫 문장")

    class Racing(Fake):
        def translate(self, texts: Sequence[str], language: str, *, retry: bool = False) -> list[str]:
            if language == "ja":
                _make_pending(seeded, key, "그새 바뀐 문장")
            return super().translate(texts, language, retry=retry)

    result = rt.translate_pending(seeded, Racing({}))
    assert result.translated == 0 and result.raced == [key]
    row_ = next(r for r in roadmap.read_roadmap(seeded)["items"] if r["id"] == key)
    assert row_["stale"] is True
