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


# -- 같은 곳에 있었던 사람들 ---------------------------------------------------
# HK, 2026-10-13: 학교, 지도교수, 프로젝트, 논문, 회사 등등을 클릭하면, 해당
# 회사의 모든 구성원, 겹치는 기간 등등이 보여지게. Read across every analysis
# file; a name is matched by its parts, so "다음 / 카카오" and "카카오" meet.

RELATED_KINDS = ("org", "school", "advisor", "project", "paper")


def name_keys(name: str) -> set[str]:
    """The comparable parts of a name: split on '/', parentheses and dashes dropped."""
    text = re.sub(r"\([^)]*\)", " ", str(name or ""))
    text = re.split(r"\s[—–-]\s", text)[0]
    keys = set()
    for part in text.split("/"):
        key = re.sub(r"[\s.,:;'\"`·]+", "", part).lower()
        key = re.sub(r"(교수님|교수)$", "", key)
        if len(key) >= 2:
            keys.add(key)
    return keys


def _entries(data: dict[str, Any]):
    for item in data.get("education") or []:
        yield "school", item.get("school"), item
        if item.get("advisor"):
            yield "advisor", item.get("advisor"), item
    for item in data.get("experience") or []:
        yield "org", item.get("org"), item
        for project in item.get("projects") or []:
            yield "project", project.get("name"), {**project, "start": item.get("start"), "end": item.get("end")}
    for paper in data.get("publications") or []:
        yield "paper", paper.get("title"), {
            "start": str(paper.get("year") or ""), "end": str(paper.get("year") or ""),
            "role": paper.get("position"), "venue": paper.get("venue"),
        }


def related(kind: str, name: str, root: Path | None = None) -> list[dict[str, Any]]:
    """Everyone whose analysis has `name` as a `kind`, with when and in what role."""
    if kind not in RELATED_KINDS:
        raise ValueError(f"unknown kind: {kind}")
    wanted = name_keys(name)
    if not wanted:
        return []
    root = root or profiles_root()
    out: list[dict[str, Any]] = []
    for path in sorted(root.glob("*.json")) if root.is_dir() else []:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        for entry_kind, entry_name, item in _entries(data):
            if entry_kind != kind or not (name_keys(entry_name) & wanted):
                continue
            out.append({
                "employee_no": str(data.get("employee_no") or path.stem),
                "name": entry_name,
                "start": item.get("start") or "",
                "end": item.get("end") or "",
                "role": item.get("title") or item.get("degree") or item.get("role") or "",
                "venue": item.get("venue"),
            })
    return out
