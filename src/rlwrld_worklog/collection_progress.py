"""Atomic per-run progress snapshots for the 수집 현황 dashboard.

The raw archive and its manifests are external evidence and are immutable: a
run in flight must not be made observable by writing into them. So a run
publishes a small, derived, always-rebuildable snapshot next to the
application's own configuration instead:

    $APP_CONFIG_ROOT/collection-status/progress/<source>/<environment>/<run_id>.json

Three properties matter:

  * **Atomic.** Every update is a temp file plus ``os.replace``, so a reader
    either sees the previous snapshot or the next one, never a half-written
    one -- including when the writer is killed mid-update.
  * **Never fatal.** A capture must not fail because a dashboard file could
    not be written. Every operation here swallows its errors and disables
    itself for the rest of the run.
  * **Stale-detectable after a crash.** A snapshot carries ``updated_at``,
    ``pid`` and ``host``. A run whose snapshot stopped advancing, or whose pid
    is gone on this host, is reported stale rather than running -- a crashed
    capture leaves no manifest, and without this it would look alive forever.

The snapshot takes no lock and lives outside the raw archive, so it cannot
interact with the daily collection lock under ``<archive_root>/locks/``.
"""

from __future__ import annotations

import json
import os
import socket
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

PROGRESS_SCHEMA_VERSION = 1

# A snapshot that has not advanced for this long, and whose run wrote no
# manifest, is reported stale rather than running.
DEFAULT_STALE_AFTER_SECONDS = 1_800

# Snapshots are derived data. Keep the recent ones per source/environment and
# drop the rest; run ids sort chronologically, so lexicographic order is age.
RETAINED_SNAPSHOTS = 200

_MIN_UPDATE_INTERVAL_SECONDS = 5.0
_SAFE_NAME = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")


def is_safe_name(value: str) -> bool:
    """A single path segment that cannot escape its parent directory."""
    return (
        bool(value)
        and len(value) <= 120
        and value[0] not in "._-"
        and set(value) <= _SAFE_NAME
    )


def progress_root(config_root: Path | None = None) -> Path:
    """Where snapshots live, for a reader that always wants a definite path."""
    if config_root is not None:
        return Path(config_root) / "collection-status" / "progress"
    configured = os.environ.get("COLLECTION_PROGRESS_ROOT") or os.environ.get("APP_CONFIG_ROOT")
    root = Path(configured) if configured else Path.home() / ".config/hk-work-assistant"
    return root / "collection-status" / "progress"


def configured_progress_root(config_root: Path | None = None) -> Path | None:
    """Where a *writer* may publish, or None when nothing is configured.

    A capture writes a snapshot only into a root the deployment has named.
    Without this, a collector run from a bare shell would quietly create files
    under the operator's home directory, and every collector test would do the
    same on the machine running it.
    """
    if config_root is not None:
        return Path(config_root) / "collection-status" / "progress"
    configured = os.environ.get("COLLECTION_PROGRESS_ROOT") or os.environ.get("APP_CONFIG_ROOT")
    return Path(configured) / "collection-status" / "progress" if configured else None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


class RunProgress:
    """One run's snapshot writer. Every method is best-effort and silent."""

    def __init__(
        self,
        *,
        source: str,
        environment: str,
        run_id: str,
        started_at: str,
        raw_run_dir: str | None = None,
        capture_density: str | None = None,
        dry_run: bool = False,
        rule_stamp: dict[str, Any] | None = None,
        config_root: Path | None = None,
        clock: Any = time.monotonic,
    ) -> None:
        root = configured_progress_root(config_root)
        self.enabled = (
            root is not None
            and is_safe_name(source)
            and is_safe_name(environment)
            and is_safe_name(run_id)
        )
        self.path = root / source / environment / f"{run_id}.json" if self.enabled and root else None
        self._clock = clock
        self._last_write = float("-inf")
        self._state: dict[str, Any] = {
            "schema_version": PROGRESS_SCHEMA_VERSION,
            "source": source,
            "environment": environment,
            "run_id": run_id,
            "phase": "capture",
            "status": "running",
            "started_at": started_at,
            "updated_at": started_at,
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "raw_run_dir": raw_run_dir,
            "capture_density": capture_density,
            "dry_run": bool(dry_run),
            "files_written": 0,
            "bytes_written": 0,
            "manifest_path": None,
            "ledger": None,
            **(rule_stamp or {}),
        }
        self._write(force=True)

    # ------------------------------------------------------------- updates

    def note_page(self, *, files_written: int, bytes_written: int) -> None:
        self._state["files_written"] = files_written
        self._state["bytes_written"] = bytes_written
        self._write()

    def note_phase(self, phase: str) -> None:
        self._state["phase"] = phase
        self._write(force=True)

    def note_finished(
        self, *, status: str, manifest_path: str | None, files_written: int, bytes_written: int
    ) -> None:
        self._state.update(
            {
                "phase": "finished",
                "status": status,
                "manifest_path": manifest_path,
                "files_written": files_written,
                "bytes_written": bytes_written,
            }
        )
        self._write(force=True)
        self._prune()

    def note_ledger(self, detail: dict[str, Any] | None) -> None:
        """Record the ledger projection of this run once it has been made."""
        if detail is None:
            return
        self._state["ledger"] = {
            "records_written": detail.get("records_written"),
            "schema_errors": detail.get("schema_errors"),
            "output_path": detail.get("output_path"),
        }
        self._write(force=True)

    # -------------------------------------------------------------- writing

    def _write(self, *, force: bool = False) -> None:
        if not self.enabled or self.path is None:
            return
        now = self._clock()
        if not force and now - self._last_write < _MIN_UPDATE_INTERVAL_SECONDS:
            return
        self._state["updated_at"] = _utc_now()
        try:
            _atomic_write_json(self.path, self._state)
        except (OSError, TypeError, ValueError):
            # A dashboard file is never worth failing a capture for.
            self.enabled = False
            return
        self._last_write = now

    def _prune(self) -> None:
        if not self.enabled or self.path is None:
            return
        try:
            names = sorted(
                entry.name
                for entry in os.scandir(self.path.parent)
                if entry.is_file() and entry.name.endswith(".json")
            )
        except OSError:
            return
        for name in names[: max(0, len(names) - RETAINED_SNAPSHOTS)]:
            if name == self.path.name:
                continue
            try:
                (self.path.parent / name).unlink()
            except OSError:
                continue


