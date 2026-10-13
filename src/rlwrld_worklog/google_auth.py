from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Sequence


DRIVE_READONLY_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
DRIVE_FILE_SCOPE = "https://www.googleapis.com/auth/drive.file"
CALENDAR_READONLY_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"


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
    """Credentials for these scopes, from a token every caller shares.

    One token serves the calendar collector and the roster sync. Refreshing
    it with only the caller's scopes narrowed the access token to them and
    wrote the narrowed list back, so whichever refreshed last decided what
    the other could do: on 2026-10-13 the backoffice's 새로고침 failed with
    403 insufficient scopes on the roster sheet while the terminal sync read
    the same sheet fine -- the calendar collector had refreshed the token
    last, and the file said calendar.readonly and nothing else.

    So a refresh asks for everything the token already carries plus what
    this caller needs, and a token that lacks this caller's scope is
    refreshed even before it expires. The refresh token was granted both,
    so this needs no new consent; if it was not, Google says invalid_scope
    and the message below says to authorise again.
    """
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    try:
        saved = json.loads(token_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = {}
    held = saved.get("scopes") or []
    if isinstance(held, str):
        held = held.split()
    wanted = sorted(set(held) | set(scopes))
    missing = not set(scopes) <= set(held)

    credentials = Credentials.from_authorized_user_file(str(token_path), scopes=wanted)
    if (credentials.expired or missing) and credentials.refresh_token:
        try:
            credentials.refresh(Request())
        except Exception as error:  # noqa: BLE001 -- google-auth raises several kinds
            raise RuntimeError(
                f"Google token could not be refreshed for {', '.join(scopes)}: {error}. "
                "Authorise Google data again on the 연결 screen"
            ) from error
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
