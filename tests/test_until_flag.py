# hook-allow: synthetic-credentials
"""`--until`, on `worklog collect` and on `worklog daily-collect`.

Slack and Notion have accepted an exclusive upper bound since they were
written, and GitHub and Slurm have always taken an explicit window. Until this
flag existed none of that was reachable from a command line, which is what made
a month-by-month backfill impossible without writing Python.

The properties pinned here are the ones a silent regression would cost a
backfill: that a bare date is read as a KST midnight rather than a UTC one,
that the bound actually reaches the collector, that no run carrying one moves a
checkpoint on disk, and that a source which cannot express a bound refuses the
run instead of collecting the live head under the window's name.

Every collector below is driven by the scripted fakes from the collector test
modules. Nothing here reaches a network.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from test_daily_collect import config, credentials  # noqa: E402
from test_slack_collector import CHANNEL, DM, FakeSlack, message, ts  # noqa: E402

from rlwrld_worklog import cli, daily  # noqa: E402
from rlwrld_worklog.archive import RawArchive  # noqa: E402
from rlwrld_worklog.daily import DailyConfig, run_daily  # noqa: E402
from rlwrld_worklog.github_collector import KST  # noqa: E402
from rlwrld_worklog.slack_collector import SlackCollector, parse_until  # noqa: E402

AUGUST_END = "2026-09-01"  # exclusive: the first instant that is not August, KST


# --------------------------------------------------------------- parsing


class TestParseUntil:
    def test_a_bare_date_is_midnight_kst_not_midnight_utc(self) -> None:
        """The nine hours between the two readings are a day of records."""
        assert parse_until(AUGUST_END) == datetime(2026, 9, 1, tzinfo=KST)
        assert parse_until(AUGUST_END) != datetime(2026, 9, 1, tzinfo=timezone.utc)

    def test_an_instant_keeps_the_offset_it_was_written_with(self) -> None:
        assert parse_until("2026-09-01T00:00:00+09:00") == datetime(2026, 9, 1, tzinfo=KST)
        assert parse_until("2026-09-01T00:00:00Z") == datetime(2026, 9, 1, tzinfo=timezone.utc)

    def test_a_duration_is_refused_rather_than_read_as_an_instant(self) -> None:
        """`--since 26h` is a floor. "Everything before 26 hours ago" is a
        window nobody means to ask for, so it is not quietly produced."""
        with pytest.raises(ValueError, match="not a duration"):
            parse_until("26h")


# ------------------------------------------------- the bound reaches through


class Recorder:
    """Stands in for a collector and keeps the arguments it was called with."""

    def __init__(self, archive: RawArchive) -> None:
        self.archive = archive
        self.seen: dict = {}

    def collect(self, **kwargs):
        self.seen.update(kwargs)
        raise daily.CaptureFailed(self.archive, RuntimeError("stop after recording"))


def record_capture(monkeypatch, module_name: str, factory_name: str, source: str, unpack):
    """Patch one collector factory to hand back a Recorder, and return it."""
    import importlib

    module = importlib.import_module(f"rlwrld_worklog.{module_name}")
    holder: dict = {}

    def factory(**kwargs):
        archive = RawArchive(
            kwargs["archive_root"],
            source,
            f"{source}-slice",
            kwargs["environment"],
            dry_run=kwargs.get("dry_run", False),
            config_root=kwargs.get("config_root"),
        )
        recorder = Recorder(archive)
        holder["recorder"] = recorder
        return unpack(archive, recorder)

    monkeypatch.setattr(module, factory_name, factory)
    return holder


def run_capture(capture, tmp_path: Path, source: str, **overrides) -> DailyConfig:
    # Named explicitly: a config asking for every source and an upper bound is
    # refused outright, which is the subject of its own test below.
    run_config = config(
        tmp_path, sources=(source,), since="2026-08-01", until=AUGUST_END, **overrides
    )
    with pytest.raises(daily.CaptureFailed):
        capture(run_config, credentials(tmp_path))
    return run_config


class TestTheBoundReachesTheCollector:
    def test_slack_is_given_the_bound_and_told_not_to_advance(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        holder = record_capture(
            monkeypatch,
            "slack_collector",
            "make_slack_collector",
            "slack",
            lambda archive, recorder: (None, archive, recorder),
        )
        run_capture(daily.capture_slack, tmp_path, "slack")

        seen = holder["recorder"].seen
        assert seen["until"] == parse_until(AUGUST_END)
        assert seen["advance_checkpoint"] is False

    def test_notion_is_given_the_bound_and_told_not_to_advance(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        holder = record_capture(
            monkeypatch,
            "notion_collector",
            "make_notion_collector",
            "notion",
            lambda archive, recorder: (archive, recorder),
        )
        run_capture(daily.capture_notion, tmp_path, "notion")

        seen = holder["recorder"].seen
        assert seen["until"] == parse_until(AUGUST_END)
        assert seen["advance_checkpoint"] is False

    def test_github_ends_its_window_on_the_last_kst_day_the_bound_admits(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """`--until 2026-09-01` is exclusive, so the window is exactly August."""
        holder = record_capture(
            monkeypatch,
            "github_collector",
            "make_github_collector",
            "github",
            lambda archive, recorder: (archive, recorder),
        )
        run_capture(daily.capture_github, tmp_path, "github")

        seen = holder["recorder"].seen
        assert seen["window"].start_date.isoformat() == "2026-08-01"
        assert seen["window"].end_date.isoformat() == "2026-08-31"
        assert seen["backfill"] is True, "a slice ignores the checkpoint in both directions"
        assert seen["advance_checkpoint"] is False

    def test_slurm_ends_its_window_on_the_last_kst_day_the_bound_admits(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        holder = record_capture(
            monkeypatch,
            "slurm_collector",
            "make_slurm_collector",
            "slurm",
            lambda archive, recorder: (archive, recorder),
        )
        run_capture(daily.capture_slurm, tmp_path, "slurm")

        seen = holder["recorder"].seen
        assert seen["window"].end_date.isoformat() == "2026-08-31"
        assert seen["backfill"] is True
        assert seen["advance_checkpoint"] is False

    def test_a_bound_inside_a_day_takes_that_whole_kst_day(self, tmp_path: Path) -> None:
        """A KST day is the smallest window GitHub and Slurm can express, so a
        mid-day bound rounds up rather than dropping the day it lands in."""
        window = daily._kst_window(
            config(
                tmp_path,
                sources=("github",),
                since="2026-08-01",
                until="2026-09-01T12:00:00+09:00",
            )
        )
        assert window.end_date.isoformat() == "2026-09-01"


# ----------------------------------------------------- the checkpoint itself


def slack_factory(client: FakeSlack):
    """The real Slack collector with a scripted client behind it."""

    def factory(*, archive_root, environment, token=None, capture_density="full", dry_run=False, config_root=None):
        archive = RawArchive(
            archive_root, "slack", "slack-slice", environment, dry_run=dry_run, config_root=config_root
        )
        return client, archive, SlackCollector(client, archive)

    return factory


def test_a_run_with_an_upper_bound_writes_no_checkpoint(tmp_path: Path, monkeypatch) -> None:
    """Asserted against the file on disk, not against what the collector says.

    A watermark moved to a slice's end would claim every month between that
    date and the previous watermark had been read, and none of them would ever
    be fetched again.
    """
    from rlwrld_worklog import slack_collector as collector_module

    client = FakeSlack(history={CHANNEL: [[message(ts(-100))]], DM: [[]]})
    monkeypatch.setattr(collector_module, "make_slack_collector", slack_factory(client))

    run_config = config(tmp_path, sources=("slack",), since="2026-08-01", until=AUGUST_END)
    summary = run_daily(run_config, credentials=credentials(tmp_path), captures=daily.DEFAULT_CAPTURES)

    result = next(item for item in summary["sources"] if item["source"] == "slack")
    assert result["checkpoint_advanced"] is False
    assert not (run_config.archive_root / "manifests/slack/test/checkpoint.json").exists()
    assert summary["mode"] == "date_slice"


def test_the_same_run_without_a_bound_does_write_one(tmp_path: Path, monkeypatch) -> None:
    """The control: the checkpoint is withheld by the bound, not by the fake."""
    from rlwrld_worklog import slack_collector as collector_module

    client = FakeSlack(history={CHANNEL: [[message(ts(-100))]], DM: [[]]})
    monkeypatch.setattr(collector_module, "make_slack_collector", slack_factory(client))

    # The same wide `--since` as the sliced run above, so the bound is the only
    # difference between the two. Unbounded, that width is refused as a
    # catch-up that should have been day slices (`test_wide_window.py`); the
    # escape hatch is what keeps this control honest rather than narrow.
    run_config = config(
        tmp_path, sources=("slack",), since="2026-08-01", allow_wide_window=True
    )
    summary = run_daily(run_config, credentials=credentials(tmp_path), captures=daily.DEFAULT_CAPTURES)

    result = next(item for item in summary["sources"] if item["source"] == "slack")
    assert result["checkpoint_advanced"] is True
    assert (run_config.archive_root / "manifests/slack/test/checkpoint.json").exists()


# ---------------------------------------------------------------- refusals


class TestARunThatCannotBeHonouredIsRefused:
    def test_daily_collect_refuses_a_source_that_cannot_express_a_bound(
        self, tmp_path: Path
    ) -> None:
        with pytest.raises(ValueError, match="google-calendar"):
            config(tmp_path, sources=("slack", "google-calendar"), until=AUGUST_END)

    def test_the_refusal_happens_before_anything_is_written(self, tmp_path: Path) -> None:
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
                    "2026-08-01",
                    "--until",
                    AUGUST_END,
                ]
            )
        assert "google-calendar" in str(refused.value)
        assert not archive_root.exists(), "no lock, no archive, no manifest"

    def test_collect_refuses_the_flag_for_google_calendar(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit) as refused:
            cli.main(
                [
                    "collect",
                    "google-calendar",
                    "--since",
                    "2026-08-01",
                    "--until",
                    AUGUST_END,
                    "--archive-root",
                    str(tmp_path / "archive"),
                    "--no-database",
                ]
            )
        assert "--until cannot be honoured for google-calendar" in str(refused.value)

    def test_a_bound_that_is_not_after_the_floor_is_refused(self, tmp_path: Path) -> None:
        """An empty window is reported exactly as a genuinely quiet one is."""
        with pytest.raises(ValueError, match="not after"):
            config(tmp_path, sources=("slack",), since="2026-09-01", until="2026-08-01")

    def test_an_unparsable_bound_is_refused_by_name(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            config(tmp_path, sources=("slack",), until="last tuesday")


# --------------------------------------------------------------- the flags


def test_both_commands_accept_the_flag_and_pass_it_through(tmp_path: Path, monkeypatch) -> None:
    seen: dict = {}

    def fake_run(run_config, **kwargs):
        seen["config"] = run_config
        return {"exit_code": 0}

    monkeypatch.setattr(daily, "run_daily", fake_run)
    cli.main(
        [
            "daily-collect",
            "--environment",
            "test",
            "--archive-root",
            str(tmp_path / "archive"),
            "--source",
            "slack",
            "--since",
            "2026-08-01",
            "--until",
            AUGUST_END,
        ]
    )

    assert seen["config"].until == AUGUST_END
    assert isinstance(cli.build_parser().parse_args(
        ["collect", "slack", "--since", "2026-08-01", "--until", AUGUST_END]
    ).until, str)


def test_a_daily_config_without_a_bound_is_unchanged(tmp_path: Path) -> None:
    """The flag is opt-in: an ordinary nightly run still resumes and advances."""
    run_config = DailyConfig(archive_root=tmp_path, ledger_root=tmp_path / "ledger")
    assert run_config.until is None
    assert daily._advance_checkpoint(run_config) is True
