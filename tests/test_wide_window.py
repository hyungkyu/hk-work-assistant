# hook-allow: synthetic-credentials
"""A catch-up too wide to be one run.

On 2026-09-05 four days of collection were started as a single
`daily-collect --since 5d`. Two hours in it was still going, and nothing it had
already read was banked: an unbounded run advances no checkpoint until it ends,
so a failure in hour three would have lost all four days together and left the
next run starting exactly where the dead one did. August, backfilled one KST
day per run, had cost one day per failure instead.

So a window wider than `MAX_WINDOW_HOURS` is refused before the lock is taken,
with `scripts/backfill-days.sh` named in the refusal. The two things this must
not break are pinned here as well: the 26-hour nightly window, and a run that
already carries `--until` and is therefore one named slice.

Nothing here reaches a network. Every refusal happens while the config is being
built, which is before the first API call by construction.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rlwrld_worklog import cli
from rlwrld_worklog.daily import DEFAULT_SINCE, MAX_WINDOW_HOURS, DailyConfig


def config(tmp_path: Path, **kwargs: object) -> DailyConfig:
    return DailyConfig(
        archive_root=tmp_path / "archive",
        ledger_root=tmp_path / "staging",
        environment="test",
        load_database=False,
        **kwargs,
    )


class TestAWindowTooWideForOneRunIsRefused:
    def test_a_five_day_catch_up_is_refused(self, tmp_path: Path) -> None:
        """The run that started this. Five days is four days unbanked."""
        with pytest.raises(ValueError, match="has to be sliced"):
            config(tmp_path, since="5d")

    def test_the_refusal_names_the_runner_that_does_it_one_day_at_a_time(
        self, tmp_path: Path
    ) -> None:
        """A refusal that does not say what to run instead is an obstacle."""
        with pytest.raises(ValueError) as refused:
            config(tmp_path, since="5d")
        assert "scripts/backfill-days.sh" in str(refused.value)
        assert "--allow-wide-window" in str(refused.value)

    def test_an_absolute_since_weeks_back_is_measured_the_same_way(
        self, tmp_path: Path
    ) -> None:
        """The width is what matters, not how the operator spelled it."""
        with pytest.raises(ValueError, match="has to be sliced"):
            config(tmp_path, since="2026-08-01")

    def test_the_refusal_happens_before_the_lock_and_leaves_nothing_behind(
        self, tmp_path: Path
    ) -> None:
        archive_root = tmp_path / "archive"
        with pytest.raises(SystemExit) as refused:
            cli.main(
                [
                    "daily-collect",
                    "--environment",
                    "test",
                    "--archive-root",
                    str(archive_root),
                    "--since",
                    "5d",
                ]
            )
        assert "scripts/backfill-days.sh" in str(refused.value)
        assert not archive_root.exists(), "no lock, no archive, no manifest"


class TestTheWindowsThatMustKeepWorking:
    def test_the_nightly_incremental_window_is_untouched(self, tmp_path: Path) -> None:
        """26 hours is what the timer runs every night, unflagged."""
        assert config(tmp_path, since=DEFAULT_SINCE).since == DEFAULT_SINCE
        assert config(tmp_path).since == DEFAULT_SINCE

    def test_a_window_at_the_line_is_allowed_and_one_past_it_is_not(
        self, tmp_path: Path
    ) -> None:
        assert MAX_WINDOW_HOURS == 48
        assert config(tmp_path, since=f"{MAX_WINDOW_HOURS}h").since == "48h"
        with pytest.raises(ValueError, match="has to be sliced"):
            config(tmp_path, since=f"{MAX_WINDOW_HOURS + 1}h")

    def test_a_run_carrying_an_upper_bound_is_not_measured_by_this_rule(
        self, tmp_path: Path
    ) -> None:
        """A slice is already bounded, and the day runner produces only slices."""
        run_config = config(
            tmp_path,
            sources=("slack",),
            since="2026-08-01",
            until="2026-09-01",
        )
        assert run_config.until == "2026-09-01"

    def test_an_unparsable_since_is_left_to_the_collectors_to_report(
        self, tmp_path: Path
    ) -> None:
        """This check measures a width; it is not a second `--since` parser."""
        assert config(tmp_path, since="last tuesday").since == "last tuesday"


class TestTheEscapeHatch:
    def test_the_flag_runs_the_wide_window_anyway(self, tmp_path: Path) -> None:
        run_config = config(tmp_path, since="5d", allow_wide_window=True)
        assert run_config.allow_wide_window is True

    def test_the_cli_flag_reaches_the_config(self, tmp_path: Path, monkeypatch) -> None:
        seen: dict[str, DailyConfig] = {}

        def fake_run(run_config: DailyConfig, **_kwargs: object) -> dict[str, int]:
            seen["config"] = run_config
            return {"exit_code": 0}

        monkeypatch.setattr("rlwrld_worklog.daily.run_daily", fake_run)
        cli.main(
            [
                "daily-collect",
                "--environment",
                "test",
                "--archive-root",
                str(tmp_path / "archive"),
                "--since",
                "5d",
                "--allow-wide-window",
            ]
        )
        assert seen["config"].allow_wide_window is True
        assert seen["config"].since == "5d"

    def test_the_help_says_what_the_flag_costs(self, capsys) -> None:
        """Someone reaching for this flag is reaching past a refusal.

        The help is the only place they are told what they are buying, so it
        names the loss rather than describing the flag's mechanics.
        """
        with pytest.raises(SystemExit):
            cli.main(["daily-collect", "--help"])
        # argparse wraps; the sentence matters, the line breaks do not.
        printed = " ".join(capsys.readouterr().out.split())
        assert "--allow-wide-window" in printed
        assert "banks nothing until it finishes" in printed
        assert "scripts/backfill-days.sh" in printed
