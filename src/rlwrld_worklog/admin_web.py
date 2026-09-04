from __future__ import annotations

import json
import logging
import os
import re
from functools import lru_cache
from importlib.resources import files
from typing import Any, Mapping

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from .admin_store import AdminStore
from .connection_checks import check_github, check_google, check_notion, check_slack
from .google_auth import CALENDAR_READONLY_SCOPE, DRIVE_FILE_SCOPE, DRIVE_READONLY_SCOPE


router = APIRouter()
LOGGER = logging.getLogger(__name__)
SESSION_COOKIE = "hk_work_assistant_session"
# The name a break-glass session carries when nobody declared one. Kept as a
# constant because it is written into the audit trail and read back by
# cowork.resolve_actor, which knows this party by exactly this spelling.
EMERGENCY_ACTOR = "local-emergency"
EMERGENCY_ACTOR_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._:-]{0,62}[A-Za-z0-9])?$")
GOOGLE_LOGIN_SCOPES = ["openid", "email", "profile"]
GOOGLE_DATA_SCOPES = [
    *GOOGLE_LOGIN_SCOPES,
    CALENDAR_READONLY_SCOPE,
    DRIVE_READONLY_SCOPE,
    DRIVE_FILE_SCOPE,
]


@lru_cache(maxsize=1)
def store() -> AdminStore:
    return AdminStore.from_environment()


def emergency_login_enabled() -> bool:
    return os.environ.get("EMERGENCY_LOGIN_ENABLED", "false").lower() == "true"


def _session(request: Request) -> dict[str, Any] | None:
    return store().read_session(request.cookies.get(SESSION_COOKIE))


def agent_name(current: Mapping[str, Any]) -> str | None:
    """The agent behind a session, or None when a person is behind it."""
    subject = str(current.get("sub") or "")
    if current.get("role") != "agent" or not subject.startswith("agent:"):
        return None
    return subject[len("agent:") :] or None


def session_actor(current: Mapping[str, Any]) -> str:
    """The name to record for whoever is acting.

    A Google session carries an email and that is the best name it has. A
    break-glass session carries only the subject it declared at the door.
    Reading `email` alone recorded every local edit as `local-emergency`, so a
    board written by several parties read as though one party wrote it.
    """
    name = agent_name(current)
    if name is not None:
        # The board already knows this party as `noa`, and cowork.resolve_actor
        # resolves that spelling. Recording `agent:noa` beside it would give one
        # party two names in the same history, which is the drift that makes a
        # trail unreadable. The subject keeps the prefix; the record does not.
        return name
    return str(current.get("email") or current.get("sub") or EMERGENCY_ACTOR)


def _emergency_subject(body: Mapping[str, Any]) -> str:
    """Who a break-glass session says it is.

    The name is self-declared: this door asks for a password, not for proof of
    identity. Recording it is still worth doing - an edit signed `hk` is more
    use than one signed `local-emergency` - but it must never be mistakable for
    an authenticated name, so an address-shaped one is refused rather than
    minted here. With no name the session stays anonymous and the trail reads
    exactly as it did before.
    """
    declared = body.get("actor")
    if declared is None:
        return EMERGENCY_ACTOR
    if not isinstance(declared, str):
        raise HTTPException(status_code=400, detail="actor must be a string")
    name = declared.strip()
    if not name:
        return EMERGENCY_ACTOR
    if "@" in name:
        raise HTTPException(
            status_code=400,
            detail="actor cannot be an email address: this login proves no identity",
        )
    if name.startswith(store().AGENT_SUBJECT_PREFIX) or name in store().AGENT_NAMES:
        # An agent's name belongs to the door that can prove it. Letting the
        # password door claim it would put two different authorities behind one
        # spelling in the history. The whole subject namespace is refused, not
        # the five bare names: the actor pattern permits a colon, so "agent:noa"
        # walked past a check that only knew "noa".
        raise HTTPException(
            status_code=400,
            detail="actor is an agent name: agents sign in with their own session",
        )
    if not EMERGENCY_ACTOR_PATTERN.match(name):
        raise HTTPException(
            status_code=400,
            detail="actor may contain letters, digits and . _ : - only, up to 64 characters",
        )
    return name


def require_company_session(request: Request) -> dict[str, Any]:
    current = _session(request)
    if current is None or current.get("role") not in {"company_user", "super_admin"}:
        raise HTTPException(status_code=401, detail="company Google login required")
    return current


def require_board_session(request: Request) -> dict[str, Any]:
    """A session allowed to read the board: the owner, or any agent.

    Reading is opened to everyone who works here. Several of this week's wrong
    calls were made by someone who could not see a screen and reasoned from the
    clock instead, so withholding a view costs more than it protects. What stays
    shut is the small set of things that cannot be undone.
    """
    current = _session(request)
    if current is None:
        raise HTTPException(status_code=401, detail="board session required")
    if current.get("role") not in {"super_admin", "agent"}:
        raise HTTPException(status_code=403, detail="board access required")
    return current