# --------------------------------------------------------------- reading


def _pid_is_alive(pid: Any, host: Any) -> bool | None:
    """True/False on this host, None when the answer cannot be known here."""
    if not isinstance(pid, int) or pid <= 0 or host != socket.gethostname():
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


def read_snapshot(path: Path) -> dict[str, Any] | None:
    """One snapshot, or None when it is missing, unreadable or malformed."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(value, dict) or not isinstance(value.get("run_id"), str):
        return None
    return value


def iter_snapshots(
    root: Path, *, source: str | None = None, environment: str | None = None
) -> Iterator[dict[str, Any]]:
    """Every readable snapshot under ``root``, newest run id last.

    Directory names are taken from the filesystem, never from a caller, and
    are re-checked against ``is_safe_name`` so a hand-made directory cannot
    make a reader walk somewhere unexpected.
    """
    for source_entry in _safe_entries(root, keep=source):
        for environment_entry in _safe_entries(source_entry, keep=environment):
            for name in sorted(_json_names(environment_entry)):
                snapshot = read_snapshot(environment_entry / name)
                if snapshot is not None:
                    yield snapshot


def _safe_entries(root: Path, *, keep: str | None) -> list[Path]:
    try:
        entries = [entry for entry in os.scandir(root) if entry.is_dir(follow_symlinks=False)]
    except OSError:
        return []
    return [
        root / entry.name
        for entry in entries
        if is_safe_name(entry.name) and (keep is None or entry.name == keep)
    ]


def _json_names(root: Path) -> list[str]:
    try:
        return [
            entry.name
            for entry in os.scandir(root)
            if entry.is_file(follow_symlinks=False)
            and entry.name.endswith(".json")
            and is_safe_name(entry.name)
        ]
    except OSError:
        return []


def snapshot_liveness(
    snapshot: dict[str, Any],
    *,
    now: datetime | None = None,
    stale_after_seconds: int = DEFAULT_STALE_AFTER_SECONDS,
) -> dict[str, Any]:
    """Whether a still-unfinished snapshot is running or stale, and why."""
    moment = now or datetime.now(timezone.utc)
    updated_at = snapshot.get("updated_at")
    age: float | None = None
    try:
        parsed = datetime.fromisoformat(str(updated_at))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        age = (moment - parsed).total_seconds()
    except (TypeError, ValueError):
        age = None
    alive = _pid_is_alive(snapshot.get("pid"), snapshot.get("host"))
    if snapshot.get("phase") == "finished":
        return {"state": "finished", "age_seconds": age, "process_alive": alive, "reason": None}
    if alive is False:
        return {
            "state": "stale",
            "age_seconds": age,
            "process_alive": False,
            "reason": "the process that wrote this snapshot is gone and no manifest was written",
        }
    if age is None:
        return {
            "state": "unknown",
            "age_seconds": None,
            "process_alive": alive,
            "reason": "the snapshot carries no usable updated_at",
        }
    if age > stale_after_seconds:
        return {
            "state": "stale",
            "age_seconds": age,
            "process_alive": alive,
            "reason": f"no progress for {int(age)}s and no manifest was written",
        }
    return {"state": "running", "age_seconds": age, "process_alive": alive, "reason": None}
