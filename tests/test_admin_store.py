# hook-allow: synthetic-credentials
from __future__ import annotations

import json
import stat
from datetime import datetime, timezone
from pathlib import Path

import pytest

from rlwrld_worklog.admin_store import AdminStore, _b64decode
from rlwrld_worklog.admin_web import _fetch_google_token, role_for_google_claims


class _ScopeChangingFlow:
    def __init__(self, granted: list[str]) -> None:
        self.oauth2session = type("Session", (), {"token": {}})()
        self.granted = granted

    def fetch_token(self, *, code: str) -> None:
        warning = Warning("scope changed")
        warning.new_scope = self.granted  # type: ignore[attr-defined]
        warning.token = {"access_token": "secret", "id_token": "identity"}  # type: ignore[attr-defined]
        raise warning


def test_bootstrap_password_and_session(tmp_path: Path) -> None:
    store = AdminStore(tmp_path / "config")
    assert store.setup_required()

    store.set_admin_password("a-long-local-password")

    assert not store.setup_required()
    assert store.verify_admin_password("a-long-local-password")
    assert not store.verify_admin_password("wrong-password")
    token, csrf = store.create_session()
    session = store.read_session(token)
    assert session is not None
    assert session["csrf"] == csrf
    assert store.read_session(token + "tampered") is None


def test_settings_are_validated_and_secret_values_are_not_returned(tmp_path: Path) -> None:
    store = AdminStore(tmp_path / "config")
    settings = store.update_settings(
        {
            "allowed_google_email": "owner@example.com",
            "google_drive_backup_folder_id": "1e2-gO4L95mqDrylZYzR01Kiqq6j9vyPk",
            "google_drive_backup_folder_url": (
                "https://drive.google.com/drive/folders/1e2-gO4L95mqDrylZYzR01Kiqq6j9vyPk"
            ),
        }
    )
    assert settings["timezone"] == "Asia/Seoul"
    assert settings["allowed_google_email"] == "owner@example.com"

    store.save_secret("slack_token", "xoxp-sensitive-token")
    assert store.secret_status()["slack_token"] is True
    assert "sensitive" not in json.dumps(store.read_audit())
    mode = stat.S_IMODE((store.credentials / "slack-token").stat().st_mode)
    assert mode == 0o600


