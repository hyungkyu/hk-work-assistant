"""인사정보 상세: a person's career as their CV and public pages tell it.

HK, 2026-10-13: 홈페이지, 링크드인, 구글스칼라, 엑스, cv의 실제값을 모두
가져와서 저장 ... linkedin 스타일로, 학교/직장 경력의 이력과 프로젝트 이력,
논문 발표 이력, 논문 요약 및 1저자 여부 등등의 상세 분석을 인사정보 상세에서.

What is collected about a person stays out of the repository, so the
analysis is a file under the archive root, one per 사번:
``<RAW_ARCHIVE_ROOT>/profiles/<사번>.json``. The app mounts that root
read-only; whatever writes the files (today a sample, later the batch) is
the only writer. Owner only -- the route decides, this module only reads.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

_EMPLOYEE_NO = re.compile(r"^[0-9A-Za-z_-]{1,40}$")


def profiles_root() -> Path:
    return Path(os.environ.get("RAW_ARCHIVE_ROOT", "/data/rlwrld-worklog")) / "profiles"


def read_cv(employee_numbers: list[str], root: Path | None = None) -> dict[str, Any] | None:
    """The newest analysis among a person's 사번 (a rehire has two), or None."""
    root = root or profiles_root()
    found: list[dict[str, Any]] = []
    for number in employee_numbers:
        if not _EMPLOYEE_NO.match(number or ""):
            continue  # never a path built from anything but a plain 사번
        path = root / f"{number}.json"
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            found.append(data)
    if not found:
        return None
    return max(found, key=lambda item: str(item.get("built_at") or ""))
