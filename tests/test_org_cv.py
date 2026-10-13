"""인사정보 상세 is read from the archive by 사번, and nothing else picks the path."""

from __future__ import annotations

import json


def test_reads_the_newest_file_among_a_persons_numbers(tmp_path) -> None:
    from rlwrld_worklog.org.cv import read_cv

    (tmp_path / "100001.json").write_text(json.dumps({"built_at": "2026-01-01", "n": 1}))
    (tmp_path / "200001.json").write_text(json.dumps({"built_at": "2026-10-13", "n": 2}))
    assert read_cv(["100001", "200001"], root=tmp_path)["n"] == 2, "a rehire's newer analysis"
    assert read_cv(["999999"], root=tmp_path) is None


def test_a_number_that_is_not_a_plain_number_reads_nothing(tmp_path) -> None:
    from rlwrld_worklog.org.cv import read_cv

    (tmp_path / "secret.json").write_text(json.dumps({"x": 1}))
    assert read_cv(["../profiles/secret", "a/b"], root=tmp_path) is None


def test_a_broken_file_is_skipped_not_raised(tmp_path) -> None:
    from rlwrld_worklog.org.cv import read_cv

    (tmp_path / "100001.json").write_text("{not json")
    assert read_cv(["100001"], root=tmp_path) is None