def test_google_client_json_validation(tmp_path: Path) -> None:
    store = AdminStore(tmp_path / "config")
    client = {
        "installed": {
            "client_id": "client.apps.googleusercontent.com",
            "client_secret": "secret",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }
    store.save_secret("google_oauth_client", json.dumps(client))
    saved = json.loads((store.credentials / "google-client.json").read_text(encoding="utf-8"))
    assert saved["installed"]["client_id"] == client["installed"]["client_id"]

    with pytest.raises(ValueError, match="valid JSON"):
        store.save_secret("google_oauth_client", "not-json")


def test_unknown_settings_and_weak_password_are_rejected(tmp_path: Path) -> None:
    store = AdminStore(tmp_path / "config")
    with pytest.raises(ValueError, match="unknown settings"):
        store.update_settings({"token": "must-not-be-a-setting"})
    with pytest.raises(ValueError, match="at least 12"):
        store.set_admin_password("short")


def test_oauth_state_is_signed_and_return_path_is_restricted(tmp_path: Path) -> None:
    store = AdminStore(tmp_path / "config")
    state = store.create_oauth_state("/backoffice")
    assert store.read_oauth_state(state)["next"] == "/backoffice"  # type: ignore[index]
    assert store.read_oauth_state(state + "tampered") is None


def test_oauth_pkce_verifier_is_server_side_and_one_use(tmp_path: Path) -> None:
    store = AdminStore(tmp_path / "config")
    state = store.create_oauth_state("/backoffice")
    store.save_oauth_code_verifier(state, "synthetic-code-verifier")
    assert store.consume_oauth_code_verifier(state) == "synthetic-code-verifier"
    assert store.consume_oauth_code_verifier(state) is None
    with pytest.raises(ValueError, match="return path"):
        store.create_oauth_state("https://attacker.example")

    data_state = store.create_oauth_state("/backoffice", purpose="google_data")
    assert store.read_oauth_state(data_state)["purpose"] == "google_data"  # type: ignore[index]
    with pytest.raises(ValueError, match="purpose"):
        store.create_oauth_state("/backoffice", purpose="unknown")


def test_connector_secret_formats_are_validated(tmp_path: Path) -> None:
    store = AdminStore(tmp_path / "config")
    store.save_secret("notion_token", "ntn_valid-looking-token")
    store.save_secret("github_token", "github_pat_valid-looking-token")
    store.save_secret(
        "google_token",
        json.dumps(
            {
                "client_id": "client",
                "client_secret": "secret",
                "refresh_token": "refresh",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        ),
    )
    with pytest.raises(ValueError, match="Notion token"):
        store.save_secret("notion_token", "wrong")
    with pytest.raises(ValueError, match="GitHub token"):
        store.save_secret("github_token", "wrong")


def test_google_company_and_super_admin_roles() -> None:
    company = role_for_google_claims(
        {"email": "member@rlwrld.ai", "email_verified": True, "hd": "rlwrld.ai"},
        company_domain="rlwrld.ai",
        super_admin_email="hyungkyu.ryu@rlwrld.ai",
    )
    owner = role_for_google_claims(
        {"email": "hyungkyu.ryu@rlwrld.ai", "email_verified": True, "hd": "rlwrld.ai"},
        company_domain="rlwrld.ai",
        super_admin_email="hyungkyu.ryu@rlwrld.ai",
    )
    outsider = role_for_google_claims(
        {"email": "hyungkyu.ryu@gmail.com", "email_verified": True},
        company_domain="rlwrld.ai",
        super_admin_email="hyungkyu.ryu@rlwrld.ai",
    )
    assert company == ("member@rlwrld.ai", "company_user")
    assert owner == ("hyungkyu.ryu@rlwrld.ai", "super_admin")
    assert outsider is None


def test_google_token_accepts_only_additional_scopes() -> None:
    flow = _ScopeChangingFlow(
        [
            "openid",
            "https://www.googleapis.com/auth/userinfo.email",
            "https://www.googleapis.com/auth/userinfo.profile",
            "drive",
        ]
    )
    _fetch_google_token(flow, code="one-use-code", required_scopes=["openid", "email", "profile"])
    assert flow.oauth2session.token["id_token"] == "identity"

    missing = _ScopeChangingFlow(["openid", "email"])
    with pytest.raises(Warning, match="scope changed"):
        _fetch_google_token(
            missing,
            code="one-use-code",
            required_scopes=["openid", "email", "profile"],
        )


# ------------------------------------------------------------- agent sessions


def test_an_agent_session_outlives_a_person_session_by_design(tmp_path: Path) -> None:
    store = AdminStore(tmp_path / "config")
    token, _ = store.issue_agent_session("noa")
    session = store.read_session(token)
    assert session is not None
    assert session["sub"] == "agent:noa"
    assert session["role"] == "agent"
    assert session["exp"] - session["iat"] == store.AGENT_SESSION_SECONDS


def test_revoking_one_agent_leaves_the_others_working(tmp_path: Path) -> None:
    """The price of a long life. Rotating the key would end everyone's."""
    store = AdminStore(tmp_path / "config")
    noa, _ = store.issue_agent_session("noa")
    boa, _ = store.issue_agent_session("boa")

    store.revoke_agent("agent:noa")

    assert store.read_session(noa) is None
    assert store.read_session(boa) is not None


def test_a_revoked_agent_can_be_given_a_working_session_again(tmp_path: Path) -> None:
    store = AdminStore(tmp_path / "config")
    first, _ = store.issue_agent_session("roa")
    store.revoke_agent("agent:roa")
    second, _ = store.issue_agent_session("roa")

    assert store.read_session(first) is None
    assert store.read_session(second) is not None


def test_a_revoked_token_is_still_correctly_signed_and_still_dead(tmp_path: Path) -> None:
    """What kills it is the generation, not the signature or the clock."""
    store = AdminStore(tmp_path / "config")
    token, _ = store.issue_agent_session("doa")
    payload = json.loads(_b64decode(token.split(".", 1)[0]))
    assert payload["exp"] > int(datetime.now(timezone.utc).timestamp())

    store.revoke_agent("agent:doa")
    assert store.read_session(token) is None


def test_an_unknown_agent_cannot_be_given_a_session(tmp_path: Path) -> None:
    """A typo must not mint an identity nobody recognises or thinks to revoke."""
    store = AdminStore(tmp_path / "config")
    with pytest.raises(ValueError):
        store.issue_agent_session("nobody")


def test_a_person_session_carries_no_generation_and_is_unaffected(tmp_path: Path) -> None:
    store = AdminStore(tmp_path / "config")
    person, _ = store.create_session(subject="owner", email="hk@example.test", role="super_admin")
    store.revoke_agent("agent:noa")
    session = store.read_session(person)
    assert session is not None
    assert "gen" not in session


def test_the_token_is_never_written_into_the_audit_trail(tmp_path: Path) -> None:
    store = AdminStore(tmp_path / "config")
    token, _ = store.issue_agent_session("soa")
    trail = store.audit_path.read_text(encoding="utf-8")
    assert "agent.session_issued" in trail
    assert token not in trail
