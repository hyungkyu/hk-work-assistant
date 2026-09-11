# hook-allow: synthetic-credentials
"""The daily orchestration command.

Sources are driven by the scripted fake API clients from the collector test
modules, so a run here exercises the real capture -> ledger path end to end
without a network call. The database stage is exercised through the recording
fake cursor from the loader tests.
"""

from __future__ import annotations

import json
import os
import signal
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from test_calendar_collector import FakeCalendarClient  # noqa: E402
from test_calendar_collector import collect as collect_calendar  # noqa: E402
from test_github_collector import FakeMirrors, FakeRest, commit, repository  # noqa: E402
from test_notion_collector import (  # noqa: E402
    PAGE_ID,
    SECOND_PAGE_ID,
    FakeNotionClient,
    exhausted,
    page_body,
)
from test_notion_collector import collect as collect_notion  # noqa: E402
from test_slack_collector import CHANNEL, DM, FakeSlack, message, ts  # noqa: E402
from test_slack_collector import collect as collect_slack  # noqa: E402
from test_slurm_collector import FakeFetcher, row, write_dump  # noqa: E402

from rlwrld_worklog import daily  # noqa: E402
from rlwrld_worklog.daily import (  # noqa: E402
    EXIT_CAPTURE_FAILED,
    EXIT_DOWNSTREAM_FAILED,
    EXIT_LOCKED,
    EXIT_OK,
    CaptureFailed,
    CaptureOutcome,
    Credentials,
    DailyConfig,
    run_daily,
)


def credentials(tmp_path: Path) -> Credentials:
    return Credentials(
        config_root=tmp_path / "config",
        slack_token="xoxp-synthetic",
        notion_token="ntn_synthetic",
        github_token="ghp_synthetic",
        google_token_path=None,
        settings={},
    )


def config(tmp_path: Path, **kwargs) -> DailyConfig:
    return DailyConfig(
        archive_root=kwargs.pop("archive_root", tmp_path / "archive"),
        ledger_root=kwargs.pop("ledger_root", tmp_path / "staging"),
        environment="test",
        load_database=kwargs.pop("load_database", False),
        **kwargs,
    )


def slack_capture(config_value: DailyConfig, _credentials: Credentials) -> CaptureOutcome:
    client = FakeSlack(history={CHANNEL: [[message(ts(-100))]], DM: [[]]})
    archive, result = collect_slack(
        config_value.archive_root,
        client,
        run_id="slack-run",
        dry_run=config_value.dry_run,
        max_messages=5 if config_value.smoke else None,
    )
    return CaptureOutcome(
        archive=archive,
        manifest_path=result.manifest_path,
        summary={"events": len(result.events)},
        checkpoint_advanced=result.checkpoint_advanced,
        events=result.events,
    )


def notion_capture(config_value: DailyConfig, _credentials: Credentials) -> CaptureOutcome:
    return _notion_capture_with(FakeNotionClient(), config_value)


def _notion_capture_with(client: FakeNotionClient, config_value: DailyConfig) -> CaptureOutcome:
    from rlwrld_worklog.daily import _notion_degraded_reason

    archive, _, result = collect_notion(
        config_value.archive_root, client, dry_run=config_value.dry_run
    )
    return CaptureOutcome(
        archive=archive,
        manifest_path=result.manifest_path,
        summary={"pages_collected": result.pages_collected, "capture_status": result.status},
        checkpoint_advanced=result.checkpoint_advanced,
        degraded_reason=_notion_degraded_reason(result),
    )


def calendar_capture(config_value: DailyConfig, _credentials: Credentials) -> CaptureOutcome:
    archive, result = collect_calendar(
        config_value.archive_root, FakeCalendarClient(), dry_run=config_value.dry_run
    )
    return CaptureOutcome(
        archive=archive,
        manifest_path=result.manifest_path,
        summary={"events": len(result.events)},
        checkpoint_advanced=result.checkpoint_advanced,
        events=result.events,
        notion_urls=result.notion_urls,
    )


GITHUB_ORGANIZATION = "example-org"
GITHUB_REPOSITORY = "alpha"
COMMIT_SHA = "a" * 40


def github_capture(config_value: DailyConfig, _credentials: Credentials) -> CaptureOutcome:
    """The real GitHub collector, on a scripted mirror and a scripted API.

    The commit is dated inside whatever window the run asks for rather than at
    a fixed date, because that window is derived from `--since` and moves with
    the clock: a fixture pinned to a calendar date would convert to zero ledger
    records the day after it was written.
    """
    from rlwrld_worklog.archive import RawArchive
    from rlwrld_worklog.github_collector import GithubCollector

    window = daily._kst_window(config_value)
    archive = RawArchive(
        config_value.archive_root, "github", "github-run", "test", dry_run=config_value.dry_run
    )
    collector = GithubCollector(
        FakeRest(repositories=[[repository(GITHUB_REPOSITORY)]]),
        FakeMirrors(
            {GITHUB_REPOSITORY: [commit(COMMIT_SHA, (window.start_at + timedelta(hours=1)).isoformat())]}
        ),
        archive,
        organization=GITHUB_ORGANIZATION,
    )
    result = collector.collect(
        window=window,
        # No REST kind, as a smoke run takes it: the commit path is what the
        # orchestration has to prove it reaches.
        kinds=(),
        advance_checkpoint=not config_value.dry_run,
    )
    return CaptureOutcome(
        archive=archive,
        manifest_path=result.manifest_path,
        checkpoint_advanced=result.checkpoint_advanced,
        summary={"commits": result.commits, "window": result.window.as_dict()},
    )


