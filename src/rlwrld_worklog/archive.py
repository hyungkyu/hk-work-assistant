"""Immutable raw archive for official-API responses.

Three rules this module exists to enforce:

  1. A raw observation is never overwritten. Every run gets its own directory,
     every page inside it gets a unique sequence-plus-digest filename, and a
     write to an existing path is refused rather than silently replacing it.
  2. A run manifest is detailed enough that a fetched response never has to be
     fetched again: endpoint, request parameters, page and item counts, file
     hashes, coverage, skips, errors, truncation and rate-limit signals.
  3. A checkpoint advances only when the caller says the run finished
     completely, and the previous checkpoint is kept as history rather than
     being lost to the overwrite.
  4. Every manifest names the collection rule version it was captured under,
     together with that rule's content digest, so a later reader never has to
     guess what "collected" meant on the day the run happened. The stamp is
     the archive's own and is applied after the caller's details, so a
     collector cannot accidentally overwrite or omit it. Dry-run, smoke and
     failure manifests carry it too.

A run in flight publishes a small derived snapshot outside the archive (see
``collection_progress``); nothing in this module ever rewrites a raw file, a
manifest or a checkpoint that already exists.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .collection_progress import RunProgress
from .collection_rules import active_rule_stamp

MANIFEST_SCHEMA_VERSION = 2

# Request parameters that must never be written to disk in a manifest, even
# though the collectors do not currently pass them.
_REDACTED_REQUEST_KEYS = {
    "token",
    "access_token",
    "authorization",
    "client_secret",
    "refresh_token",
    "password",
}


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _redact_request(request: Any) -> dict[str, Any]:
    if not isinstance(request, dict):
        return {}
    redacted: dict[str, Any] = {}
    for key, value in request.items():
        name = str(key)
        if name.lower() in _REDACTED_REQUEST_KEYS:
            redacted[name] = "<redacted>"
        elif isinstance(value, (str, int, float, bool)) or value is None:
            redacted[name] = value
        else:
            redacted[name] = str(value)
    return redacted


class RawArchive:
    def __init__(
        self,
        root: Path,
        source: str,
        run_id: str,
        environment: str,
        *,
        capture_profile: str | None = None,
        capture_density: str = "full",
        dry_run: bool = False,
        config_root: Path | None = None,
    ) -> None:
        now = datetime.now(timezone.utc)
        self.root = root
        self.source = source
        self.run_id = run_id
        self.environment = environment
        self.capture_profile = capture_profile or f"live-{source}/v1"
        self.capture_density = capture_density
        self.dry_run = dry_run
        self.started_at = now.isoformat()
        self.run_dir = root / "raw" / source / environment / now.strftime("%Y/%m/%d") / run_id
        self.manifest_dir = root / "manifests" / source / environment
        self.files: list[dict[str, Any]] = []
        self._sequence = 0
        self._manifests_written = 0
        # Run-level signals, all reported in the manifest.
        self.api_coverage: dict[str, dict[str, int]] = {}
        self.skips: list[dict[str, Any]] = []
        self.errors: list[dict[str, Any]] = []
        self.truncation: list[dict[str, Any]] = []
        self.rate_limit_hits = 0
        self.requested_window: dict[str, Any] = {}
        self.checkpoint_in: dict[str, Any] = {}
        self.checkpoint_out: dict[str, Any] | None = None
        self.coverage_notes: list[str] = []
        self.bytes_archived = 0
        self.rule_stamp = active_rule_stamp()
        # Derived, best-effort, and outside the archive: a reader can see a
        # run that has not written its manifest yet, and can tell a crashed
        # run from a live one. Never fatal, never touches raw bytes.
        self.progress = RunProgress(
            source=source,
            environment=environment,
            run_id=run_id,
            started_at=self.started_at,
            raw_run_dir=str(self.run_dir.relative_to(root)) if _within(self.run_dir, root) else None,
            capture_density=capture_density,
            dry_run=dry_run,
            rule_stamp=self.rule_stamp,
            config_root=config_root,
        )

    # ------------------------------------------------------------- writing

    @staticmethod
    def _atomic_write(path: Path, content: bytes) -> None:
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

    def write_page(
        self,
        kind: str,
        body: dict[str, Any],
        *,
        endpoint: str | None = None,
        request: dict[str, Any] | None = None,
        item_count: int | None = None,
    ) -> Path:
        """Archive one API response verbatim. Never replaces an existing file."""
        self._sequence += 1
        serialized = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        compressed = gzip.compress(serialized, mtime=0)
        digest = hashlib.sha256(compressed).hexdigest()
        path = self.run_dir / f"{self._sequence:06d}-{kind}-{digest[:12]}.json.gz"
        if path.exists():
            raise FileExistsError(f"raw archive is append-only; refusing to overwrite {path}")
        self._atomic_write(path, compressed)
        entry: dict[str, Any] = {
            "path": str(path.relative_to(self.root)),
            "sha256": digest,
            "compressed_bytes": len(compressed),
            "kind": kind,
            "endpoint": endpoint,
            "request": _redact_request(request),
            "item_count": item_count,
            "written_at": datetime.now(timezone.utc).isoformat(),
        }
        self.files.append(entry)
        self.bytes_archived += len(compressed)
        self.progress.note_page(
            files_written=len(self.files), bytes_written=self.bytes_archived
        )
        if endpoint:
            self.note_call(endpoint, items=item_count or 0)
        return path

    # ------------------------------------------------------------- signals

    def note_call(self, endpoint: str, *, items: int = 0, pages: int = 1) -> None:
        bucket = self.api_coverage.setdefault(endpoint, {"pages": 0, "items": 0})
        bucket["pages"] += pages
        bucket["items"] += items

    def note_skip(self, kind: str, **details: Any) -> None:
        self.skips.append({"kind": kind, **details})

    def note_error(self, kind: str, **details: Any) -> None:
        self.errors.append({"kind": kind, **details})

    def note_truncation(self, reason: str, **details: Any) -> None:
        self.truncation.append({"reason": reason, **details})

    def note_coverage(self, note: str) -> None:
        if note not in self.coverage_notes:
            self.coverage_notes.append(note)

    def note_rate_limit(self, hits: int) -> None:
        self.rate_limit_hits = max(self.rate_limit_hits, int(hits))

    def set_requested_window(self, window: dict[str, Any]) -> None:
        self.requested_window = dict(window)

    def set_checkpoint_in(self, checkpoint: dict[str, Any] | None) -> None:
        self.checkpoint_in = dict(checkpoint or {})

    # ------------------------------------------------------------ manifest

    @property
    def truncated(self) -> bool:
        return bool(self.truncation)

    def finish(self, details: dict[str, Any]) -> Path:
        manifest = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "source": self.source,
            "environment": self.environment,
            "run_id": self.run_id,
            "capture_profile": self.capture_profile,
            "capture_density": self.capture_density,
            "dry_run": self.dry_run,
            "started_at": self.started_at,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "requested_window": self.requested_window,
            "checkpoint_in": self.checkpoint_in,
            "checkpoint_out": self.checkpoint_out,
            "checkpoint_advanced": self.checkpoint_out is not None,
            "api_coverage": {key: dict(value) for key, value in sorted(self.api_coverage.items())},
            "coverage_notes": list(self.coverage_notes),
            "pages_archived": len(self.files),
            "rate_limit_hits": self.rate_limit_hits,
            "truncated": self.truncated,
            "truncation": list(self.truncation),
            "skips": list(self.skips),
            "errors": list(self.errors),
            "files": self.files,
            **details,
            # The rule stamp is the archive's own record of how this run was
            # supposed to collect. It is applied last so no caller detail can
            # drop it, and it is identical for success, dry-run and failure.
            **self.rule_stamp,
        }
        content = (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
        name = f"{self.run_id}.json" if not self._manifests_written else f"{self.run_id}.{self._manifests_written}.json"
        self._manifests_written += 1
        path = self.manifest_dir / name
        self._atomic_write(path, content)
        self.progress.note_finished(
            status=str(manifest.get("status") or "unknown"),
            manifest_path=str(path),
            files_written=len(self.files),
            bytes_written=self.bytes_archived,
        )
        return path

    # ---------------------------------------------------------- checkpoint

    @property
    def checkpoint_path(self) -> Path:
        return self.manifest_dir / "checkpoint.json"

    def read_checkpoint(self) -> dict[str, Any]:
        try:
            value = json.loads(self.checkpoint_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def write_checkpoint(self, checkpoint: dict[str, Any]) -> Path:
        """Replace the current checkpoint, keeping the prior one as history.

        Refused during a dry run: a bounded or smoke capture must never move a
        production checkpoint forward.
        """
        if self.dry_run:
            raise RuntimeError("a dry-run capture must not advance the checkpoint")
        previous = self.read_checkpoint()
        if previous:
            history_name = str(previous.get("run_id") or previous.get("updated_at") or "unknown")
            safe = "".join(character if character.isalnum() or character in "-_." else "_" for character in history_name)
            history = self.manifest_dir / "checkpoints" / f"{safe}.json"
            if not history.exists():
                self._atomic_write(
                    history,
                    (json.dumps(previous, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(),
                )
        content = (json.dumps(checkpoint, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
        self._atomic_write(self.checkpoint_path, content)
        self.checkpoint_out = checkpoint
        return self.checkpoint_path
