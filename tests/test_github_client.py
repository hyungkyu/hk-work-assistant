"""GitHub client parsing: commit records, diff stats, token precedence.

No process is spawned and no network call is made. The parsing helpers are
exercised directly on invented `git log` output, because that is where the
legacy collector lost commit bodies: it split records on tabs and kept only
`%s`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rlwrld_worklog.github_client import (
    _parse_commit_records,
    _parse_json_items,
    _parse_numstat,
    _parse_numstat_pass,
    read_github_token,
)
from rlwrld_worklog.github_collector import GitHubApiError

US = "\x1f"
RS = "\x1e"


def record(
    *,
    sha: str = "a" * 40,
    parents: str = "p1",
    subject: str = "one line",
    body: str = "one line\n\nlonger body\nwith\ttabs and newlines",
    committed_at: str = "2026-08-01T05:00:00+09:00",
) -> str:
    fields = [
        sha,
        sha[:7],
        parents,
        "t" * 40,
        "Author One",
        "author@example.invalid",
        "2026-08-01T04:00:00+09:00",
        "Committer Two",
        "committer@example.invalid",
        committed_at,
        subject,
        body,
    ]
    return US.join(fields) + RS


class TestCommitParsing:
    def test_a_multiline_body_with_tabs_survives(self) -> None:
        records = list(_parse_commit_records(record(), repository="alpha"))
        assert len(records) == 1
        parsed = records[0]
        assert parsed["subject"] == "one line"
        # The whole body, not just its first line: the legacy collector kept
        # `%s` alone and every commit message body was lost.
        assert parsed["body"] == "one line\n\nlonger body\nwith\ttabs and newlines"
        assert parsed["repository"] == "alpha"
        assert parsed["committer_name"] == "Committer Two"
        assert parsed["author_name"] == "Author One"

    def test_author_and_committer_stay_distinct(self) -> None:
        parsed = next(_parse_commit_records(record(), repository="alpha"))
        assert parsed["author_email"] != parsed["committer_email"]
        assert parsed["authored_at"] != parsed["committed_at"]

    def test_a_merge_commit_reports_its_parents(self) -> None:
        parsed = next(_parse_commit_records(record(parents="p1 p2"), repository="alpha"))
        assert parsed["parents"] == ["p1", "p2"]
        assert parsed["parent_count"] == 2
        assert parsed["is_merge"] is True

    def test_a_root_commit_has_no_parents(self) -> None:
        parsed = next(_parse_commit_records(record(parents=""), repository="alpha"))
        assert parsed["parents"] == []
        assert parsed["parent_count"] == 0
        assert parsed["is_merge"] is False

    def test_two_records_are_split_on_the_record_separator(self) -> None:
        # `git log --pretty=format:` puts a newline *between* records, so the
        # second record arrives with it attached to its first field. Without
        # that newline in the fixture the sha looks clean when it is not, and
        # the commit silently fails to join with its own diff statistics.
        text = record(sha="a" * 40) + "\n" + record(sha="b" * 40)
        parsed = list(_parse_commit_records(text, repository="alpha"))
        assert [entry["sha"] for entry in parsed] == ["a" * 40, "b" * 40]
        assert [entry["sha_short"] for entry in parsed] == ["a" * 7, "b" * 7]

    def test_a_short_record_is_dropped_rather_than_misaligned(self) -> None:
        broken = US.join(["only", "three", "fields"]) + RS
        assert list(_parse_commit_records(broken, repository="alpha")) == []

    def test_empty_output_yields_nothing(self) -> None:
        assert list(_parse_commit_records("", repository="alpha")) == []


class TestDiffstatPass:
    def test_statistics_are_keyed_by_sha_across_records(self) -> None:
        text = (
            "\x1e" + "a" * 40 + "\n\n4\t2\tsrc/one.py\n"
            "\x1e" + "b" * 40 + "\n\n1\t0\tsrc/two.py\n"
        )
        statistics = _parse_numstat_pass(text)
        assert sorted(statistics) == ["a" * 40, "b" * 40]
        assert statistics["a" * 40][0]["path"] == "src/one.py"
        assert statistics["b" * 40][0]["additions"] == 1

    def test_a_commit_with_no_file_lines_maps_to_an_empty_list(self) -> None:
        statistics = _parse_numstat_pass("\x1e" + "c" * 40 + "\n")
        assert statistics == {"c" * 40: []}

    def test_empty_output_is_an_empty_mapping(self) -> None:
        assert _parse_numstat_pass("") == {}


class TestNumstat:
    def test_counts_are_summed_per_commit(self) -> None:
        files = _parse_numstat("4\t2\tsrc/one.py\n10\t0\tsrc/two.py")
        assert files is not None
        assert [entry["path"] for entry in files] == ["src/one.py", "src/two.py"]
        assert sum(entry["additions"] for entry in files) == 14
        assert sum(entry["deletions"] for entry in files) == 2
        assert all(entry["binary"] is False for entry in files)

    def test_a_binary_file_is_marked_binary_not_zero_lines(self) -> None:
        files = _parse_numstat("-\t-\tassets/logo.png")
        assert files is not None
        assert files[0]["binary"] is True
        assert files[0]["additions"] == 0

    def test_absent_statistics_are_none_not_an_empty_change(self) -> None:
        assert _parse_numstat("") is None
        assert _parse_numstat("   ") is None


class TestJsonItems:
    def test_a_list_body_is_returned_as_items(self) -> None:
        assert _parse_json_items(json.dumps([{"id": 1}, {"id": 2}])) == [{"id": 1}, {"id": 2}]

    def test_a_wrapped_body_is_unwrapped_rather_than_read_as_one_item(self) -> None:
        body = json.dumps({"total_count": 2, "items": [{"id": 1}, {"id": 2}]})
        assert _parse_json_items(body) == [{"id": 1}, {"id": 2}]

    def test_a_single_object_is_one_item(self) -> None:
        assert _parse_json_items(json.dumps({"id": 7})) == [{"id": 7}]

    def test_an_empty_body_is_empty(self) -> None:
        assert _parse_json_items("") == []

    def test_unparseable_output_raises_rather_than_returning_nothing(self) -> None:
        with pytest.raises(GitHubApiError):
            _parse_json_items("{not json")


class TestToken:
    def test_the_credentials_file_wins_over_the_environment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        credentials = tmp_path / "credentials"
        credentials.mkdir()
        # Synthetic value, never a real credential.
        (credentials / "github-token").write_text("file-value-not-a-real-token\n", encoding="utf-8")
        monkeypatch.setenv("GITHUB_TOKEN", "environment-value-not-a-real-token")
        assert read_github_token(tmp_path) == "file-value-not-a-real-token"

    def test_the_environment_is_the_fallback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GITHUB_TOKEN", "environment-value-not-a-real-token")
        assert read_github_token(tmp_path) == "environment-value-not-a-real-token"

    def test_no_token_anywhere_is_none_rather_than_an_empty_string(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        assert read_github_token(tmp_path) is None

    def test_a_blank_file_falls_through_to_the_environment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        credentials = tmp_path / "credentials"
        credentials.mkdir()
        (credentials / "github-token").write_text("\n", encoding="utf-8")
        monkeypatch.setenv("GITHUB_TOKEN", "environment-value-not-a-real-token")
        assert read_github_token(tmp_path) == "environment-value-not-a-real-token"