def require_super_admin_session(request: Request) -> dict[str, Any]:
    current = _session(request)
    if current is None:
        raise HTTPException(status_code=401, detail="super administrator login required")
    if current.get("role") != "super_admin":
        raise HTTPException(status_code=403, detail="super administrator access required")
    return current


def _require_csrf(request: Request, current: Mapping[str, Any]) -> None:
    supplied = request.headers.get("x-csrf-token")
    if not supplied or supplied != current.get("csrf"):
        raise HTTPException(status_code=403, detail="invalid CSRF token")


async def _json_object(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception as error:
        raise HTTPException(status_code=400, detail="request body must be JSON") from error
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="request body must be an object")
    return body


def _set_session_cookie(response: Response, token: str) -> None:
    # The cookie's life is read from the token rather than written here a second
    # time. Two copies of "twelve hours" that do not know about each other would
    # drift the moment one session type got a different length: the token would
    # still be valid while the browser had already dropped it, and the symptom
    # would appear nowhere near the cause.
    session = store().read_session(token) or {}
    lifetime = int(session.get("exp", 0)) - int(session.get("iat", 0))
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=lifetime if lifetime > 0 else 43_200,
        httponly=True,
        secure=os.environ.get("ADMIN_SESSION_SECURE", "false").lower() == "true",
        samesite="lax",
        path="/",
    )


def role_for_google_claims(
    claims: Mapping[str, Any], *, company_domain: str, super_admin_email: str
) -> tuple[str, str] | None:
    email = str(claims.get("email", "")).lower()
    hosted_domain = str(claims.get("hd", "")).lower()
    if claims.get("email_verified") is not True:
        return None
    if hosted_domain != company_domain.lower() or not email.endswith("@" + company_domain.lower()):
        return None
    role = "super_admin" if email == super_admin_email.lower() else "company_user"
    return email, role


def _google_client_id(client_config: Mapping[str, Any]) -> str:
    section = client_config.get("web") or client_config.get("installed")
    if not isinstance(section, Mapping) or not section.get("client_id"):
        raise RuntimeError("Google OAuth client configuration is invalid")
    return str(section["client_id"])


def _google_callback_url() -> str:
    settings = store().load_settings()
    return str(settings["google_oauth_base_url"]).rstrip("/") + "/auth/google/callback"


def _fetch_google_token(flow: Any, *, code: str, required_scopes: list[str]) -> None:
    """Accept Google's additional previously-granted scopes, but never missing ones."""
    try:
        flow.fetch_token(code=code)
    except Warning as warning:
        # oauthlib raises Warning when Google returns a superset of the scopes
        # requested in this transaction (common after Calendar/Drive consent).
        # Preserve strictness for missing scopes and accept only a superset.
        aliases = {
            "email": "https://www.googleapis.com/auth/userinfo.email",
            "profile": "https://www.googleapis.com/auth/userinfo.profile",
        }
        normalize = lambda scope: aliases.get(scope, scope)
        granted = {normalize(scope) for scope in (getattr(warning, "new_scope", ()) or ())}
        required = {normalize(scope) for scope in required_scopes}
        if not required.issubset(granted):
            raise
        token = getattr(warning, "token", None)
        if not isinstance(token, Mapping):
            raise
        flow.oauth2session.token = dict(token)


@router.get("/", response_class=HTMLResponse)
def service_page() -> HTMLResponse:
    html = files("rlwrld_worklog").joinpath("static/service.html").read_text(encoding="utf-8")
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@router.get("/backoffice", response_class=HTMLResponse)
def backoffice_page() -> HTMLResponse:
    html = files("rlwrld_worklog").joinpath("static/admin.html").read_text(encoding="utf-8")
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@router.get("/admin")
def old_admin_redirect() -> RedirectResponse:
    return RedirectResponse("/backoffice", status_code=308)


@router.get("/api/v1/session")
def session_status(request: Request) -> dict[str, Any]:
    current = _session(request)
    google_ready = store().secret_status()["google_oauth_client"]
    return {
        "authenticated": current is not None,
        "email": current.get("email") if current else None,
        "role": current.get("role") if current else None,
        "auth_method": current.get("auth_method") if current else None,
        "csrf_token": current.get("csrf") if current else None,
        "google_login_available": google_ready,
    }


@router.get("/api/v1/admin/session")
def admin_session_status(request: Request) -> dict[str, Any]:
    current = _session(request)
    google_ready = store().secret_status()["google_oauth_client"]
    return {
        "setup_required": store().setup_required() if emergency_login_enabled() else False,
        "authenticated": current is not None,
        "authorized": bool(current and current.get("role") == "super_admin"),
        "email": current.get("email") if current else None,
        "role": current.get("role") if current else None,
        "auth_method": current.get("auth_method") if current else None,
        "csrf_token": current.get("csrf") if current else None,
        "google_login_available": google_ready,
        "emergency_login_available": emergency_login_enabled(),
    }


