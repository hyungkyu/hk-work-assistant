"""What this product schedules, and what is actually true about it right now.

Two kinds of fact live here and they are kept apart on purpose:

  * **The catalogue** describes batches this repository defines -- name,
    purpose, who runs them, cadence, scope, how overlap is prevented, where the
    logs are. These are properties of the code and are safe to state.
  * **The state** -- installed, enabled, active, when it last ran, when it runs
    next -- is never stated by this module. It is read from the operator's own
    settings and from systemd, and when it cannot be read the answer is
    ``unknown`` with a reason. A schedule page that invents a green light is
    worse than one that admits it cannot see.

Nothing here installs, enables, starts, stops or writes anything. The only
external call is ``systemctl show``, which is read-only, is given a unit name
taken from the catalogue rather than from a caller, runs without a shell, and
is bounded by a timeout.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

SCHEDULE_SCHEMA_VERSION = 1

# systemd properties worth showing. All are scalar and non-secret.
_UNIT_PROPERTIES = (
    "LoadState",
    "ActiveState",
    "SubState",
    "UnitFileState",
    "NextElapseUSecRealtime",
    "LastTriggerUSec",
    "Result",
    "ExecMainStatus",
)
_SYSTEMCTL_TIMEOUT_SECONDS = 3.0


@dataclass(frozen=True)
class Batch:
    key: str
    name: str
    purpose: str
    runner: str
    command: str
    timer_unit: str
    service_unit: str
    cadence: str
    schedule_description: str
    scope: str
    concurrency: str
    logs: str
    failure_check: str
    detail: str
    # Settings that decide when it runs, so the page can show the operator's
    # own configured time rather than a number baked into the page.
    hour_setting: str | None = None
    timezone_setting: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "purpose": self.purpose,
            "runner": self.runner,
            "command": self.command,
            "timer_unit": self.timer_unit,
            "service_unit": self.service_unit,
            "cadence": self.cadence,
            "schedule_description": self.schedule_description,
            "scope": self.scope,
            "concurrency": self.concurrency,
            "logs": self.logs,
            "failure_check": self.failure_check,
            "detail": self.detail,
        }


CATALOGUE: tuple[Batch, ...] = (
    Batch(
        key="daily-collect",
        name="일일 수집 (daily collect)",
        purpose=(
            "Slack · Google Calendar · GitHub · Slurm · Notion을 공식 읽기 전용 API로 증분 "
            "수집해 불변 원본 아카이브와 실행 manifest를 남기고, 표준 v1 원장으로 투영한다."
        ),
        runner="systemd user timer → oneshot service (사람의 수동 실행이 아님)",
        command="scripts/run-logged.sh daily-collect -- worklog daily-collect",
        # 이 저장소의 deploy/systemd/ 에 있는 유닛이다. 앞선 판은 시스템 유닛
        # 디렉터리에 있어 개발계에서 읽을 수도 고칠 수도 없었고, 그래서 다섯 중
        # 두 소스만 돌고 있다는 사실이 코드보다 오래 살아남았다.
        timer_unit="hkwa-collect.timer",
        service_unit="hkwa-collect.service",
        cadence="하루 1회",
        schedule_description=(
            "유닛의 OnCalendar 는 01:00 Asia/Seoul 로, 시간대를 가정하지 않고 이름으로 "
            "박아 두었다. 백오피스 설정의 '일일 수집 시작 시각'은 참고값이며, systemd에 "
            "설치된 OnCalendar 가 실제 실행 시각을 결정한다 — 둘이 다르면 systemd 쪽이 사실이다."
        ),
        scope=(
            "환경 production, 소스 다섯 개 전부(slack · google-calendar · github · slurm · "
            "notion). 각 소스는 자기 checkpoint 이후분만 읽고, 기본 요청 창은 26시간이다. "
            "GitHub 과 Slurm 은 그 26시간이 걸치는 KST 날짜 구간으로 환산해 읽는다."
        ),
        concurrency=(
            "<RAW_ARCHIVE_ROOT>/locks/daily-collect-<environment>.lock 에 비차단 flock. "
            "이전 실행이 아직 돌고 있으면 두 번째 실행은 아무것도 건드리지 않고 exit 3."
        ),
        logs=(
            "<RAW_ARCHIVE_ROOT>/logs/daily-collect/ 의 timestamped.log · latest.log · "
            "last.json(종료 코드와 꼬리). journalctl --user -u hkwa-collect.service 도 있다. "
            "실행별 manifest는 <RAW_ARCHIVE_ROOT>/manifests/<source>/<environment>/<run_id>.json"
        ),
        failure_check=(
            "백오피스 '수집 현황'의 최근 실행 표에서 상태·skip·failure 수를 먼저 본다. "
            "종료 코드는 0 정상, 1 수집 실패, 2 후속 단계 실패, 3 잠금 충돌."
        ),
        detail=(
            "소스별로 격리되어 한 소스의 실패가 다른 소스를 막지 않는다. checkpoint는 "
            "완전하고 잘리지 않은 실행에서만 전진하며, 실패한 실행도 status=failed manifest를 "
            "남긴다. 모든 manifest에 적용된 수집 규칙 버전과 digest가 기록된다."
        ),
        hour_setting="daily_collection_hour",
        timezone_setting="timezone",
    ),
)

_ALLOWED_UNITS = frozenset(
    unit for batch in CATALOGUE for unit in (batch.timer_unit, batch.service_unit)
)


def _unavailable(reason: str) -> dict[str, Any]:
    return {"available": False, "reason": reason, "properties": {}}


def read_unit(unit: str) -> dict[str, Any]:
    """One unit's state from systemd, or an explicit reason it is unknown.

    ``unit`` must already be a catalogue unit name; it never comes from a
    request. The call takes no shell, so nothing in the name could be
    interpreted even if that guarantee were ever weakened.
    """
    if unit not in _ALLOWED_UNITS:
        return _unavailable("이 배치 목록에 없는 unit 이름입니다")
    systemctl = shutil.which("systemctl")
    if systemctl is None:
        return _unavailable("이 실행 환경에는 systemctl이 없습니다 (컨테이너이거나 미설치)")
    arguments = [systemctl, "show", "--no-pager"]
    arguments += [f"--property={name}" for name in _UNIT_PROPERTIES]
    arguments += ["--", unit]
    try:
        completed = subprocess.run(
            arguments,
            capture_output=True,
            text=True,
            timeout=_SYSTEMCTL_TIMEOUT_SECONDS,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return _unavailable(f"systemctl을 실행할 수 없습니다 ({type(error).__name__})")
    if completed.returncode != 0:
        return _unavailable(f"systemctl이 종료 코드 {completed.returncode}로 응답했습니다")
    properties: dict[str, str] = {}
    for line in completed.stdout.splitlines():
        name, separator, value = line.partition("=")
        if separator and name in _UNIT_PROPERTIES:
            properties[name] = value
    if not properties:
        return _unavailable("systemctl이 이 unit에 대해 아무 속성도 반환하지 않았습니다")
    return {"available": True, "reason": None, "properties": properties}


def _usec_to_iso(value: str | None) -> str | None:
    """A systemd microsecond stamp, or None when it means 'never'."""
    if not value or not value.isdigit():
        return None
    microseconds = int(value)
    # systemd writes 0 for "never happened" and UINT64_MAX for "no next run".
    if microseconds == 0 or microseconds >= 2**63:
        return None
    try:
        return datetime.fromtimestamp(microseconds / 1_000_000, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _installation(unit_state: Mapping[str, Any]) -> dict[str, Any]:
    """Installed / enabled / active, or unknown -- never assumed."""
    if not unit_state.get("available"):
        return {
            "installed": None,
            "enabled": None,
            "active": None,
            "state_label": "불명",
            "reason": unit_state.get("reason"),
        }
    properties = unit_state["properties"]
    load_state = properties.get("LoadState")
    file_state = properties.get("UnitFileState")
    active_state = properties.get("ActiveState")
    installed = load_state not in {None, "not-found", "masked"}
    if not installed:
        label = "미설치" if load_state == "not-found" else "차단됨 (masked)"
    elif file_state in {"enabled", "enabled-runtime"} and active_state == "active":
        label = "활성"
    elif file_state in {"disabled", "masked"}:
        label = "설치됨 · 비활성"
    elif active_state in {"failed"}:
        label = "설치됨 · 실패 상태"
    else:
        label = f"설치됨 · {active_state or '상태 불명'}"
    return {
        "installed": installed,
        "enabled": None if not installed else file_state in {"enabled", "enabled-runtime"},
        "active": None if not installed else active_state == "active",
        "state_label": label,
        "reason": None,
    }


def next_run_from_settings(
    settings: Mapping[str, Any], batch: Batch, *, now: datetime | None = None
) -> dict[str, Any]:
    """The next run the *configured* hour implies. Explicitly not authoritative.

    systemd, not this calculation, decides when a timer fires. This exists so
    an operator can see what the backoffice settings ask for and compare it
    against what systemd reports.
    """
    if batch.hour_setting is None or batch.timezone_setting is None:
        return {"at": None, "timezone": None, "hour": None, "reason": "이 배치는 시각 설정이 없습니다"}
    hour = settings.get(batch.hour_setting)
    zone_name = settings.get(batch.timezone_setting)
    if not isinstance(hour, int) or isinstance(hour, bool) or not 0 <= hour <= 23:
        return {"at": None, "timezone": None, "hour": None, "reason": "설정의 실행 시각이 올바르지 않습니다"}
    try:
        zone = ZoneInfo(str(zone_name))
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        return {
            "at": None,
            "timezone": str(zone_name),
            "hour": hour,
            "reason": "설정의 타임존을 해석할 수 없습니다",
        }
    moment = (now or datetime.now(timezone.utc)).astimezone(zone)
    candidate = moment.replace(hour=hour, minute=0, second=0, microsecond=0)
    if candidate <= moment:
        candidate += timedelta(days=1)
    return {"at": candidate.isoformat(), "timezone": str(zone_name), "hour": hour, "reason": None}


def describe(
    settings: Mapping[str, Any],
    *,
    now: datetime | None = None,
    read_unit_state: Any = read_unit,
) -> dict[str, Any]:
    """The whole schedule page payload: catalogue plus whatever state is real."""
    batches: list[dict[str, Any]] = []
    for batch in CATALOGUE:
        timer = read_unit_state(batch.timer_unit)
        service = read_unit_state(batch.service_unit)
        installation = _installation(timer)
        timer_properties = timer.get("properties", {}) if timer.get("available") else {}
        service_properties = service.get("properties", {}) if service.get("available") else {}
        next_from_systemd = _usec_to_iso(timer_properties.get("NextElapseUSecRealtime"))
        entry = batch.as_dict()
        entry.update(
            {
                "installation": installation,
                "next_run": {
                    # systemd is the only authority on when a timer fires.
                    "at": next_from_systemd,
                    "source": "systemd" if next_from_systemd else None,
                    "authoritative": bool(next_from_systemd),
                    "unknown_reason": None
                    if next_from_systemd
                    else (timer.get("reason") or "systemd가 다음 실행 시각을 보고하지 않았습니다"),
                },
                "configured_next_run": next_run_from_settings(settings, batch, now=now),
                "last_trigger_at": _usec_to_iso(timer_properties.get("LastTriggerUSec")),
                "last_result": service_properties.get("Result"),
                "last_exit_status": service_properties.get("ExecMainStatus"),
                "timer_state": timer,
                "service_state": service,
            }
        )
        batches.append(entry)
    unavailable = [batch for batch in batches if not batch["timer_state"]["available"]]
    return {
        "schema_version": SCHEDULE_SCHEMA_VERSION,
        "generated_at": (now or datetime.now(timezone.utc)).isoformat(),
        "systemd_readable": len(unavailable) < len(batches) if batches else False,
        "systemd_unavailable_reason": unavailable[0]["timer_state"]["reason"] if unavailable else None,
        "settings_timezone": settings.get("timezone"),
        "archive_root": os.environ.get("RAW_ARCHIVE_ROOT", "/data/rlwrld-worklog"),
        "batches": batches,
    }
