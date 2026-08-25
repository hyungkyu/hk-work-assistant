from __future__ import annotations

import hashlib
import io
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Protocol, Sequence


FOLDER_MIME = "application/vnd.google-apps.folder"
GOOGLE_NATIVE_PREFIX = "application/vnd.google-apps."
DATE_FOLDER = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class DriveFiles(Protocol):
    def list_children(self, folder_id: str) -> list[dict[str, Any]]: ...
    def download(self, file_id: str, destination: Path) -> None: ...


class GoogleDriveFiles:
    def __init__(self, credentials: Any) -> None:
        from googleapiclient.discovery import build

        self.service = build("drive", "v3", credentials=credentials, cache_discovery=False)

    def list_children(self, folder_id: str) -> list[dict[str, Any]]:
        fields = "nextPageToken,files(id,name,mimeType,size,md5Checksum,modifiedTime,createdTime)"
        result: list[dict[str, Any]] = []
        page_token = None
        while True:
            response = (
                self.service.files()
                .list(
                    q=f"'{folder_id}' in parents and trashed = false",
                    spaces="drive",
                    fields=fields,
                    pageSize=1000,
                    pageToken=page_token,
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                )
                .execute()
            )
            result.extend(response.get("files", []))
            page_token = response.get("nextPageToken")
            if not page_token:
                return result

    def download(self, file_id: str, destination: Path) -> None:
        from googleapiclient.http import MediaIoBaseDownload

        request = self.service.files().get_media(fileId=file_id, supportsAllDrives=True)
        with destination.open("wb") as stream:
            downloader = MediaIoBaseDownload(stream, request, chunksize=8 * 1024 * 1024)
            done = False
            while not done:
                _, done = downloader.next_chunk()


@dataclass(frozen=True)
class DownloadResult:
    manifest_path: Path
    downloaded: int
    unchanged: int
    bytes_downloaded: int
    source_roots: int


def _safe_name(name: str) -> str:
    cleaned = name.replace("/", "_").replace("\x00", "_")
    if cleaned in {"", ".", ".."}:
        return "_unnamed"
    return cleaned


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _md5(path: Path) -> str:
    digest = hashlib.md5(usedforsecurity=False)
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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


def _resolved_names(children: Sequence[dict[str, Any]]) -> list[tuple[dict[str, Any], str]]:
    counts: dict[str, int] = {}
    for child in children:
        name = _safe_name(str(child["name"]))
        counts[name] = counts.get(name, 0) + 1
    return [
        (
            child,
            f"{_safe_name(str(child['name']))}__drive_{child['id']}"
            if counts[_safe_name(str(child["name"]))] > 1
            else _safe_name(str(child["name"])),
        )
        for child in children
    ]


class LegacyDriveDownloader:
    def __init__(self, drive: DriveFiles, archive_root: Path) -> None:
        self.drive = drive
        self.archive_root = archive_root
        self.mirror_root = archive_root / "legacy" / "google_drive" / "daily_raw"

    def download(self, daily_raw_folder_id: str, sources: Iterable[str]) -> DownloadResult:
        selected = set(sources)
        if not selected or not selected <= {"slack", "gcal"}:
            raise ValueError("sources must contain slack and/or gcal")
        started = datetime.now(timezone.utc)
        run_id = started.strftime("%Y%m%dT%H%M%SZ")
        records: list[dict[str, Any]] = []
        stats = {"downloaded": 0, "unchanged": 0, "bytes_downloaded": 0, "source_roots": 0}

        date_folders = [
            child
            for child in self.drive.list_children(daily_raw_folder_id)
            if child.get("mimeType") == FOLDER_MIME and DATE_FOLDER.match(str(child.get("name", "")))
        ]
        for date_folder, date_name in _resolved_names(date_folders):
            source_children = [
                child
                for child in self.drive.list_children(str(date_folder["id"]))
                if child.get("mimeType") == FOLDER_MIME and child.get("name") in selected
            ]
            for source_folder, source_name in _resolved_names(source_children):
                stats["source_roots"] += 1
                destination = self.mirror_root / date_name / source_name
                self._walk(str(source_folder["id"]), destination, records, stats)

        manifest = {
            "schema_version": 1,
            "run_id": run_id,
            "started_at": started.isoformat(),
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "daily_raw_folder_id": daily_raw_folder_id,
            "sources": sorted(selected),
            "policy": "immutable Drive mirror; attachments referenced by JSON are not followed",
            "stats": stats,
            "files": records,
        }
        manifest_path = self.archive_root / "legacy" / "manifests" / f"drive-{run_id}.json"
        _atomic_json(manifest_path, manifest)
        return DownloadResult(manifest_path=manifest_path, **stats)

    def _walk(
        self,
        folder_id: str,
        destination: Path,
        records: list[dict[str, Any]],
        stats: dict[str, int],
    ) -> None:
        children = self.drive.list_children(folder_id)
        for child, resolved_name in _resolved_names(children):
            target = destination / resolved_name
            if child.get("mimeType") == FOLDER_MIME:
                self._walk(str(child["id"]), target, records, stats)
                continue
            mime_type = str(child.get("mimeType", ""))
            if mime_type.startswith(GOOGLE_NATIVE_PREFIX):
                raise RuntimeError(
                    f"Cannot losslessly mirror Google-native file {child['id']} ({child['name']}, {mime_type})"
                )
            record = self._download_file(child, target, stats)
            records.append(record)

    def _download_file(self, metadata: dict[str, Any], target: Path, stats: dict[str, int]) -> dict[str, Any]:
        target.parent.mkdir(parents=True, exist_ok=True)
        expected_size = int(metadata["size"]) if metadata.get("size") is not None else None
        expected_md5 = metadata.get("md5Checksum")
        known_unchanged = (
            target.exists()
            and (expected_size is None or target.stat().st_size == expected_size)
            and expected_md5 is not None
            and _md5(target) == expected_md5
        )
        if known_unchanged:
            digest = _sha256(target)
            stats["unchanged"] += 1
            state = "unchanged"
        else:
            descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
            os.close(descriptor)
            temporary = Path(temporary_name)
            try:
                self.drive.download(str(metadata["id"]), temporary)
                if expected_size is not None and temporary.stat().st_size != expected_size:
                    raise IOError(
                        f"size mismatch for {metadata['id']}: {temporary.stat().st_size} != {expected_size}"
                    )
                digest = _sha256(temporary)
                if target.exists():
                    old_digest = _sha256(target)
                    if old_digest == digest:
                        temporary.unlink()
                        stats["unchanged"] += 1
                        state = "unchanged"
                    else:
                        version = target.with_name(f"{target.name}.superseded-{old_digest[:12]}")
                        if not version.exists():
                            os.replace(target, version)
                        else:
                            target.unlink()
                        os.replace(temporary, target)
                        stats["downloaded"] += 1
                        stats["bytes_downloaded"] += target.stat().st_size
                        state = "downloaded"
                else:
                    os.replace(temporary, target)
                    stats["downloaded"] += 1
                    stats["bytes_downloaded"] += target.stat().st_size
                    state = "downloaded"
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise
        return {
            "drive_id": metadata["id"],
            "drive_name": metadata["name"],
            "mime_type": metadata.get("mimeType"),
            "size": target.stat().st_size,
            "sha256": digest,
            "md5_checksum": metadata.get("md5Checksum"),
            "modified_time": metadata.get("modifiedTime"),
            "created_time": metadata.get("createdTime"),
            "local_path": str(target.relative_to(self.archive_root)),
            "state": state,
        }