@router.post("/api/v1/admin/bootstrap")
async def bootstrap(request: Request, response: Response) -> dict[str, bool]:
    if not emergency_login_enabled():
        raise HTTPException(status_code=404, detail="local emergency login is disabled")
    if not store().setup_required():
        raise HTTPException(status_code=409, detail="administrator is already configured")
    body = await _json_object(request)
    try:
        store().set_admin_password(str(body.get("password", "")))
    except (ValueError, RuntimeError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    token, _ = store().create_session(subject=_emergency_subject(body))
    _set_session_cookie(response, token)
    return {"ok": True}


@router.post("/api/v1/admin/login")
async def emergency_login(request: Request, response: Response) -> dict[str, bool]:
    if not emergency_login_enabled():
        raise HTTPException(status_code=404, detail="local emergency login is disabled")
    body = await _json_object(request)
    if not store().verify_admin_password(str(body.get("password", ""))):
        raise HTTPException(status_code=401, detail="invalid administrator password")
    subject = _emergency_subject(body)
    token, _ = store().create_session(subject=subject)
    _set_session_cookie(response, token)
    store().audit(
        "admin.login",
        actor=subject,
        details={"method": "local_emergency", "actor_declared": subject != EMERGENCY_ACTOR},
    )
    return {"ok": True}


@router.get("/auth/google/login")
def google_login(next: str = "/") -> RedirectResponse:
    if next not in {"/", "/backoffice"}:
        raise HTTPException(status_code=400, detail="invalid return path")
    client_path = store().secret_path("google_oauth_client")
    if not client_path.exists():
        raise HTTPException(status_code=503, detail="Google OAuth client is not configured")
    from google_auth_oauthlib.flow import Flow

    state = store().create_oauth_state(next)
    flow = Flow.from_client_secrets_file(
        str(client_path),
        scopes=GOOGLE_LOGIN_SCOPES,
        state=state,
        autogenerate_code_verifier=True,
    )
    flow.redirect_uri = _google_callback_url()
    authorization_url, _ = flow.authorization_url(
        access_type="online",
        # Login is identity-only. Asking Google to include previously granted
        # Drive/Calendar scopes makes oauthlib reject the token response as a
        # scope change when this account has already authorized data access.
        # Data authorization has its own flow below.
        prompt="select_account",
    )
    store().save_oauth_code_verifier(state, str(flow.code_verifier))
    return RedirectResponse(authorization_url, status_code=302)


@router.get("/auth/google/data/login")
def google_data_login(request: Request) -> RedirectResponse:
    require_super_admin_session(request)
    client_path = store().secret_path("google_oauth_client")
    if not client_path.exists():
        raise HTTPException(status_code=503, detail="Google OAuth client is not configured")
    from google_auth_oauthlib.flow import Flow

    state = store().create_oauth_state("/backoffice", purpose="google_data")
    flow = Flow.from_client_secrets_file(
        str(client_path),
        scopes=GOOGLE_DATA_SCOPES,
        state=state,
        autogenerate_code_verifier=True,
    )
    flow.redirect_uri = _google_callback_url()
    authorization_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent select_account",
    )
    store().save_oauth_code_verifier(state, str(flow.code_verifier))
    return RedirectResponse(authorization_url, status_code=302)


