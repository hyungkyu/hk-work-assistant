"""The schedule page states facts about the code and reads state; never invents it."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException

from rlwrld_worklog import admin_web, schedule_web, schedules
from rlwrld_worklog.schedules import CATALOGUE, describe, next_run_from_settings, read_unit

NOW = datetime(2026, 9, 2, 3, 0, tzinfo=timezone.utc)  # 12:00 KST
SETTINGS = {"daily_collection_hour": 3, "timezone": "Asia/Seoul"}


def unavailable(_unit: str) -> dict[str, Any]:
    return {"available": False, "reason": "systemctl이 없습니다", "properties": {}}


def installed(**properties: str):
    def read(_unit: str) -> dict[str, Any]:
        return {"available": True, "reason": None, "properties": dict(properties)}

    return read


def test_the_catalogue_only_describes_batches_this_repository_defines() -> None:
    assert [batch.key for batch in CATALOGUE] == ["daily-collect"]
    batch = CATALOGUE[0]
    # The command names the wrapper, not just the collector: the unit runs
    # through run-logged.sh so the run's output lands under the data disk
    # rather than wherever the caller redirected it.
    assert batch.command == "scripts/run-logged.sh daily-collect -- worklog daily-collect"
    for field in (batch.purpose, batch.runner, batch.cadence, batch.scope,
                  batch.concurrency, batch.logs, batch.failure_check, batch.detail):
        assert field.strip()
    assert "flock" in batch.concurrency and "journalctl" in batch.logs


def test_the_payload_carries_every_field_the_page_promises() -> None:
    batch = describe(SETTINGS, now=NOW, read_unit_state=unavailable)["batches"][0]
    for key in ("name", "purpose", "runner", "cadence", "schedule_description", "scope",
                "concurrency", "logs", "failure_check", "detail", "installation",
                "next_run", "configured_next_run", "last_trigger_at"):
        assert key in batch


def test_an_unreadable_systemd_reports_unknown_rather_than_a_green_light() -> None:
    payload = describe(SETTINGS, now=NOW, read_unit_state=unavailable)
    assert payload["systemd_readable"] is False
    assert payload["systemd_unavailable_reason"] == "systemctl이 없습니다"
    batch = payload["batches"][0]
    assert batch["installation"] == {
        "installed": None, "enabled": None, "active": None,
        "state_label": "불명", "reason": "systemctl이 없습니다",
    }
    assert batch["next_run"]["at"] is None
    assert batch["next_run"]["authoritative"] is False
    assert batch["next_run"]["unknown_reason"]
    assert batch["last_trigger_at"] is None
    assert batch["last_result"] is None


def test_a_unit_that_is_not_installed_says_so() -> None:
    payload = describe(SETTINGS, now=NOW, read_unit_state=installed(LoadState="not-found"))
    batch = payload["batches"][0]
    assert batch["installation"]["installed"] is False
    assert batch["installation"]["state_label"] == "미설치"
    assert batch["next_run"]["at"] is None


def test_an_installed_active_timer_reports_systemd_as_the_authority() -> None:
    read = installed(
        LoadState="loaded",
        ActiveState="active",
        UnitFileState="enabled",
        NextElapseUSecRealtime=str(int(datetime(2026, 9, 3, 18, 0, tzinfo=timezone.utc).timestamp() * 1_000_000)),
        LastTriggerUSec=str(int(datetime(2026, 9, 1, 18, 0, tzinfo=timezone.utc).timestamp() * 1_000_000)),
        Result="success",
        ExecMainStatus="0",
    )
    batch = describe(SETTINGS, now=NOW, read_unit_state=read)["batches"][0]
    assert batch["installation"] == {
        "installed": True, "enabled": True, "active": True, "state_label": "활성", "reason": None,
    }
    assert batch["next_run"]["at"] == "2026-09-03T18:00:00+00:00"
    assert batch["next_run"]["source"] == "systemd"
    assert batch["next_run"]["authoritative"] is True
    assert batch["last_trigger_at"] == "2026-09-01T18:00:00+00:00"
    assert batch["last_result"] == "success"
    # The settings-derived time is still shown, and still marked non-authoritative.
    assert batch["configured_next_run"]["at"] == "2026-09-03T03:00:00+09:00"


@pytest.mark.parametrize("value", ["0", str(2**64 - 1), "", None, "not-a-number"])
def test_systemd_sentinel_timestamps_are_read_as_never(value: str | None) -> None:
    read = installed(LoadState="loaded", ActiveState="active", UnitFileState="enabled",
                     **({"NextElapseUSecRealtime": value} if value is not None else {}))
    batch = describe(SETTINGS, now=NOW, read_unit_state=read)["batches"][0]
    assert batch["next_run"]["at"] is None
    assert batch["next_run"]["unknown_reason"]


def test_the_configured_next_run_comes_from_settings_and_rolls_to_tomorrow() -> None:
    batch = CATALOGUE[0]
    # 12:00 KST, configured for 03:00 -> tomorrow.
    assert next_run_from_settings(SETTINGS, batch, now=NOW)["at"] == "2026-09-03T03:00:00+09:00"
    # 01:00 KST, configured for 03:00 -> later today.
    earlier = datetime(2026, 9, 1, 16, 0, tzinfo=timezone.utc)
    assert next_run_from_settings(SETTINGS, batch, now=earlier)["at"] == "2026-09-02T03:00:00+09:00"


@pytest.mark.parametrize(
    "settings", [{"daily_collection_hour": 99, "timezone": "Asia/Seoul"},
                 {"daily_collection_hour": 3, "timezone": "Mars/Olympus"},
                 {"daily_collection_hour": None, "timezone": None}],
)
def test_unusable_settings_produce_an_explicit_reason_not_a_guess(settings: dict) -> None:
    result = next_run_from_settings(settings, CATALOGUE[0], now=NOW)
    assert result["at"] is None and result["reason"]


def test_a_unit_name_outside_the_catalogue_is_never_executed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No request can reach systemctl with a name of its own choosing."""
    def explode(*_args: object, **_kwargs: object) -> None:  # pragma: no cover
        raise AssertionError("systemctl must not run for an unlisted unit")

    monkeypatch.setattr(schedules.subprocess, "run", explode)
    for unit in ("evil.service; rm -rf /", "../../etc/passwd", "sshd.service", ""):
        assert read_unit(unit)["available"] is False


