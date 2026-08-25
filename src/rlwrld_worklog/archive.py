from __future__ import annotations

import gzip
import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class RawArchive:
    def __init__(self, root: Path, source: str, run_id: str, environment: str) -> None:
        now = datetime.now(timezone.utc)
        self.root = root
        self.source = source
        self.run_id = run_id
        self.environment = environment
        self.run_dir = root / "raw" / source / environment / now.strftime("%Y/%m/%d") / run_id
        self.manifest_dir = root / "manifests" / source / environment
        self.files: list[dict[str, Any]] = []
        self._sequence = 0

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

    def write_page(self, kind: str, body: dict[str, Any]) -> Path:
        self._sequence += 1
        serialized = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        compressed = gzip.compress(serialized, mtime=0)
        digest = hashlib.sha256(compressed).hexdigest()
        path = self.run_dir / f"{self._sequence:06d}-{kind}-{digest[:12]}.json.gz"
        self._atomic_write(path, compressed)
        self.files.append(
            {
                "path": str(path.relative_to(self.root)),
                "sha256": digest,
                "compressed_bytes": len(compressed),
                "kind": kind,
            }
        )
        return path

    def finish(self, details: dict[str, Any]) -> Path:
        manifest = {
            "schema_version": 1,
            "source": self.source,
            "environment": self.environment,
            "run_id": self.run_id,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "files": self.files,
            **details,
        }
        content = (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
        path = self.manifest_dir / f"{self.run_id}.json"
        self._atomic_write(path, content)
        return path

    def write_checkpoint(self, checkpoint: dict[str, Any]) -> Path:
        content = (json.dumps(checkpoint, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
        path = self.manifest_dir / "checkpoint.json"
        self._atomic_write(path, content)
        return path
