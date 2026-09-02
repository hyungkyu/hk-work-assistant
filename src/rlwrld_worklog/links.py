from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any


NOTION_URL_RE = re.compile(
    r"https?://(?:[A-Za-z0-9-]+\.)?(?:notion\.so|notion\.site)/[^\s<>\]\[\"']+",
    re.IGNORECASE,
)
NOTION_PAGE_ID_RE = re.compile(
    r"(?<![0-9a-f])([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}|[0-9a-f]{32})(?![0-9a-f])",
    re.IGNORECASE,
)


def canonical_notion_page_id(value: str) -> str | None:
    match = None
    for match in NOTION_PAGE_ID_RE.finditer(value):
        pass
    if match is None:
        return None
    compact = match.group(1).replace("-", "").lower()
    return f"{compact[:8]}-{compact[8:12]}-{compact[12:16]}-{compact[16:20]}-{compact[20:]}"


def extract_notion_urls(value: Any) -> list[str]:
    found: dict[str, None] = {}

    def visit(item: Any) -> None:
        if isinstance(item, str):
            for match in NOTION_URL_RE.findall(item):
                found[match.rstrip(".,;:!?)]}")] = None
        elif isinstance(item, dict):
            for nested in item.values():
                visit(nested)
        elif isinstance(item, Iterable) and not isinstance(item, (bytes, bytearray)):
            for nested in item:
                visit(nested)

    visit(value)
    return sorted(found)
