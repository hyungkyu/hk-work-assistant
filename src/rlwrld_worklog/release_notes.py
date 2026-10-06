"""What has been deployed, and when -- read from what the deploy batch writes.

HK, 2026-10-06: 배포 히스토리를 알아야 내가 딴 이야기를 안할거같아.

He was right, and the gap was ours. `incoming/last-deploy.json` says what is
running *now* and is overwritten every ten minutes, so there was nowhere to
look up when a version went out or what was in it. Three times in two days he
reasoned from a version that had already been replaced, and each time the
round trip to find that out cost more than this screen does.

Two files, written by `deploy-tick.sh` on the host and read here:

* `current.json` -- the last tick's verdict, whatever it was;
* `deploy-log.jsonl` -- one line per *change*, appended. A tick that finds
  the running image already correct writes nothing, which is what keeps a
  ten-minute batch from burying the four lines a day that mean something.

They live under the data root rather than in the repository: the container
mounts that read-only and does not mount the checkout, and a deploy log is a
record of what happened, not source.

This module only reads. It never deploys, and it never decides anything.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

KST = timezone(timedelta(hours=9))

DEFAULT_DATA_ROOT = "/data/rlwrld-worklog"

# What each outcome means, in the words the screen should use. Kept here
# rather than in the page so that a new outcome in the batch is a one-line
# change in one file -- and so an outcome nobody has translated yet shows up
# as itself instead of as a blank.
OUTCOMES: dict[str, str] = {
    "deployed": "배포됨",
    "current": "이미 같은 버전",
    "idle": "변화 없음",
    "blocked": "통합데브가 막음",
    "awaiting-verification": "검증 대기",
    "build-failed": "빌드 실패",
    "up-failed": "기동 실패",
    "unverified": "확인 불가",
    "refused": "거부됨",
    "busy": "이전 작업 진행 중",
    "no-docker": "docker 없음",
}

# The outcomes that mean a version actually went out. Everything else is the
# batch reporting on itself.
LANDED = {"deployed"}


def deploy_dir() -> Path:
    root = os.environ.get("RAW_ARCHIVE_ROOT") or DEFAULT_DATA_ROOT
    return Path(root) / "deploy"


def _kst(value: Any) -> str | None:
    """An ISO instant as KST, because that is the clock HK reads."""
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(KST).strftime("%Y-%m-%d %H:%M")


def _decorate(entry: dict[str, Any]) -> dict[str, Any]:
    outcome = str(entry.get("outcome") or "")
    return {
        **entry,
        "outcome_label": OUTCOMES.get(outcome, outcome or "불명"),
        "landed": outcome in LANDED,
        "at_kst": _kst(entry.get("finished_at") or entry.get("started_at")),
    }


def read(limit: int = 50) -> dict[str, Any]:
    """The running version and the recent history, newest first.

    Missing files are not an error. A machine that has not deployed since
    this shipped has no log, and saying so plainly is more useful than an
    empty table that looks like a failed query.
    """
    directory = deploy_dir()
    current: dict[str, Any] | None = None
    reason: str | None = None

    try:
        current = _decorate(json.loads((directory / "current.json").read_text("utf-8")))
    except FileNotFoundError:
        reason = "아직 기록이 없습니다. 다음 배포 점검(10분 주기) 때 생깁니다."
    except Exception as error:  # pragma: no cover - exercised by the broken-file test
        reason = f"현재 버전을 읽지 못했습니다: {error}"

    entries: list[dict[str, Any]] = []
    try:
        lines = (directory / "deploy-log.jsonl").read_text("utf-8").splitlines()
    except FileNotFoundError:
        lines = []
    except Exception as error:  # pragma: no cover
        lines = []
        reason = reason or f"이력을 읽지 못했습니다: {error}"

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            # One unreadable line must not take the rest of the history with
            # it -- a log is appended to by a shell script, and the one line
            # a crash truncated is exactly the one you want the others for.
            continue
        if isinstance(parsed, dict):
            entries.append(_decorate(parsed))

    entries.reverse()
    return {
        "current": current,
        "entries": entries[:limit],
        "landed": sum(1 for entry in entries if entry["landed"]),
        "reason": reason,
    }
