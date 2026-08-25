from __future__ import annotations

import hashlib
from pathlib import Path

from rlwrld_worklog.legacy_drive import FOLDER_MIME, LegacyDriveDownloader


class FakeDrive:
    def __init__(self) -> None:
        self.children = {
            "root": [
                {"id": "d1", "name": "2026-07-30", "mimeType": FOLDER_MIME},
                {"id": "d2", "name": "not-a-date", "mimeType": FOLDER_MIME},
            ],
            "d1": [
                {"id": "s1", "name": "slack", "mimeType": FOLDER_MIME},
                {"id": "o1", "name": "github", "mimeType": FOLDER_MIME},
            ],
            "s1": [
                {"id": "c1", "name": "common", "mimeType": FOLDER_MIME},
                {"id": "a1", "name": "same.json", "mimeType": "application/json", "size": "3"},
                {"id": "a2", "name": "same.json", "mimeType": "application/json", "size": "3"},
            ],
            "c1": [
                {"id": "m1", "name": "meta.json", "mimeType": "application/json", "size": "4"}
            ],
        }
        self.content = {"a1": b"one", "a2": b"two", "m1": b"meta"}

    def list_children(self, folder_id: str):
        return self.children.get(folder_id, [])

    def download(self, file_id: str, destination: Path) -> None:
        destination.write_bytes(self.content[file_id])


def test_download_preserves_tree_and_duplicate_names(tmp_path: Path) -> None:
    result = LegacyDriveDownloader(FakeDrive(), tmp_path).download("root", ["slack", "gcal"])
    base = tmp_path / "legacy/google_drive/daily_raw/2026-07-30/slack"
    assert (base / "common/meta.json").read_bytes() == b"meta"
    assert (base / "same.json__drive_a1").read_bytes() == b"one"
    assert (base / "same.json__drive_a2").read_bytes() == b"two"
    assert result.downloaded == 3
    assert result.source_roots == 1
    assert result.manifest_path.exists()

    again = LegacyDriveDownloader(FakeDrive(), tmp_path).download("root", ["slack"])
    assert again.downloaded == 0
    assert again.unchanged == 3


def test_sha_is_of_downloaded_bytes(tmp_path: Path) -> None:
    result = LegacyDriveDownloader(FakeDrive(), tmp_path).download("root", ["slack"])
    manifest = result.manifest_path.read_text(encoding="utf-8")
    assert hashlib.sha256(b"meta").hexdigest() in manifest