def slurm_capture(config_value: DailyConfig, _credentials: Credentials) -> CaptureOutcome:
    """The real Slurm collector, on a dump this test writes itself."""
    from rlwrld_worklog.archive import RawArchive
    from rlwrld_worklog.slurm_collector import SlurmCollector

    window = daily._kst_window(config_value)
    # sacct writes local time with no offset, which the collector reads as KST.
    ended_at = (window.start_at + timedelta(hours=10)).strftime("%Y-%m-%dT%H:%M:%S")
    dump = write_dump(
        config_value.archive_root / "dumps" / "kakao.psv.gz", [row("1001", end=ended_at)]
    )
    archive = RawArchive(
        config_value.archive_root, "slurm", "slurm-run", "test", dry_run=config_value.dry_run
    )
    collector = SlurmCollector(
        FakeFetcher({"kakao": dump}),
        archive,
        staging_root=config_value.archive_root / "staging" / "slurm",
    )
    result = collector.collect(
        window=window,
        clouds=("kakao",),
        advance_checkpoint=not config_value.dry_run,
    )
    return CaptureOutcome(
        archive=archive,
        manifest_path=result.manifest_path,
        checkpoint_advanced=result.checkpoint_advanced,
        summary={"jobs": result.jobs, "clouds_collected": list(result.clouds_collected)},
    )


ALL_CAPTURES = {
    "slack": slack_capture,
    "google-calendar": calendar_capture,
    "github": github_capture,
    "slurm": slurm_capture,
    "notion": notion_capture,
}


def stage(source_summary: dict, name: str) -> dict:
    return next(item for item in source_summary["stages"] if item["stage"] == name)


def source(summary: dict, name: str) -> dict:
    return next(item for item in summary["sources"] if item["source"] == name)


# ------------------------------------------------------------- happy path


def test_all_five_sources_capture_and_convert(tmp_path: Path) -> None:
    summary = run_daily(config(tmp_path), credentials=credentials(tmp_path), captures=ALL_CAPTURES)

    assert summary["status"] == "ok"
    assert summary["exit_code"] == EXIT_OK
    assert [item["source"] for item in summary["sources"]] == [
        "slack",
        "google-calendar",
        "github",
        "slurm",
        "notion",
    ], "Notion runs last so it drains the link queue Slack and Calendar filled"
    for name in ("slack", "google-calendar", "github", "slurm", "notion"):
        result = source(summary, name)
        assert result["status"] == "ok"
        assert stage(result, "capture")["status"] == "ok"
        assert stage(result, "ledger")["status"] == "ok"
        assert stage(result, "ledger")["detail"]["records_written"] > 0
        assert Path(stage(result, "ledger")["detail"]["output_path"]).is_file()


def test_a_notion_url_seen_in_calendar_is_fetched_by_notion_in_the_same_run(tmp_path: Path) -> None:
    run_config = config(tmp_path)
    run_daily(run_config, credentials=credentials(tmp_path), captures=ALL_CAPTURES)

    queue = json.loads(
        (run_config.archive_root / "manifests/notion/test/link-queue.json").read_text()
    )
    fetched = [item for item in queue["items"] if item["status"] == "fetched"]
    assert fetched, "the Notion URL discovered in a calendar event must be resolved"


def test_only_the_requested_sources_run(tmp_path: Path) -> None:
    summary = run_daily(
        config(tmp_path, sources=("notion",)),
        credentials=credentials(tmp_path),
        captures=ALL_CAPTURES,
    )
    assert [item["source"] for item in summary["sources"]] == ["notion"]


# ------------------------------------------------------- github and slurm


def test_github_and_slurm_reach_the_ledger_and_load_stages(tmp_path: Path, monkeypatch) -> None:
    """The gap this closes: both sources captured and then stopped.

    `github-collect` and `slurm-collect` write a manifest and return. Nothing
    converted it, so three of five sources had no ledger record since
    2026-09-01 while the archive filled up behind them.
    """
    import rlwrld_worklog.ledger.extract_text as extract_module
    import rlwrld_worklog.ledger.load as load_module

    loaded: list[str] = []
    indexed: list[tuple[str, ...]] = []

    def record(**kwargs):
        loaded.append(kwargs["source"])
        return load_module.LoadResult(source=kwargs["source"], dry_run=kwargs["dry_run"])

    def record_index(database_url, *, sources=(), dry_run=False):
        indexed.append(tuple(sources))
        return extract_module.IndexResult(dry_run=dry_run)

    monkeypatch.setattr(load_module, "load_source", record)
    monkeypatch.setattr(extract_module, "index_ledger_text", record_index)
    summary = run_daily(
        config(
            tmp_path,
            sources=("github", "slurm"),
            load_database=True,
            database_url="postgresql://fake",
        ),
        credentials=credentials(tmp_path),
        captures=ALL_CAPTURES,
    )

    assert [item["source"] for item in summary["sources"]] == ["github", "slurm"]
    assert loaded == ["github", "slurm"], "the ledger source names, not the collector directories"
    assert indexed == [("github",), ("slurm",)], "a successful load is followed by text indexing"
    for name in ("github", "slurm"):
        result = source(summary, name)
        assert result["status"] == "ok"
        for stage_name in ("capture", "ledger", "load", "index"):
            assert stage(result, stage_name)["status"] == "ok"
        assert stage(result, "ledger")["detail"]["records_written"] > 0
        assert Path(stage(result, "ledger")["detail"]["output_path"]).is_file()


