from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .links import canonical_notion_page_id, extract_notion_urls
from .models import TimelineEvent


def _atomic_json(path: Path, value: Any) -> None:
    content = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


class NotionLinkQueue:
    """The set of Notion URLs discovered elsewhere that Notion still has to fetch.

    This is persistent production state: an entry marked `fetched` is never
    offered again. A `read_only` queue therefore exists so a dry run or a smoke
    run can read the real queue -- it still needs to know what is pending in
    order to report honestly -- without creating, updating or marking anything.
    Enforcement lives here rather than at each call site, so a new caller
    inherits the guarantee instead of having to remember it.

    In read-only mode the mutating methods still return the count they *would*
    have written, so a dry-run summary stays informative.
    """

    def __init__(self, archive_root: Path, environment: str, *, read_only: bool = False) -> None:
        self.archive_root = archive_root
        self.environment = environment
        self.read_only = read_only
        self.path = archive_root / "manifests" / "notion" / environment / "link-queue.json"

    def read_only_view(self) -> "NotionLinkQueue":
        if self.read_only:
            return self
        return NotionLinkQueue(self.archive_root, self.environment, read_only=True)

    def load(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        value = json.loads(self.path.read_text(encoding="utf-8"))
        return {item["url"]: item for item in value.get("items", [])}

    def add_events(self, events: Iterable[TimelineEvent]) -> int:
        queue = self.load()
        now = datetime.now(timezone.utc).isoformat()
        added = 0
        for event in events:
            for url in extract_notion_urls({"payload": event.payload, "permalink": event.permalink}):
                item = queue.get(url)
                if item is None:
                    item = {
                        "url": url,
                        "page_id": canonical_notion_page_id(url),
                        "status": "pending",
                        "first_seen_at": now,
                        "sources": [],
                    }
                    queue[url] = item
                    added += 1
                item["last_seen_at"] = now
                source_ref = {
                    "source": event.source.value,
                    "event_id": event.event_id,
                    "external_id": event.external_id,
                }
                if source_ref not in item["sources"]:
                    item["sources"].append(source_ref)
                if item.get("status") in {"failed", "unresolved"}:
                    item["status"] = "pending"
        self._save(queue)
        return added

    def add_urls(self, urls: Iterable[str], *, source: str, run_id: str) -> int:
        queue = self.load()
        now = datetime.now(timezone.utc).isoformat()
        added = 0
        for url in urls:
            item = queue.get(url)
            if item is None:
                item = {
                    "url": url,
                    "page_id": canonical_notion_page_id(url),
                    "status": "pending",
                    "first_seen_at": now,
                    "sources": [],
                }
                queue[url] = item
                added += 1
            item["last_seen_at"] = now
            source_ref = {"source": source, "run_id": run_id}
            if source_ref not in item["sources"]:
                item["sources"].append(source_ref)
            if item.get("status") in {"failed", "unresolved"}:
                item["status"] = "pending"
        self._save(queue)
        return added

    def pending(self) -> list[dict[str, Any]]:
        return [item for item in self.load().values() if item.get("status") != "fetched"]

    def mark(self, url: str, status: str, *, error: str | None = None) -> None:
        if self.read_only:
            return
        queue = self.load()
        item = queue.get(url)
        if item is None:
            return
        item["status"] = status
        item["last_attempt_at"] = datetime.now(timezone.utc).isoformat()
        if error:
            item["last_error"] = error
        else:
            item.pop("last_error", None)
        self._save(queue)

    def _save(self, queue: dict[str, dict[str, Any]]) -> None:
        if self.read_only:
            return
        _atomic_json(
            self.path,
            {
                "schema_version": 1,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "items": sorted(queue.values(), key=lambda item: item["url"]),
            },
        )