def test_systemctl_is_invoked_without_a_shell_and_with_a_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    class Completed:
        returncode = 0
        stdout = "LoadState=loaded\nActiveState=active\n"

    def record(arguments: list[str], **kwargs: Any) -> Completed:
        seen["arguments"] = arguments
        seen["kwargs"] = kwargs
        return Completed()

    monkeypatch.setattr(schedules.shutil, "which", lambda _name: "/usr/bin/systemctl")
    monkeypatch.setattr(schedules.subprocess, "run", record)
    result = read_unit(CATALOGUE[0].timer_unit)
    assert result["available"] is True
    assert result["properties"]["LoadState"] == "loaded"
    assert seen["arguments"][:3] == ["/usr/bin/systemctl", "show", "--no-pager"]
    assert seen["arguments"][-2:] == ["--", CATALOGUE[0].timer_unit]
    assert seen["kwargs"]["shell"] is False
    assert seen["kwargs"]["check"] is False
    assert seen["kwargs"]["timeout"] <= 5


def test_a_missing_systemctl_is_reported_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(schedules.shutil, "which", lambda _name: None)
    result = read_unit(CATALOGUE[0].timer_unit)
    assert result["available"] is False and "systemctl" in result["reason"]


def test_a_systemctl_failure_is_reported_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(schedules.shutil, "which", lambda _name: "/usr/bin/systemctl")

    def boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("no such file")

    monkeypatch.setattr(schedules.subprocess, "run", boom)
    assert read_unit(CATALOGUE[0].timer_unit)["available"] is False


def test_the_schedule_api_is_super_admin_only_and_takes_no_parameters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import inspect

    from rlwrld_worklog.web import app

    # The session check builds an AdminStore, which creates and chmods its own
    # root.  Point it at a temporary directory so the test never touches the
    # operator's real APP_CONFIG_ROOT.
    monkeypatch.setenv("APP_CONFIG_ROOT", str(tmp_path / "config"))
    admin_web.store.cache_clear()
    try:
        parameters = set(inspect.signature(schedule_web.list_schedules).parameters)
        assert parameters == {"request"}, "no unit or path may be supplied by a caller"

        class Anonymous:
            cookies: dict[str, str] = {}
            headers: dict[str, str] = {}

        with pytest.raises(HTTPException) as error:
            schedule_web.list_schedules(Anonymous())
        assert error.value.status_code == 401
        assert admin_web.store().root == tmp_path / "config"

        schema = app.openapi()["paths"]["/api/v1/admin/schedules"]
        assert set(schema) == {"get"}
    finally:
        admin_web.store.cache_clear()