def test_a_missing_github_token_fails_only_that_source(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    without_github = Credentials(
        config_root=tmp_path / "config",
        slack_token="xoxp-synthetic",
        notion_token="ntn_synthetic",
        github_token=None,
        google_token_path=None,
        settings={},
    )

    summary = run_daily(
        config(tmp_path),
        credentials=without_github,
        # The real capture, which is the thing that has to refuse before it
        # builds a client and shells out to `gh`.
        captures={**ALL_CAPTURES, "github": daily.capture_github},
    )

    assert summary["credentials_available"]["github"] is False
    assert source(summary, "github")["status"] == "failed"
    assert "no GitHub token" in stage(source(summary, "github"), "capture")["error"]
    for name in ("slack", "google-calendar", "slurm", "notion"):
        assert source(summary, name)["status"] == "ok"


def test_a_dry_run_moves_no_github_or_slurm_checkpoint(tmp_path: Path) -> None:
    """Both collectors record a `collected_through` KST date. A dry run that
    moved it would mark a day collected that was only rehearsed."""
    run_config = config(tmp_path, dry_run=True, sources=("github", "slurm"))
    summary = run_daily(run_config, credentials=credentials(tmp_path), captures=ALL_CAPTURES)

    for name in ("github", "slurm"):
        assert stage(source(summary, name), "capture")["status"] == "ok", "the capture still ran"
        assert source(summary, name)["checkpoint_advanced"] is False
        assert not (run_config.archive_root / f"manifests/{name}/test/checkpoint.json").exists()


def test_the_kst_window_covers_the_whole_day_the_since_instant_falls_in(tmp_path: Path) -> None:
    """GitHub and Slurm file records under a KST date, so the instant `--since`
    describes has to become a pair of whole days -- rounded outwards, because
    the alternative to re-reading a day's earlier hours is never reading them.
    """
    from rlwrld_worklog.github_collector import KST

    # 10:30 KST on 2026-09-05; 26 hours earlier is 08:30 KST on the 4th.
    now = datetime(2026, 9, 5, 1, 30, tzinfo=timezone.utc)
    window = daily._kst_window(config(tmp_path, since="26h"), now=now)

    assert window.days == ("2026-09-04", "2026-09-05")
    assert window.start_at == datetime(2026, 9, 4, tzinfo=KST), "the 08:30 cut is widened back"
    assert window.end_at == datetime(2026, 9, 6, tzinfo=KST), "today is captured whole"


# --------------------------------------------------------------- isolation


def test_one_source_failing_does_not_stop_the_others(tmp_path: Path) -> None:
    def broken(config_value: DailyConfig, _credentials: Credentials) -> CaptureOutcome:
        from rlwrld_worklog.archive import RawArchive

        archive = RawArchive(config_value.archive_root, "slack", "slack-broken", "test")
        raise CaptureFailed(archive, RuntimeError("synthetic Slack outage"))

    summary = run_daily(
        config(tmp_path),
        credentials=credentials(tmp_path),
        captures={**ALL_CAPTURES, "slack": broken},
    )

    assert summary["status"] == "failed"
    assert summary["exit_code"] == EXIT_CAPTURE_FAILED
    assert source(summary, "slack")["status"] == "failed"
    assert source(summary, "notion")["status"] == "ok"
    assert source(summary, "google-calendar")["status"] == "ok"


def test_a_failed_capture_still_leaves_a_manifest_on_disk(tmp_path: Path) -> None:
    def broken(config_value: DailyConfig, _credentials: Credentials) -> CaptureOutcome:
        from rlwrld_worklog.archive import RawArchive

        archive = RawArchive(config_value.archive_root, "slack", "slack-broken", "test")
        archive.write_page(
            "history",
            {"ok": True, "messages": [{"ts": "1.0"}]},
            endpoint="conversations.history",
            item_count=1,
        )
        raise CaptureFailed(archive, RuntimeError("synthetic Slack outage"))

    summary = run_daily(
        config(tmp_path), credentials=credentials(tmp_path), captures={"slack": broken}
    )
    manifest = json.loads(Path(source(summary, "slack")["manifest"]).read_text())

    assert manifest["status"] == "failed"
    assert "synthetic Slack outage" in manifest["error"]
    assert manifest["counters"]["pages_archived"] == 1
    assert manifest["counters"]["api_calls"] == 1
    assert manifest["counters"]["api_items"] == 1
    assert manifest["counters"]["coverage_complete"] is False


def test_sigterm_finishes_the_open_manifest_and_stops_later_sources(tmp_path: Path) -> None:
    calls: list[str] = []

    def interrupted(config_value: DailyConfig, _credentials: Credentials) -> CaptureOutcome:
        from rlwrld_worklog.archive import RawArchive

        calls.append("slack")
        archive = RawArchive(config_value.archive_root, "slack", "slack-term", "test")
        archive.write_page("history", {"ok": True}, endpoint="conversations.history")
        try:
            os.kill(os.getpid(), signal.SIGTERM)
        except daily.TerminationRequested as error:
            # The real capture adapters already turn ordinary Exceptions into
            # CaptureFailed with their open archive attached.
            raise CaptureFailed(archive, error) from error
        raise AssertionError("SIGTERM handler did not interrupt the capture")

    def should_not_run(config_value: DailyConfig, _credentials: Credentials) -> CaptureOutcome:
        calls.append("notion")
        raise AssertionError("a source after SIGTERM must not start")

    summary = run_daily(
        config(tmp_path),
        credentials=credentials(tmp_path),
        captures={"slack": interrupted, "notion": should_not_run},
    )

    manifest = json.loads(Path(source(summary, "slack")["manifest"]).read_text())
    assert calls == ["slack"]
    assert summary["status"] == "failed"
    assert summary["termination_signal"] == signal.SIGTERM
    assert "notion" in summary["sources_not_started"]
    assert manifest["status"] == "failed"
    assert manifest["counters"]["pages_archived"] == 1


def test_a_failing_source_cannot_touch_another_source_checkpoint(tmp_path: Path) -> None:
    def broken(config_value: DailyConfig, _credentials: Credentials) -> CaptureOutcome:
        from rlwrld_worklog.archive import RawArchive

        archive = RawArchive(config_value.archive_root, "slack", "slack-broken", "test")
        raise CaptureFailed(archive, RuntimeError("synthetic Slack outage"))

    run_config = config(tmp_path)
    run_daily(run_config, credentials=credentials(tmp_path), captures={**ALL_CAPTURES, "slack": broken})

    assert not (run_config.archive_root / "manifests/slack/test/checkpoint.json").exists()
    assert (run_config.archive_root / "manifests/notion/test/checkpoint.json").exists()
    assert (run_config.archive_root / "manifests/google-calendar/test/checkpoint.json").exists()


def test_a_collector_exiting_the_process_cannot_abort_the_run(tmp_path: Path) -> None:
    """A collector factory raises SystemExit for a missing credential."""

    def exits(config_value: DailyConfig, _credentials: Credentials) -> CaptureOutcome:
        raise SystemExit("SLACK_USER_TOKEN is required")

    summary = run_daily(
        config(tmp_path),
        credentials=credentials(tmp_path),
        captures={**ALL_CAPTURES, "slack": exits},
    )

    assert source(summary, "slack")["status"] == "failed"
    assert source(summary, "notion")["status"] == "ok", "the other sources still run"
    assert summary["exit_code"] == EXIT_CAPTURE_FAILED


def test_credentials_missing_for_one_source_is_reported_not_raised(tmp_path: Path) -> None:
    def needs_token(config_value: DailyConfig, _credentials: Credentials) -> CaptureOutcome:
        raise RuntimeError("no Notion token is available")

    summary = run_daily(
        config(tmp_path),
        credentials=credentials(tmp_path),
        captures={**ALL_CAPTURES, "notion": needs_token},
    )
    assert source(summary, "notion")["status"] == "failed"
    assert "no Notion token" in stage(source(summary, "notion"), "capture")["error"]
    assert source(summary, "slack")["status"] == "ok"


# ------------------------------------------------------------ stage split


def test_a_capture_stays_successful_when_the_ledger_stage_fails(tmp_path: Path, monkeypatch) -> None:
    import rlwrld_worklog.ledger.live as live_module

    def explode(**_kwargs):
        raise RuntimeError("synthetic converter fault")

    monkeypatch.setattr(live_module, "convert_live_run", explode)
    summary = run_daily(
        config(tmp_path), credentials=credentials(tmp_path), captures={"slack": slack_capture}
    )
    result = source(summary, "slack")

    assert summary["exit_code"] == EXIT_DOWNSTREAM_FAILED
    assert result["status"] == "degraded"
    assert stage(result, "capture")["status"] == "ok"
    assert stage(result, "ledger")["status"] == "failed"
    assert Path(result["manifest"]).is_file(), "the raw capture remains usable and re-convertible"


def test_a_capture_stays_successful_when_the_database_stage_fails(tmp_path: Path, monkeypatch) -> None:
    import rlwrld_worklog.ledger.load as load_module

    def explode(**_kwargs):
        raise RuntimeError("synthetic database outage")

    monkeypatch.setattr(load_module, "load_source", explode)
    summary = run_daily(
        config(tmp_path, load_database=True, database_url="postgresql://fake"),
        credentials=credentials(tmp_path),
        captures={"slack": slack_capture},
    )
    result = source(summary, "slack")

    assert summary["exit_code"] == EXIT_DOWNSTREAM_FAILED
    assert result["status"] == "degraded"
    assert stage(result, "capture")["status"] == "ok"
    assert stage(result, "ledger")["status"] == "ok"
    assert stage(result, "load")["status"] == "failed"


def test_a_missing_database_url_degrades_the_run_instead_of_passing_it(tmp_path: Path) -> None:
    """No DATABASE_URL is an accident, and an accident must not report `ok`.

    `--no-database` is a choice and stays `ok` (the test that follows this
    one). A missing URL
    is the systemd unit losing its environment -- which happened on the night
    of 2026-09-10 KST: every load was skipped and the run still said `ok`, so
    the screen said the data was in the database when none of it was. Degraded
    exits nonzero, which is what makes the miss visible the same night.
    """
    summary = run_daily(
        config(tmp_path, load_database=True, database_url=None),
        credentials=credentials(tmp_path),
        captures={"slack": slack_capture},
    )
    result = source(summary, "slack")
    assert result["status"] == "degraded"
    assert summary["exit_code"] == EXIT_DOWNSTREAM_FAILED
    assert stage(result, "load")["status"] == "skipped"
    assert stage(result, "load")["detail"]["reason"] == "no database url configured"


def test_choosing_no_database_keeps_the_run_ok(tmp_path: Path) -> None:
    summary = run_daily(
        config(tmp_path, load_database=False, database_url=None),
        credentials=credentials(tmp_path),
        captures={"slack": slack_capture},
    )
    result = source(summary, "slack")
    assert result["status"] == "ok"
    assert stage(result, "load")["status"] == "skipped"
    assert stage(result, "load")["detail"]["reason"] == "database loading disabled"


def test_the_load_stage_runs_in_dry_run_mode_for_a_dry_run(tmp_path: Path, monkeypatch) -> None:
    import rlwrld_worklog.ledger.load as load_module

    seen: dict = {}

    def record(**kwargs):
        seen.update(kwargs)
        return load_module.LoadResult(source=kwargs["source"], dry_run=kwargs["dry_run"])

    monkeypatch.setattr(load_module, "load_source", record)
    run_daily(
        config(tmp_path, load_database=True, database_url="postgresql://fake", dry_run=True),
        credentials=credentials(tmp_path),
        captures={"slack": slack_capture},
    )
    assert seen["dry_run"] is True


# --------------------------------------------------------- dry run / smoke


def test_a_dry_run_advances_no_checkpoint(tmp_path: Path) -> None:
    run_config = config(tmp_path, dry_run=True)
    summary = run_daily(run_config, credentials=credentials(tmp_path), captures=ALL_CAPTURES)

    assert summary["capture_density"] == "dry-run"
    for name in ("slack", "google-calendar", "github", "slurm", "notion"):
        assert source(summary, name)["checkpoint_advanced"] is False
    manifests = run_config.archive_root / "manifests"
    assert not list(manifests.glob("*/test/checkpoint.json"))


def test_smoke_implies_dry_run_and_bounds_the_capture(tmp_path: Path) -> None:
    run_config = config(tmp_path, smoke=True)
    assert run_config.dry_run is True, "a smoke run can never move a production checkpoint"
    assert run_config.capture_density == "smoke"

    summary = run_daily(run_config, credentials=credentials(tmp_path), captures=ALL_CAPTURES)
    assert summary["smoke"] is True
    assert all(item["checkpoint_advanced"] is False for item in summary["sources"])


def test_a_real_run_does_advance_checkpoints(tmp_path: Path) -> None:
    summary = run_daily(config(tmp_path), credentials=credentials(tmp_path), captures=ALL_CAPTURES)
    assert all(item["checkpoint_advanced"] is True for item in summary["sources"])


# --------------------------------------------------------------- locking


def test_an_overlapping_run_is_refused_rather_than_run_twice(tmp_path: Path) -> None:
    run_config = config(tmp_path)
    handle = daily._acquire_lock(run_config.resolved_lock_path())
    assert handle is not None
    try:
        summary = run_daily(run_config, credentials=credentials(tmp_path), captures=ALL_CAPTURES)
    finally:
        daily._release_lock(handle)

    assert summary["status"] == "locked"
    assert summary["exit_code"] == EXIT_LOCKED
    assert summary["sources"] == []


def test_the_lock_is_released_even_when_a_source_raises(tmp_path: Path) -> None:
    def hard_failure(config_value: DailyConfig, _credentials: Credentials) -> CaptureOutcome:
        raise RuntimeError("synthetic")

    run_config = config(tmp_path)
    run_daily(run_config, credentials=credentials(tmp_path), captures={"slack": hard_failure})

    handle = daily._acquire_lock(run_config.resolved_lock_path())
    assert handle is not None, "the lock must not survive the run"
    daily._release_lock(handle)


# ----------------------------------------------------------- credentials


def test_admin_managed_files_win_over_environment_variables(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "config"
    (root / "credentials").mkdir(parents=True)
    (root / "credentials" / "slack-token").write_text("xoxp-from-backoffice\n", encoding="utf-8")
    (root / "credentials" / "notion-token").write_text("ntn_from-backoffice\n", encoding="utf-8")
    (root / "credentials" / "github-token").write_text("ghp_from-backoffice\n", encoding="utf-8")
    (root / "credentials" / "google-token.json").write_text("{}", encoding="utf-8")
    (root / "settings.json").write_text(
        json.dumps(
            {"slack_expected_team_id": "T0FROMSETTINGS", "github_organization": "org-from-settings"}
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("SLACK_USER_TOKEN", "xoxp-stale-shell-export")
    monkeypatch.setenv("NOTION_TOKEN", "ntn_stale")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_stale")
    monkeypatch.setenv("GITHUB_ORG", "org-from-stale-shell-export")

    resolved = daily.load_credentials(root)

    assert resolved.slack_token == "xoxp-from-backoffice"
    assert resolved.notion_token == "ntn_from-backoffice"
    assert resolved.github_token == "ghp_from-backoffice"
    assert resolved.google_token_path == root / "credentials" / "google-token.json"
    assert resolved.slack_expected_team_id == "T0FROMSETTINGS"
    assert resolved.github_organization == "org-from-settings"
    assert resolved.availability() == {
        "slack": True,
        "notion": True,
        "google-calendar": True,
        "github": True,
        "slurm": True,
    }


def test_missing_credentials_are_reported_not_invented(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("SLACK_USER_TOKEN", raising=False)
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GOOGLE_TOKEN_PATH", raising=False)
    resolved = daily.load_credentials(tmp_path / "absent")

    assert resolved.slack_token is None
    assert resolved.github_token is None
    assert resolved.availability() == {
        "slack": False,
        "notion": False,
        "google-calendar": False,
        "github": False,
        # Slurm asks the tailnet for an unauthenticated dump, so there is no
        # credential that can be missing.
        "slurm": True,
    }


def test_the_summary_never_contains_credential_material(tmp_path: Path) -> None:
    summary = run_daily(config(tmp_path), credentials=credentials(tmp_path), captures=ALL_CAPTURES)
    text = json.dumps(summary)
    assert "xoxp-synthetic" not in text
    assert "ntn_synthetic" not in text


def test_an_error_message_carrying_a_token_is_redacted(tmp_path: Path) -> None:
    def leaky(config_value: DailyConfig, _credentials: Credentials) -> CaptureOutcome:
        raise RuntimeError("auth failed for xoxp-1234567890-secret-material")

    summary = run_daily(config(tmp_path), credentials=credentials(tmp_path), captures={"slack": leaky})
    text = json.dumps(summary)
    assert "1234567890-secret-material" not in text
    assert "xoxp-<redacted>" in text


def test_unknown_sources_are_named_rather_than_silently_dropped(tmp_path: Path) -> None:
    summary = run_daily(
        config(tmp_path, sources=("slack", "jira")),
        credentials=credentials(tmp_path),
        captures=ALL_CAPTURES,
    )
    assert summary["unknown_sources"] == ["jira"]
    assert [item["source"] for item in summary["sources"]] == ["slack"]


@pytest.mark.parametrize("stage_name", ["capture", "ledger"])
def test_every_source_reports_each_stage_it_reached(tmp_path: Path, stage_name: str) -> None:
    summary = run_daily(config(tmp_path), credentials=credentials(tmp_path), captures=ALL_CAPTURES)
    for item in summary["sources"]:
        assert stage(item, stage_name)["status"] == "ok"


# ----------------------------------------------------------------- the CLI


def test_the_cli_passes_its_flags_through_and_returns_the_exit_code(tmp_path: Path, monkeypatch, capsys) -> None:
    from rlwrld_worklog import cli

    seen: dict = {}

    def fake_run(run_config, **kwargs):
        seen["config"] = run_config
        return {"status": "degraded", "exit_code": EXIT_DOWNSTREAM_FAILED, "sources": []}

    monkeypatch.setattr(daily, "run_daily", fake_run)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    code = cli.main(
        [
            "daily-collect",
            "--source",
            "notion",
            "--environment",
            "test",
            "--since",
            "48h",
            "--archive-root",
            str(tmp_path / "archive"),
            "--ledger-root",
            str(tmp_path / "staging"),
            "--no-database",
            "--smoke",
        ]
    )

    assert code == EXIT_DOWNSTREAM_FAILED
    assert seen["config"].sources == ("notion",)
    assert seen["config"].since == "48h"
    assert seen["config"].smoke is True
    assert seen["config"].dry_run is True
    assert seen["config"].load_database is False
    printed = capsys.readouterr().out
    assert printed.startswith("daily_collect_config=")
    assert "daily_collect=" in printed


def test_the_cli_accepts_every_source_the_runner_can_order(tmp_path: Path, monkeypatch) -> None:
    """`--source` and `SOURCE_ORDER` cannot drift: a source the runner knows
    and the parser refuses is a source unreachable from the nightly timer."""
    from rlwrld_worklog import cli

    seen: dict = {}

    def fake_run(run_config, **kwargs):
        seen["config"] = run_config
        return {"exit_code": EXIT_OK}

    monkeypatch.setattr(daily, "run_daily", fake_run)
    arguments = ["daily-collect", "--environment", "test", "--archive-root", str(tmp_path)]
    for name in daily.SOURCE_ORDER:
        arguments += ["--source", name]

    cli.main(arguments)

    assert seen["config"].sources == daily.SOURCE_ORDER


def test_the_cli_config_line_never_prints_a_database_url(tmp_path: Path, monkeypatch, capsys) -> None:
    from rlwrld_worklog import cli
    from rlwrld_worklog.daily import config_as_dict

    monkeypatch.setattr(daily, "run_daily", lambda run_config, **kwargs: {"exit_code": EXIT_OK})
    monkeypatch.setenv("DATABASE_URL", "postgresql://worklog:super-secret@localhost/worklog")

    cli.main(["daily-collect", "--archive-root", str(tmp_path / "archive"), "--environment", "test"])

    printed = capsys.readouterr().out
    assert "super-secret" not in printed
    assert "database_url" not in config_as_dict(config(tmp_path))


def test_the_default_ledger_root_sits_beside_the_archive(tmp_path: Path, monkeypatch) -> None:
    from rlwrld_worklog import cli

    seen: dict = {}

    def fake_run(run_config, **kwargs):
        seen["config"] = run_config
        return {"exit_code": EXIT_OK}

    monkeypatch.setattr(daily, "run_daily", fake_run)
    monkeypatch.delenv("LEDGER_ROOT", raising=False)

    cli.main(["daily-collect", "--archive-root", str(tmp_path / "archive"), "--environment", "test"])

    assert seen["config"].ledger_root == tmp_path / "archive" / "staging" / "ledger"


# ------------------------------------------- the link queue in a dry run


def seed_link_queue(archive_root: Path, url: str) -> bytes:
    from rlwrld_worklog.link_queue import NotionLinkQueue

    queue = NotionLinkQueue(archive_root, "test")
    queue.add_urls([url], source="slack", run_id="earlier-run")
    return queue.path.read_bytes()


def queue_path(run_config: DailyConfig) -> Path:
    return run_config.archive_root / "manifests/notion/test/link-queue.json"


def test_a_dry_run_orchestration_leaves_the_link_queue_untouched(tmp_path: Path) -> None:
    """The whole promise of --dry-run: no production state moves. The queue is
    production state, because an entry marked fetched is never offered again."""
    run_config = config(tmp_path, dry_run=True)
    url = "https://www.notion.so/Meeting-0123456789abcdef0123456789abcdef"
    before = seed_link_queue(run_config.archive_root, url)

    summary = run_daily(run_config, credentials=credentials(tmp_path), captures=ALL_CAPTURES)

    assert queue_path(run_config).read_bytes() == before
    assert json.loads(queue_path(run_config).read_text())["items"][0]["status"] == "pending"
    calendar_detail = stage(source(summary, "google-calendar"), "capture")
    assert calendar_detail["detail"]["notion_links_persisted"] is False
    assert source(summary, "notion")["status"] == "ok", "the run still succeeds and reports"


def test_a_dry_run_orchestration_creates_no_link_queue(tmp_path: Path) -> None:
    run_config = config(tmp_path, dry_run=True)
    run_daily(run_config, credentials=credentials(tmp_path), captures=ALL_CAPTURES)
    assert not queue_path(run_config).exists()


def test_a_smoke_run_leaves_the_link_queue_untouched(tmp_path: Path) -> None:
    run_config = config(tmp_path, smoke=True)
    url = "https://www.notion.so/Meeting-0123456789abcdef0123456789abcdef"
    before = seed_link_queue(run_config.archive_root, url)

    run_daily(run_config, credentials=credentials(tmp_path), captures=ALL_CAPTURES)

    assert queue_path(run_config).read_bytes() == before


def test_a_real_run_does_persist_its_discoveries(tmp_path: Path) -> None:
    run_config = config(tmp_path)
    summary = run_daily(run_config, credentials=credentials(tmp_path), captures=ALL_CAPTURES)

    calendar_detail = stage(source(summary, "google-calendar"), "capture")
    assert calendar_detail["detail"]["notion_links_persisted"] is True
    assert queue_path(run_config).exists()


def test_smoke_bounds_the_notion_comment_sweep_but_production_does_not(tmp_path: Path, monkeypatch) -> None:
    from rlwrld_worklog import notion_collector as collector_module
    from rlwrld_worklog.daily import SMOKE_LIMITS, capture_notion

    seen: list = []

    class Recorder:
        def collect(self, **kwargs):
            seen.append(kwargs["comment_request_budget"])
            raise CaptureFailed(self.archive, RuntimeError("stop after recording"))

    def fake_factory(
        *, token, archive_root, environment, capture_density, dry_run, config_root=None
    ):
        from rlwrld_worklog.archive import RawArchive

        recorder = Recorder()
        recorder.archive = RawArchive(
            archive_root, "notion", f"n-{capture_density}", environment, config_root=config_root
        )
        return recorder.archive, recorder

    monkeypatch.setattr(collector_module, "make_notion_collector", fake_factory)

    for run_config in (config(tmp_path), config(tmp_path, smoke=True)):
        try:
            capture_notion(run_config, credentials(tmp_path))
        except CaptureFailed:
            pass

    assert seen == [None, SMOKE_LIMITS["notion_comment_request_budget"]], (
        "production is exhaustive; smoke is explicitly bounded"
    )


def test_an_unresolved_notion_object_degrades_the_source_not_just_the_manifest(
    tmp_path: Path,
) -> None:
    """A run summary must never look cleaner than the manifest behind it."""
    def degraded_notion(config_value: DailyConfig, _credentials: Credentials) -> CaptureOutcome:
        client = FakeNotionClient(
            search_results=[
                {"object": "page", "id": PAGE_ID, "last_edited_time": "2026-08-26T01:00:00Z"},
                {"object": "page", "id": SECOND_PAGE_ID, "last_edited_time": "2026-08-26T02:00:00Z"},
            ],
            pages={PAGE_ID: page_body(PAGE_ID)},
            page_errors={SECOND_PAGE_ID: exhausted()},
        )
        return _notion_capture_with(client, config_value)

    summary = run_daily(
        config(tmp_path),
        credentials=credentials(tmp_path),
        captures={**ALL_CAPTURES, "notion": degraded_notion},
    )

    result = source(summary, "notion")
    assert result["status"] == "degraded"
    assert "1 object(s) unresolved" in result["reason"]
    assert stage(result, "capture")["status"] == "ok", "the capture stage itself did finish"
    assert stage(result, "capture")["detail"]["capture_status"] == "degraded"
    assert stage(result, "capture")["detail"]["pages_collected"] == 1, "the healthy page still landed"
    assert source(summary, "slack")["status"] == "ok", "the other sources are unaffected"
    manifest = json.loads(Path(result["manifest"]).read_text())
    assert manifest["status"] == "degraded"


def test_a_notion_run_with_only_permanent_skips_is_not_degraded(tmp_path: Path) -> None:
    """A deleted page is a complete observation, not an unknown."""
    def skipping_notion(config_value: DailyConfig, _credentials: Credentials) -> CaptureOutcome:
        client = FakeNotionClient(
            search_results=[
                {"object": "page", "id": PAGE_ID, "last_edited_time": "2026-08-26T01:00:00Z"},
                {"object": "page", "id": SECOND_PAGE_ID, "last_edited_time": "2026-08-26T02:00:00Z"},
            ],
            pages={PAGE_ID: page_body(PAGE_ID)},
        )
        return _notion_capture_with(client, config_value)

    summary = run_daily(
        config(tmp_path),
        credentials=credentials(tmp_path),
        captures={**ALL_CAPTURES, "notion": skipping_notion},
    )

    result = source(summary, "notion")
    assert result["status"] == "ok"
    assert result["reason"] is None
    assert json.loads(Path(result["manifest"]).read_text())["status"] == "success_with_skips"


def test_every_collector_factory_publishes_progress_under_the_configured_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--config-root` reaches the snapshot without an environment variable."""
    import inspect

    from rlwrld_worklog.calendar_collector import make_calendar_collector
    from rlwrld_worklog.notion_collector import make_notion_collector
    from rlwrld_worklog.slack_collector import make_slack_collector

    monkeypatch.delenv("APP_CONFIG_ROOT", raising=False)
    monkeypatch.delenv("COLLECTION_PROGRESS_ROOT", raising=False)
    config_root = tmp_path / "config"
    archive_root = tmp_path / "archive"

    for factory in (make_slack_collector, make_notion_collector, make_calendar_collector):
        assert "config_root" in inspect.signature(factory).parameters

    _, slack_archive, _ = make_slack_collector(
        archive_root=archive_root,
        environment="test",
        token="xoxp-synthetic-not-a-real-token",
        config_root=config_root,
    )
    notion_archive, _ = make_notion_collector(
        token="ntn_synthetic",
        archive_root=archive_root,
        environment="test",
        config_root=config_root,
    )
    for archive in (slack_archive, notion_archive):
        assert archive.progress.enabled is True
        assert archive.progress.path is not None
        assert archive.progress.path.is_relative_to(config_root / "collection-status")
        assert archive.progress.path.is_file()

    # Without a config root, and with none in the environment, a run publishes
    # nothing rather than writing into the operator's home directory.
    _, bare, _ = make_slack_collector(
        archive_root=archive_root, environment="test", token="xoxp-synthetic-not-a-real-token"
    )
    assert bare.progress.enabled is False


def test_the_daily_run_hands_the_collector_the_seeds_the_backoffice_holds(
    tmp_path: Path, monkeypatch
) -> None:
    """The seed list is only insurance if the run that needs it actually reads it.

    A seed added in the backoffice and never passed to the collector would be
    a list an operator maintains for nothing -- and it would look maintained,
    which is worse than absent.
    """
    import rlwrld_worklog.notion_collector as collector_module
    from rlwrld_worklog.admin_store import AdminStore
    from rlwrld_worklog.daily import capture_notion

    config_root = tmp_path / "config"
    store = AdminStore(config_root)
    entry = store.add_notion_seed("2e1d872b594c817fb0650002b5686f9e", actor="hk")

    seen: list[tuple] = []

    class Recorder:
        def collect(self, **kwargs):
            seen.append(tuple(kwargs.get("seed_pages") or ()))
            raise CaptureFailed(self.archive, RuntimeError("stop after recording"))

    def fake_factory(
        *, token, archive_root, environment, capture_density, dry_run, config_root=None
    ):
        from rlwrld_worklog.archive import RawArchive

        recorder = Recorder()
        recorder.archive = RawArchive(
            archive_root, "notion", f"seed-{capture_density}", environment, config_root=config_root
        )
        return recorder.archive, recorder

    monkeypatch.setattr(collector_module, "make_notion_collector", fake_factory)

    for run_config in (
        config(tmp_path, config_root=config_root),
        config(tmp_path, config_root=config_root, smoke=True),
    ):
        try:
            capture_notion(run_config, credentials(tmp_path))
        except CaptureFailed:
            pass

    assert seen == [(entry["page_id"],), ()], (
        "a production run carries the configured seeds; a smoke run is bounded "
        "by construction and must not inherit a list of unknown length"
    )


def test_an_index_failure_degrades_the_run_but_keeps_the_load(tmp_path: Path, monkeypatch) -> None:
    """The corpus falling behind is visible, without undoing the night's load."""
    import rlwrld_worklog.ledger.extract_text as extract_module
    import rlwrld_worklog.ledger.load as load_module

    monkeypatch.setattr(
        load_module,
        "load_source",
        lambda **kwargs: load_module.LoadResult(source=kwargs["source"], dry_run=kwargs["dry_run"]),
    )

    def explode(*_args, **_kwargs):
        raise RuntimeError("synthetic extractor outage")

    monkeypatch.setattr(extract_module, "index_ledger_text", explode)
    summary = run_daily(
        config(tmp_path, load_database=True, database_url="postgresql://fake"),
        credentials=credentials(tmp_path),
        captures={"slack": slack_capture},
    )
    result = source(summary, "slack")
    assert result["status"] == "degraded"
    assert stage(result, "load")["status"] == "ok"
    assert stage(result, "index")["status"] == "failed"
    assert summary["exit_code"] == EXIT_DOWNSTREAM_FAILED