@router.get("/auth/google/callback")
def google_callback(
    request: Request, state: str | None = None, code: str | None = None
) -> RedirectResponse:
    state_payload = store().read_oauth_state(state)
    if state_payload is None:
        raise HTTPException(status_code=400, detail="invalid or expired Google OAuth state")
    if not code:
        raise HTTPException(status_code=400, detail="Google OAuth code is missing")
    code_verifier = store().consume_oauth_code_verifier(state or "")
    if code_verifier is None:
        raise HTTPException(
            status_code=400, detail="Google OAuth transaction is missing or already used"
        )
    client_path = store().secret_path("google_oauth_client")
    if not client_path.exists():
        raise HTTPException(status_code=503, detail="Google OAuth client is not configured")

    from google.auth.transport.requests import Request as GoogleRequest
    from google.oauth2 import id_token
    from google_auth_oauthlib.flow import Flow

    purpose = str(state_payload["purpose"])
    if purpose == "google_data":
        require_super_admin_session(request)
    scopes = GOOGLE_DATA_SCOPES if purpose == "google_data" else GOOGLE_LOGIN_SCOPES
    client_config = json.loads(client_path.read_text(encoding="utf-8"))
    flow = Flow.from_client_config(
        client_config,
        scopes=scopes,
        state=state,
        code_verifier=code_verifier,
        autogenerate_code_verifier=False,
    )
    callback_url = _google_callback_url()
    flow.redirect_uri = callback_url
    try:
        # State was verified above and the one-use PKCE verifier is local.
        # Passing only the code avoids relaxing oauthlib's HTTPS validation for
        # Google's explicitly allowed loopback callback exception.
        _fetch_google_token(flow, code=code, required_scopes=scopes)
        claims = id_token.verify_oauth2_token(
            flow.credentials.id_token,
            GoogleRequest(),
            audience=_google_client_id(client_config),
        )
    except Exception as error:
        # Never log the authorization response, code, state, or token.  The
        # exception class and OAuth error category are sufficient to diagnose
        # configuration failures safely.
        LOGGER.warning(
            "Google OAuth callback failed: type=%s oauth_error=%s",
            type(error).__name__,
            getattr(error, "error", None),
        )
        raise HTTPException(status_code=401, detail="Google login verification failed") from error

    settings = store().load_settings()
    identity = role_for_google_claims(
        claims,
        company_domain=str(settings["company_google_domain"]),
        super_admin_email=str(settings["super_admin_google_email"]),
    )
    if identity is None:
        raise HTTPException(status_code=403, detail="a verified company Google account is required")
    email, role = identity
    if purpose == "google_data":
        if role != "super_admin":
            raise HTTPException(status_code=403, detail="super administrator Google account is required")
        store().save_secret("google_token", flow.credentials.to_json(), actor=email)
        store().audit("connection.authorized", actor=email, details={"name": "google_data"})
        return RedirectResponse("/backoffice?connected=google", status_code=302)

    token, _ = store().create_session(
        subject=str(claims.get("sub", email)), email=email, role=role, auth_method="google"
    )
    response = RedirectResponse(str(state_payload["next"]), status_code=302)
    _set_session_cookie(response, token)
    store().audit("user.login", actor=email, details={"method": "google", "role": role})
    return response


@router.post("/api/v1/logout")
@router.post("/api/v1/admin/logout")
def logout(request: Request, response: Response) -> dict[str, bool]:
    current = require_company_session(request)
    _require_csrf(request, current)
    response.delete_cookie(SESSION_COOKIE, path="/")
    store().audit("user.logout", actor=str(current.get("email") or current.get("sub")))
    return {"ok": True}


@router.get("/api/v1/admin/settings")
def get_settings(request: Request) -> dict[str, Any]:
    require_super_admin_session(request)
    return {"settings": store().load_settings(), "secrets": store().secret_status()}


@router.put("/api/v1/admin/settings")
async def put_settings(request: Request) -> dict[str, Any]:
    current = require_super_admin_session(request)
    _require_csrf(request, current)
    body = await _json_object(request)
    try:
        settings = store().update_settings(body, actor=session_actor(current))
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {"settings": settings, "secrets": store().secret_status()}


@router.put("/api/v1/admin/secrets/{name}")
async def put_secret(name: str, request: Request) -> dict[str, Any]:
    current = require_super_admin_session(request)
    _require_csrf(request, current)
    body = await _json_object(request)
    try:
        store().save_secret(name, str(body.get("value", "")), actor=session_actor(current))
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {"name": name, "configured": True}


@router.post("/api/v1/admin/connections/{name}/test")
def test_connection(name: str, request: Request) -> dict[str, Any]:
    current = require_super_admin_session(request)
    _require_csrf(request, current)
    settings = store().load_settings()
    secret_name = {
        "slack": "slack_token",
        "notion": "notion_token",
        "google": "google_token",
        "github": "github_token",
    }.get(name)
    if secret_name is None:
        raise HTTPException(status_code=404, detail="unsupported connection")
    secret_path = store().secret_path(secret_name)
    if not secret_path.exists():
        raise HTTPException(status_code=409, detail=f"{name} authentication is not configured")
    try:
        if name == "slack":
            result = check_slack(secret_path, expected_team_id=str(settings["slack_expected_team_id"]))
        elif name == "notion":
            result = check_notion(secret_path)
        elif name == "google":
            result = check_google(secret_path)
        else:
            result = check_github(secret_path, organization=str(settings["github_organization"]))
    except Exception as error:
        store().audit(
            "connection.test_failed",
            actor=session_actor(current),
            details={"name": name, "error_type": type(error).__name__},
        )
        raise HTTPException(status_code=400, detail=str(error)) from error
    store().audit(
        "connection.tested",
        actor=session_actor(current),
        details={"name": name, "ok": bool(result.get("ok"))},
    )
    return {"name": name, "result": result}


@router.get("/api/v1/admin/audit")
def get_audit(request: Request, limit: int = 100) -> dict[str, Any]:
    require_super_admin_session(request)
    return {"items": store().read_audit(max(1, min(limit, 500)))}
