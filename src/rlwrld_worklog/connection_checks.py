from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .google_auth import CALENDAR_READONLY_SCOPE, DRIVE_FILE_SCOPE, DRIVE_READONLY_SCOPE
from .notion_client import NotionClient
from .slack_client import SlackClient


SLACK_REQUIRED_SCOPES = {
    "channels:history",
    "channels:read",
    "groups:history",
    "groups:read",
    "im:history",
    "im:read",
    "mpim:history",
    "mpim:read",
    "reactions:read",
    "search:read",
    "users:read",
}


def check_slack(token_path: Path, *, expected_team_id: str = "") -> dict[str, Any]:
    client = SlackClient(token_path.read_text(encoding="utf-8").strip())
    identity = client.call("auth.test")
    scope_header = client.last_response_headers.get("x-oauth-scopes", "")
    scopes = sorted({scope.strip() for scope in scope_header.split(",") if scope.strip()})
    client.call("conversations.list", limit=1, types="public_channel,private_channel,mpim,im")
    client.call("users.list", limit=1)
    missing = sorted(SLACK_REQUIRED_SCOPES - set(scopes))
    actual_team_id = str(identity.get("team_id", ""))
    team_matches = not expected_team_id or actual_team_id == expected_team_id
    return {
        "ok": not missing and team_matches,
        "identity": identity.get("user"),
        "workspace": identity.get("team"),
        "team_id": actual_team_id,
        "team_matches": team_matches,
        "scopes": scopes,
        "missing_scopes": missing,
    }


def check_notion(token_path: Path) -> dict[str, Any]:
    client = NotionClient(token_path.read_text(encoding="utf-8").strip())
    identity = client.call("GET", "/users/me")
    search = client.call("POST", "/search", body={"page_size": 1})
    bot = identity.get("bot", {}) if isinstance(identity.get("bot"), dict) else {}
    workspace = bot.get("workspace_name") or bot.get("owner", {}).get("workspace", {}).get("name")
    return {
        "ok": True,
        "identity": identity.get("name") or identity.get("id"),
        "workspace": workspace,
        "has_accessible_content": bool(search.get("results")),
        "accessible_sample_count": len(search.get("results", [])),
        "note": None if search.get("results") else "토큰은 유효하지만 Integration에 공유된 콘텐츠가 없습니다.",
    }


def check_google(token_path: Path) -> dict[str, Any]:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    data = json.loads(token_path.read_text(encoding="utf-8"))
    credentials = Credentials.from_authorized_user_info(data)
    if credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
    if not credentials.valid:
        raise RuntimeError("Google OAuth token is invalid or expired")
    calendar = build("calendar", "v3", credentials=credentials, cache_discovery=False)
    drive = build("drive", "v3", credentials=credentials, cache_discovery=False)
    calendar_page = calendar.calendarList().list(maxResults=1, showHidden=True).execute()
    drive_about = drive.about().get(fields="user(displayName,emailAddress)").execute()
    granted = sorted(set(data.get("scopes", []) or credentials.scopes or []))
    required = {CALENDAR_READONLY_SCOPE, DRIVE_READONLY_SCOPE, DRIVE_FILE_SCOPE}
    return {
        "ok": required.issubset(granted),
        "identity": drive_about.get("user", {}).get("emailAddress"),
        "calendar_access": isinstance(calendar_page.get("items", []), list),
        "drive_access": bool(drive_about.get("user")),
        "scopes": granted,
        "missing_scopes": sorted(required - set(granted)),
        "has_refresh_token": bool(data.get("refresh_token")),
    }


def _github_get(path: str, token: str) -> tuple[dict[str, Any] | list[Any], dict[str, str]]:
    request = urllib.request.Request(
        "https://api.github.com" + path,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "rlwrld-worklog/0.1",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read()), dict(response.headers)
    except urllib.error.HTTPError as error:
        try:
            detail = json.loads(error.read()).get("message", "unknown")
        except (json.JSONDecodeError, UnicodeDecodeError):
            detail = "unknown"
        raise RuntimeError(f"GitHub API returned HTTP {error.code}: {detail}") from error


def check_github(token_path: Path, *, organization: str) -> dict[str, Any]:
    token = token_path.read_text(encoding="utf-8").strip()
    user, headers = _github_get("/user", token)
    organization_data, _ = _github_get(f"/orgs/{organization}", token)
    repositories, _ = _github_get(
        f"/orgs/{organization}/repos?type=all&sort=updated&per_page=1", token
    )
    scopes = sorted(filter(None, headers.get("x-oauth-scopes", "").split(", ")))
    return {
        "ok": True,
        "identity": user.get("login") if isinstance(user, dict) else None,
        "organization": organization_data.get("login") if isinstance(organization_data, dict) else None,
        "repository_access": bool(repositories),
        "classic_scopes": scopes,
        "note": "Fine-grained token permissions are verified per API operation." if not scopes else None,
    }
