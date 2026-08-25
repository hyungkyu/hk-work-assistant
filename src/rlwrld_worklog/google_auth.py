from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Sequence


DRIVE_READONLY_SCOPE = "https://www.googleapis.com/auth/drive.readonly"


def _atomic_private_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
        path.chmod(0o600)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def authorize_installed_app(client_secrets: Path, token_path: Path, scopes: Sequence[str]) -> None:
    """Open Google's loopback login and save only the resulting user token locally."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(str(client_secrets), scopes=list(scopes))
    credentials = flow.run_local_server(host="127.0.0.1", port=0, open_browser=True)
    _atomic_private_write(token_path, credentials.to_json())


def load_credentials(token_path: Path, scopes: Sequence[str]):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    credentials = Credentials.from_authorized_user_file(str(token_path), scopes=list(scopes))
    if credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
        _atomic_private_write(token_path, credentials.to_json())
    if not credentials.valid:
        raise RuntimeError("Google token is invalid; run `worklog google-auth` again")
    return credentials


def token_summary(token_path: Path) -> dict[str, object]:
    data = json.loads(token_path.read_text(encoding="utf-8"))
    return {
        "path": str(token_path),
        "scopes": data.get("scopes", []),
        "has_refresh_token": bool(data.get("refresh_token")),
        "expiry": data.get("expiry"),
    }
