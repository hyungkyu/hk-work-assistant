from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


DEFAULT_SETTINGS: dict[str, Any] = {
    "allowed_google_email": "",
    "company_google_domain": "rlwrld.ai",
    "super_admin_google_email": "hyungkyu.ryu@rlwrld.ai",
    "google_oauth_base_url": "http://127.0.0.1:8080",
    "timezone": "Asia/Seoul",
    "daily_collection_hour": 3,
    "data_root": "/data/hk-work-assistant",
    "runtime_root": "/var/lib/hk-work-assistant",
    "google_drive_backup_folder_id": "",
    "google_drive_backup_folder_url": "",
    "google_drive_backup_enabled": False,
    "slack_expected_team_id": "",
    "github_organization": "RLWRLD",
    "local_model_provider": "ollama",
    "local_model_name": "",
    "local_model_endpoint": "http://127.0.0.1:11434",
}

SECRET_FILES = {
    "google_oauth_client": "google-client.json",
    "google_token": "google-token.json",
    "slack_token": "slack-token",
    "github_token": "github-token",
    "notion_token": "notion-token",
    "restic_password": "restic-password",
}

_DRIVE_ID = re.compile(r"^[A-Za-z0-9_-]{10,200}$")
_TEAM_ID = re.compile(r"^(|T[A-Z0-9]{5,30})$")
_GITHUB_ORG = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _atomic_private_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
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


class AdminStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.credentials = root / "credentials"
        self.settings_path = root / "settings.json"
        self.password_path = self.credentials / "admin-password.json"
        # A second door, to the same building, that opens onto less of it. The
        # owner's password is the one that can change settings and read
        # secrets; this one signs in as an ordinary company user, so the owner
        # can look at his own backoffice the way everyone else sees it without
        # handing anybody the password that runs it.
        self.staff_password_path = self.credentials / "staff-password.json"
        self.session_key_path = self.credentials / "admin-session-key"
        self.agent_generations_path = self.credentials / "agent-session-generations.json"
        self.oauth_pkce_dir = self.credentials / "oauth-pkce"
        # Not a setting. A setting is one value edited in place; this is a list
        # that grows and shrinks, and each entry records when and by whom it
        # was added, which a flat settings map has no room for.
        self.notion_seeds_path = root / "notion-seeds.json"
        # Same reason as the seed list, and one more: a bookmark is worth
        # nothing if it is only in one person's browser. This file is what
        # makes "the storage dashboard" mean the same address to everybody.
        self.bookmarks_path = root / "bookmarks.json"
        self.audit_path = root / "audit.jsonl"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        self.credentials.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.credentials.chmod(0o700)
        self.oauth_pkce_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.oauth_pkce_dir.chmod(0o700)

    @classmethod
    def from_environment(cls) -> "AdminStore":
        configured = os.environ.get("APP_CONFIG_ROOT")
        root = Path(configured) if configured else Path.home() / ".config/hk-work-assistant"
        return cls(root)

    def setup_required(self) -> bool:
        return not self.password_path.exists()

    def load_settings(self) -> dict[str, Any]:
        settings = dict(DEFAULT_SETTINGS)
        if self.settings_path.exists():
            stored = json.loads(self.settings_path.read_text(encoding="utf-8"))
            if not isinstance(stored, dict):
                raise RuntimeError("settings.json must contain an object")
            settings.update({key: stored[key] for key in DEFAULT_SETTINGS if key in stored})
        return settings

    def update_settings(self, changes: Mapping[str, Any], *, actor: str = "owner") -> dict[str, Any]:
        unknown = sorted(set(changes) - set(DEFAULT_SETTINGS))
        if unknown:
            raise ValueError(f"unknown settings: {', '.join(unknown)}")
        settings = self.load_settings()
        settings.update(changes)
        self._validate_settings(settings)
        _atomic_private_write(
            self.settings_path,
            json.dumps(settings, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
        self.audit("settings.updated", actor=actor, details={"keys": sorted(changes)})
        return settings

    def _validate_settings(self, settings: Mapping[str, Any]) -> None:
        email = settings["allowed_google_email"]
        if not isinstance(email, str) or (email and ("@" not in email or len(email) > 254)):
            raise ValueError("allowed_google_email must be a valid email address")
        company_domain = settings["company_google_domain"]
        if not isinstance(company_domain, str) or not company_domain or "." not in company_domain:
            raise ValueError("company_google_domain must be a valid domain")
        super_email = settings["super_admin_google_email"]
        if not isinstance(super_email, str) or "@" not in super_email or len(super_email) > 254:
            raise ValueError("super_admin_google_email must be a valid email address")
        oauth_base = settings["google_oauth_base_url"]
        if not isinstance(oauth_base, str) or not (
            oauth_base.startswith("http://127.0.0.1:") or oauth_base.startswith("https://")
        ):
            raise ValueError("google_oauth_base_url must be loopback HTTP or HTTPS")
        timezone_name = settings["timezone"]
        if not isinstance(timezone_name, str) or not timezone_name or len(timezone_name) > 100:
            raise ValueError("timezone is required")
        hour = settings["daily_collection_hour"]
        if not isinstance(hour, int) or isinstance(hour, bool) or not 0 <= hour <= 23:
            raise ValueError("daily_collection_hour must be between 0 and 23")
        for key in ("data_root", "runtime_root"):
            path = settings[key]
            if not isinstance(path, str) or not Path(path).is_absolute():
                raise ValueError(f"{key} must be an absolute path")
        folder_id = settings["google_drive_backup_folder_id"]
        if not isinstance(folder_id, str) or (folder_id and not _DRIVE_ID.fullmatch(folder_id)):
            raise ValueError("google_drive_backup_folder_id is invalid")
        folder_url = settings["google_drive_backup_folder_url"]
        if not isinstance(folder_url, str) or (
            folder_url and not folder_url.startswith("https://drive.google.com/drive/folders/")
        ):
            raise ValueError("google_drive_backup_folder_url must be a Google Drive folder URL")
        enabled = settings["google_drive_backup_enabled"]
        if not isinstance(enabled, bool):
            raise ValueError("google_drive_backup_enabled must be a boolean")
        team_id = settings["slack_expected_team_id"]
        if not isinstance(team_id, str) or not _TEAM_ID.fullmatch(team_id):
            raise ValueError("slack_expected_team_id is invalid")
        github_organization = settings["github_organization"]
        if not isinstance(github_organization, str) or not _GITHUB_ORG.fullmatch(github_organization):
            raise ValueError("github_organization is invalid")
        for key in ("local_model_provider", "local_model_name", "local_model_endpoint"):
            if not isinstance(settings[key], str) or len(settings[key]) > 500:
                raise ValueError(f"{key} must be a string")

    def set_admin_password(self, password: str) -> None:
        if not self.setup_required():
            raise RuntimeError("administrator password already exists")
        self._validate_password(password)
        salt = secrets.token_bytes(16)
        digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
        record = {"algorithm": "scrypt", "salt": _b64encode(salt), "digest": _b64encode(digest)}
        _atomic_private_write(self.password_path, json.dumps(record, sort_keys=True) + "\n")
        self.audit("admin.bootstrapped", actor="owner")

    def verify_admin_password(self, password: str) -> bool:
        if self.setup_required() or len(password) > 1024:
            return False
        return self._verify_against(self.password_path, password)

    def staff_password_set(self) -> bool:
        return self.staff_password_path.exists()

    def set_staff_password(self, password: str, *, actor: str = "owner") -> None:
        """Set or replace the staff password.

        Unlike the administrator password this one may be replaced. That is the
        whole difference between the two doors: the owner's password is set
        once at bootstrap and is a secret about a person, while this is a value
        that gets handed around and therefore has to be rotatable without
        touching the install.
        """
        self._validate_password(password)
        salt = secrets.token_bytes(16)
        digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
        record = {"algorithm": "scrypt", "salt": _b64encode(salt), "digest": _b64encode(digest)}
        replaced = self.staff_password_set()
        _atomic_private_write(self.staff_password_path, json.dumps(record, sort_keys=True) + "\n")
        self.audit("admin.staff_password_set", actor=actor, details={"replaced": replaced})

    def clear_staff_password(self, *, actor: str = "owner") -> bool:
        """Close the second door. Returns whether there was one to close."""
        if not self.staff_password_set():
            return False
        self.staff_password_path.unlink()
        self.audit("admin.staff_password_cleared", actor=actor)
        return True

    def verify_staff_password(self, password: str) -> bool:
        if not self.staff_password_set() or len(password) > 1024:
            return False
        return self._verify_against(self.staff_password_path, password)

    def _verify_against(self, path: Path, password: str) -> bool:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            salt = _b64decode(record["salt"])
            expected = _b64decode(record["digest"])
            actual = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
            return hmac.compare_digest(actual, expected)
        except (KeyError, ValueError, TypeError, OSError, json.JSONDecodeError):
            return False

    @staticmethod
    def _validate_password(password: str) -> None:
        if len(password) < 12:
            raise ValueError("administrator password must be at least 12 characters")
        if len(password) > 1024:
            raise ValueError("administrator password is too long")

    def _session_key(self) -> bytes:
        if not self.session_key_path.exists():
            _atomic_private_write(self.session_key_path, _b64encode(secrets.token_bytes(32)) + "\n")
        return _b64decode(self.session_key_path.read_text(encoding="utf-8").strip())

    # An agent session is long-lived on purpose: the person who has to create it
    # should not be interrupted for it four times a year, let alone monthly. The
    # length is a judgement about someone's attention, not a security boundary -
    # a leaked cookie is as bad at thirty days as at ninety. What bounds it is
    # that the server is loopback-only, the cookie is httponly, and a single
    # agent can be cut off without touching the others.
    AGENT_SESSION_SECONDS = 90 * 24 * 60 * 60
    AGENT_SUBJECT_PREFIX = "agent:"
    # A closed roster, so a typo cannot quietly mint a sixth identity that
    # nobody recognises and nobody thinks to revoke. Judgement roles are absent
    # deliberately: they direct work rather than run it, and one of them cannot
    # hold a token at all.
    AGENT_NAMES = ("noa", "boa", "doa", "roa", "soa")

    def agent_generations(self) -> dict[str, int]:
        """How many times each agent's sessions have been revoked."""
        try:
            loaded = json.loads(self.agent_generations_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(loaded, dict):
            return {}
        return {
            str(name): int(value)
            for name, value in loaded.items()
            if isinstance(value, int) and not isinstance(value, bool)
        }

    def agent_subject(self, name_or_subject: str) -> str:
        """The one spelling of an agent's session subject.

        Callers arrive with either form: the CLI prefixes, a direct caller often
        does not. Accepting both and validating once means a generation can no
        longer be raised on a key no token was ever signed with, which reads as a
        successful revocation and is not one.
        """
        name = str(name_or_subject or "").strip()
        if name.startswith(self.AGENT_SUBJECT_PREFIX):
            name = name[len(self.AGENT_SUBJECT_PREFIX):]
        if name not in self.AGENT_NAMES:
            raise ValueError(f"unknown agent: {name_or_subject}")
        return f"{self.AGENT_SUBJECT_PREFIX}{name}"

    def revoke_agent(self, subject: str, *, actor: str = "owner") -> int:
        """Cut off one agent, and only that one.

        A session token is a signed envelope that the server does not keep a
        copy of, so there is nothing to delete. Rotating the signing key would
        end every session at once, which makes a long life unsafe for everyone
        because of one agent. Instead each subject carries a generation: the
        token records the generation it was issued under, and raising one
        subject's generation leaves every other token untouched.
        """
        subject = self.agent_subject(subject)
        generations = self.agent_generations()
        generations[subject] = generations.get(subject, 0) + 1
        _atomic_private_write(
            self.agent_generations_path,
            json.dumps(generations, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
        self.audit("agent.revoked", actor=actor, details={"subject": subject})
        return generations[subject]

    def issue_agent_session(self, name: str, *, actor: str = "owner") -> tuple[str, str]:
        """A long-lived session for one agent. The caller must not log it."""
        subject = self.agent_subject(name)
        token, csrf = self.create_session(
            subject=subject,
            role="agent",
            auth_method="agent_token",
            lifetime_seconds=self.AGENT_SESSION_SECONDS,
        )
        # The token itself is never recorded - only that one was made, which is
        # what an audit trail needs to show.
        # Who asked for it, not a fixed "owner": issuing another principal's
        # session is the one CLI operation that crosses the web boundary rather
        # than sitting beside it, so the trail has to name a person.
        self.audit("agent.session_issued", actor=actor, details={"subject": subject})
        return token, csrf

    def create_session(
        self,
        *,
        subject: str = "owner",
        email: str | None = None,
        role: str = "super_admin",
        auth_method: str = "local_emergency",
        lifetime_seconds: int = 43_200,
    ) -> tuple[str, str]:
        now = int(datetime.now(timezone.utc).timestamp())
        csrf = secrets.token_urlsafe(24)
        payload = {
            "sub": subject,
            "email": email,
            "role": role,
            "auth_method": auth_method,
            "iat": now,
            "exp": now + lifetime_seconds,
            "csrf": csrf,
        }
        if subject.startswith(self.AGENT_SUBJECT_PREFIX):
            payload["gen"] = self.agent_generations().get(subject, 0)
        encoded = _b64encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
        signature = _b64encode(hmac.new(self._session_key(), encoded.encode("ascii"), hashlib.sha256).digest())
        return f"{encoded}.{signature}", csrf

    def read_session(self, token: str | None) -> dict[str, Any] | None:
        if not token or "." not in token:
            return None
        encoded, signature = token.split(".", 1)
        expected = _b64encode(hmac.new(self._session_key(), encoded.encode("ascii"), hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected):
            return None
        try:
            payload = json.loads(_b64decode(encoded))
            now = int(datetime.now(timezone.utc).timestamp())
            subject = payload.get("sub")
            if not subject or int(payload.get("exp", 0)) < now:
                return None
            if str(subject).startswith(self.AGENT_SUBJECT_PREFIX):
                # A revoked agent's tokens are still correctly signed and still
                # unexpired. The generation is what makes them dead.
                if int(payload.get("gen", -1)) != self.agent_generations().get(str(subject), 0):
                    return None
            return payload
        except (ValueError, TypeError, json.JSONDecodeError):
            return None

    def save_secret(self, name: str, value: str, *, actor: str = "owner") -> None:
        if name not in SECRET_FILES:
            raise ValueError("unsupported secret")
        normalized = value.strip()
        if not normalized:
            raise ValueError("secret cannot be empty")
        if len(normalized) > 100_000:
            raise ValueError("secret is too large")
        if name == "google_oauth_client":
            normalized = self._validate_google_client(normalized)
        elif name == "google_token":
            normalized = self._validate_google_token(normalized)
        elif name == "slack_token" and not normalized.startswith("xoxp-"):
            raise ValueError("Slack collector requires a user token beginning with xoxp-")
        elif name == "notion_token" and not normalized.startswith(("ntn_", "secret_")):
            raise ValueError("Notion token must begin with ntn_ or secret_")
        elif name == "github_token" and not normalized.startswith(("github_pat_", "ghp_")):
            raise ValueError("GitHub token must be a fine-grained or classic personal access token")
        path = self.credentials / SECRET_FILES[name]
        _atomic_private_write(path, normalized + ("\n" if not normalized.endswith("\n") else ""))
        self.audit("secret.updated", actor=actor, details={"name": name})

    @staticmethod
    def _validate_google_client(value: str) -> str:
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as error:
            raise ValueError("Google OAuth client must be valid JSON") from error
        section = parsed.get("installed") or parsed.get("web")
        if not isinstance(section, dict):
            raise ValueError("Google OAuth JSON must contain installed or web credentials")
        required = {"client_id", "client_secret", "auth_uri", "token_uri"}
        if not required.issubset(section):
            raise ValueError("Google OAuth JSON is missing required fields")
        return json.dumps(parsed, ensure_ascii=False, indent=2, sort_keys=True)

    @staticmethod
    def _validate_google_token(value: str) -> str:
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as error:
            raise ValueError("Google token must be valid JSON") from error
        required = {"client_id", "client_secret", "refresh_token", "token_uri"}
        if not isinstance(parsed, dict) or not required.issubset(parsed):
            raise ValueError("Google token is missing required OAuth fields")
        return json.dumps(parsed, ensure_ascii=False, indent=2, sort_keys=True)

    def secret_status(self) -> dict[str, bool]:
        return {name: (self.credentials / filename).is_file() for name, filename in SECRET_FILES.items()}

    def secret_path(self, name: str) -> Path:
        if name not in SECRET_FILES:
            raise ValueError("unsupported secret")
        return self.credentials / SECRET_FILES[name]

    def create_oauth_state(
        self, next_path: str, *, purpose: str = "google_login", lifetime_seconds: int = 600
    ) -> str:
        if next_path not in {"/", "/backoffice"}:
            raise ValueError("invalid OAuth return path")
        if purpose not in {"google_login", "google_data"}:
            raise ValueError("invalid OAuth purpose")
        now = int(datetime.now(timezone.utc).timestamp())
        payload = {"purpose": purpose, "next": next_path, "iat": now, "exp": now + lifetime_seconds}
        encoded = _b64encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
        signature = _b64encode(hmac.new(self._session_key(), encoded.encode("ascii"), hashlib.sha256).digest())
        return f"{encoded}.{signature}"

    def read_oauth_state(self, state: str | None) -> dict[str, Any] | None:
        if not state or "." not in state:
            return None
        encoded, signature = state.split(".", 1)
        expected = _b64encode(hmac.new(self._session_key(), encoded.encode("ascii"), hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected):
            return None
        try:
            payload = json.loads(_b64decode(encoded))
            now = int(datetime.now(timezone.utc).timestamp())
            if payload.get("purpose") not in {"google_login", "google_data"} or int(payload.get("exp", 0)) < now:
                return None
            if payload.get("next") not in {"/", "/backoffice"}:
                return None
            return payload
        except (ValueError, TypeError, json.JSONDecodeError):
            return None

    def save_oauth_code_verifier(self, state: str, verifier: str) -> None:
        """Keep a PKCE verifier server-side; never place it in OAuth state."""
        if not state or not verifier or len(verifier) > 1024:
            raise ValueError("invalid OAuth PKCE transaction")
        key = hashlib.sha256(state.encode("utf-8")).hexdigest()
        _atomic_private_write(self.oauth_pkce_dir / key, verifier + "\n")

    def consume_oauth_code_verifier(self, state: str) -> str | None:
        """Return and remove the one-use PKCE verifier for this signed state."""
        key = hashlib.sha256(state.encode("utf-8")).hexdigest()
        path = self.oauth_pkce_dir / key
        try:
            value = path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return None
        path.unlink(missing_ok=True)
        return value or None

    # ------------------------------------------------- Notion seed pages

    MAX_NOTION_SEEDS = 200

    def load_notion_seeds(self) -> list[dict[str, Any]]:
        """The operator's list of Notion pages that must never be missed.

        A day's documents come from `/search` plus the data-source row query.
        A seed is the answer to "and check this one anyway": the collector
        retrieves each seed every run and captures it only if it changed in the
        window, so the list is cheap to hold and its cost is one request per
        entry per run.
        """
        if not self.notion_seeds_path.exists():
            return []
        stored = json.loads(self.notion_seeds_path.read_text(encoding="utf-8"))
        entries = stored.get("seeds") if isinstance(stored, dict) else stored
        if not isinstance(entries, list):
            raise RuntimeError("notion-seeds.json must hold a list of seeds")
        seeds: list[dict[str, Any]] = []
        for entry in entries:
            if not isinstance(entry, dict) or not entry.get("page_id"):
                continue
            seeds.append(
                {
                    "page_id": str(entry["page_id"]),
                    "label": str(entry.get("label") or ""),
                    "added_at": str(entry.get("added_at") or ""),
                    "added_by": str(entry.get("added_by") or ""),
                }
            )
        return seeds

    def notion_seed_ids(self) -> list[str]:
        """Just the ids, for the collector. Never raises on a malformed file."""
        try:
            return [seed["page_id"] for seed in self.load_notion_seeds()]
        except (OSError, ValueError, RuntimeError):
            # A seed list that cannot be read must not stop a collection. The
            # run loses its insurance, not its day.
            return []

    def add_notion_seed(
        self, value: str, *, label: str = "", actor: str = "owner"
    ) -> dict[str, Any]:
        """Add one seed from a Notion URL or a page id.

        A person pastes a URL; the collector needs an id. Accepting both and
        canonicalising here means the two never diverge, and a typo is refused
        at the moment it is typed rather than becoming a 404 in a nightly run.
        """
        from .links import canonical_notion_page_id

        text = (value or "").strip()
        page_id = canonical_notion_page_id(text)
        if page_id is None:
            raise ValueError("a Notion page URL or 32-character page id is required")
        seeds = self.load_notion_seeds()
        if any(seed["page_id"] == page_id for seed in seeds):
            raise ValueError("that page is already a seed")
        if len(seeds) >= self.MAX_NOTION_SEEDS:
            raise ValueError(f"at most {self.MAX_NOTION_SEEDS} seeds")
        entry = {
            "page_id": page_id,
            "label": (label or "").strip()[:200],
            "added_at": _utc_now(),
            "added_by": actor,
        }
        seeds.append(entry)
        self._write_notion_seeds(seeds)
        self.audit("notion_seed.added", actor=actor, details={"page_id": page_id})
        return entry

    def remove_notion_seed(self, page_id: str, *, actor: str = "owner") -> bool:
        seeds = self.load_notion_seeds()
        remaining = [seed for seed in seeds if seed["page_id"] != page_id]
        if len(remaining) == len(seeds):
            return False
        self._write_notion_seeds(remaining)
        self.audit("notion_seed.removed", actor=actor, details={"page_id": page_id})
        return True

    def _write_notion_seeds(self, seeds: list[dict[str, Any]]) -> None:
        _atomic_private_write(
            self.notion_seeds_path,
            json.dumps({"seeds": seeds}, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )

    # ------------------------------------------------- 사내 시스템 북마크

    MAX_BOOKMARKS = 300
    MAX_BOOKMARK_URL = 2000
    DEFAULT_BOOKMARK_GROUP = "기타"
    # The addresses the screen starts with, read off what people actually
    # paste in Slack rather than guessed. `rlwrld.co` is the internal
    # service domain -- inside the office network or on Tailscale only --
    # and the bare IPs and infra-node names are Tailscale-only.
    #
    # They are the file's initial content, not a floor: every write below
    # persists the whole list, so deleting or renaming one of these sticks.
    # scripts/bookmarks-seed-sync.py is how an install that already has a
    # bookmarks.json picks up entries added here later.
    SEEDED_BOOKMARKS: tuple[dict[str, str], ...] = (
        {
            "id": "bm_infra_gpu",
            "group": "인프라",
            "label": "GPU 대시보드",
            "url": "http://infra-node:8888/",
            "note": "카카오·AWS SKT·네이버 세 클러스터의 GPU 현황·배치·사용량",
        },
        {
            "id": "bm_infra_gpu_reserved",
            "group": "인프라",
            "label": "GPU — dedicated · reserved",
            "url": "http://infra-node:8889/",
            "note": "",
        },
        {
            "id": "bm_infra_gpu_legacy",
            "group": "인프라",
            "label": "GPU — 레거시 대시보드",
            "url": "http://infra-node:8090/",
            "note": "",
        },
        {
            "id": "bm_infra_slurm",
            "group": "인프라",
            "label": "Slurm · MLXP job 현황",
            "url": "http://210.109.83.131:8888/",
            "note": "running·pending job",
        },
        {
            "id": "bm_data_foundry",
            "group": "데이터 · 스토리지",
            "label": "Foundry",
            "url": "https://foundry.rlwrld.co/",
            "note": "AWS·Kakao·Naver 오브젝트 스토리지를 한 논리 경로로",
        },
        {
            "id": "bm_infra_storage",
            "group": "데이터 · 스토리지",
            "label": "Lightdash",
            "url": "https://lightdash.rlwrld.co/",
            "note": "전사 대시보드 통합 중. 예전 ts.net 주소에서 이사했다",
        },
        {
            "id": "bm_data_fsx",
            "group": "데이터 · 스토리지",
            "label": "FSx 용량 인벤토리",
            "url": "https://fsx-inventory.rlwrld.co/",
            "note": "",
        },
        {
            "id": "bm_dash_host",
            "group": "대시보드",
            "label": "사내 대시보드",
            "url": "https://dashboard.rlwrld.co/",
            "note": "",
        },
        {
            "id": "bm_dash_hw",
            "group": "대시보드",
            "label": "HW 스펙 · POC",
            "url": "https://hwdashboard.rlwrld.co/",
            "note": "",
        },
        {
            "id": "bm_dash_ops",
            "group": "대시보드",
            "label": "장비 · 자산 관리",
            "url": "https://ops.rlwrld.co/",
            "note": "",
        },
        {
            "id": "bm_dash_rd",
            "group": "대시보드",
            "label": "RD Resource",
            "url": "https://rd-resource.rlwrld.co/",
            "note": "",
        },
        {
            "id": "bm_tool_tree",
            "group": "도구",
            "label": "TREE — 작업 배포가능성 판정",
            "url": "https://tree.tailadd0bc.ts.net/",
            "note": "",
        },
        {
            "id": "bm_tool_trackio",
            "group": "도구",
            "label": "trackio — 실험 추적",
            "url": "http://100.57.101.80:10001/",
            "note": "",
        },
        {
            "id": "bm_tool_slides",
            "group": "도구",
            "label": "SlideSearcher",
            "url": "https://slidesearcher.rlwrld.co/",
            "note": "사내 PPT 검색",
        },
        {
            "id": "bm_tool_rxview",
            "group": "도구",
            "label": "RX 데이터 뷰어",
            "url": "http://100.127.185.54/",
            "note": "",
        },
        {
            "id": "bm_doc_rrc",
            "group": "문서",
            "label": "RRC 매뉴얼 · 런북",
            "url": "https://rrc.rlwrld.co/",
            "note": "",
        },
        {
            "id": "bm_doc_roadmap",
            "group": "문서",
            "label": "Platform Team Roadmap",
            "url": "https://app.notion.com/p/3ce6cbdff6f68086b8f7cc174bbb040d",
            "note": "로드맵 백오피스의 원본",
        },
        {
            "id": "bm_tool_slack",
            "group": "협업 도구",
            "label": "Slack",
            "url": "https://rlwrld.slack.com/",
            "note": "",
        },
        {
            "id": "bm_tool_notion",
            "group": "협업 도구",
            "label": "Notion — RLWRLD AI HOME",
            "url": "https://app.notion.com/p/1716cbdff6f680dca3a8d1b389ccb256",
            "note": "워크스페이스 최상위",
        },
        {
            "id": "bm_tool_github",
            "group": "협업 도구",
            "label": "GitHub — RLWRLD 조직",
            "url": "https://github.com/RLWRLD",
            "note": "",
        },
        {
            "id": "bm_tool_drive",
            "group": "협업 도구",
            "label": "Google Drive",
            "url": "https://drive.google.com/",
            "note": "",
        },
    )
    @staticmethod
    def _clean_bookmark_url(value: str) -> str:
        """Accept only an http(s) address, because the page turns this into a link.

        A `javascript:` or `data:` value in this field would be a stored
        script running in a super-admin's session, so the refusal is here --
        at the moment it is typed -- rather than in the renderer alone.
        """
        url = (value or "").strip()
        if not url:
            raise ValueError("주소가 필요합니다")
        if len(url) > AdminStore.MAX_BOOKMARK_URL:
            raise ValueError(f"주소는 {AdminStore.MAX_BOOKMARK_URL}자를 넘을 수 없습니다")
        lowered = url.lower()
        if not (lowered.startswith("http://") or lowered.startswith("https://")):
            raise ValueError("http:// 또는 https:// 로 시작하는 주소만 등록할 수 있습니다")
        if any(character in url for character in ("\n", "\r", "\t", " ")):
            raise ValueError("주소에 공백이나 줄바꿈이 들어 있습니다")
        return url

    @staticmethod
    def _clean_bookmark_text(value: str, *, limit: int = 120) -> str:
        return " ".join((value or "").split())[:limit]

    def load_bookmarks(self) -> list[dict[str, Any]]:
        """Links to the company systems, grouped, in the order they were added.

        Order is insertion order and nothing else: a list somebody built by
        hand should read back the way he built it, and re-sorting it on every
        load would move a link out from under the click that was aimed at it.
        """
        if not self.bookmarks_path.exists():
            return [dict(entry, added_at="", added_by="") for entry in self.SEEDED_BOOKMARKS]
        stored = json.loads(self.bookmarks_path.read_text(encoding="utf-8"))
        entries = stored.get("bookmarks") if isinstance(stored, dict) else stored
        if not isinstance(entries, list):
            raise RuntimeError("bookmarks.json must hold a list of bookmarks")
        bookmarks: list[dict[str, Any]] = []
        for entry in entries:
            if not isinstance(entry, dict) or not entry.get("id") or not entry.get("url"):
                continue
            bookmarks.append(
                {
                    "id": str(entry["id"]),
                    "group": str(entry.get("group") or self.DEFAULT_BOOKMARK_GROUP),
                    "label": str(entry.get("label") or ""),
                    "url": str(entry["url"]),
                    "note": str(entry.get("note") or ""),
                    "added_at": str(entry.get("added_at") or ""),
                    "added_by": str(entry.get("added_by") or ""),
                }
            )
        return bookmarks

    def bookmark_groups(self) -> list[dict[str, Any]]:
        """The same list, bucketed, with the groups in first-seen order."""
        groups: list[dict[str, Any]] = []
        index: dict[str, dict[str, Any]] = {}
        for bookmark in self.load_bookmarks():
            name = bookmark["group"]
            bucket = index.get(name)
            if bucket is None:
                bucket = {"group": name, "bookmarks": []}
                index[name] = bucket
                groups.append(bucket)
            bucket["bookmarks"].append(bookmark)
        return groups

    def add_bookmark(
        self,
        url: str,
        *,
        label: str = "",
        group: str = "",
        note: str = "",
        actor: str = "owner",
    ) -> dict[str, Any]:
        bookmarks = self.load_bookmarks()
        if len(bookmarks) >= self.MAX_BOOKMARKS:
            raise ValueError(f"북마크는 최대 {self.MAX_BOOKMARKS}개까지입니다")
        clean_url = self._clean_bookmark_url(url)
        if any(entry["url"] == clean_url for entry in bookmarks):
            raise ValueError("이미 등록된 주소입니다")
        entry = {
            "id": f"bm_{secrets.token_hex(6)}",
            "group": self._clean_bookmark_text(group, limit=60) or self.DEFAULT_BOOKMARK_GROUP,
            "label": self._clean_bookmark_text(label) or clean_url,
            "url": clean_url,
            "note": self._clean_bookmark_text(note, limit=200),
            "added_at": _utc_now(),
            "added_by": actor,
        }
        bookmarks.append(entry)
        self._write_bookmarks(bookmarks)
        self.audit("bookmark.added", actor=actor, details={"id": entry["id"], "url": clean_url})
        return entry

    def update_bookmark(
        self, bookmark_id: str, changes: Mapping[str, Any], *, actor: str = "owner"
    ) -> dict[str, Any]:
        bookmarks = self.load_bookmarks()
        target = next((entry for entry in bookmarks if entry["id"] == bookmark_id), None)
        if target is None:
            raise KeyError(bookmark_id)
        unknown = sorted(set(changes) - {"group", "label", "url", "note"})
        if unknown:
            raise ValueError(f"unknown bookmark fields: {', '.join(unknown)}")
        if "url" in changes:
            clean_url = self._clean_bookmark_url(str(changes["url"]))
            if any(
                entry["url"] == clean_url and entry["id"] != bookmark_id for entry in bookmarks
            ):
                raise ValueError("이미 등록된 주소입니다")
            target["url"] = clean_url
        if "group" in changes:
            target["group"] = (
                self._clean_bookmark_text(str(changes["group"]), limit=60)
                or self.DEFAULT_BOOKMARK_GROUP
            )
        if "label" in changes:
            target["label"] = self._clean_bookmark_text(str(changes["label"])) or target["url"]
        if "note" in changes:
            target["note"] = self._clean_bookmark_text(str(changes["note"]), limit=200)
        self._write_bookmarks(bookmarks)
        self.audit("bookmark.updated", actor=actor, details={"id": bookmark_id})
        return target

    def remove_bookmark(self, bookmark_id: str, *, actor: str = "owner") -> bool:
        bookmarks = self.load_bookmarks()
        remaining = [entry for entry in bookmarks if entry["id"] != bookmark_id]
        if len(remaining) == len(bookmarks):
            return False
        self._write_bookmarks(remaining)
        self.audit("bookmark.removed", actor=actor, details={"id": bookmark_id})
        return True

    def _write_bookmarks(self, bookmarks: list[dict[str, Any]]) -> None:
        # sort_keys inside each entry only -- the list itself keeps its order,
        # which is the order the operator put the links in.
        _atomic_private_write(
            self.bookmarks_path,
            json.dumps({"bookmarks": bookmarks}, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
        )

    def audit(self, action: str, *, actor: str, details: Mapping[str, Any] | None = None) -> None:
        entry = {"at": _utc_now(), "actor": actor, "action": action, "details": dict(details or {})}
        descriptor = os.open(self.audit_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            with os.fdopen(descriptor, "a", encoding="utf-8") as stream:
                stream.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            self.audit_path.chmod(0o600)

    def read_audit(self, limit: int = 100) -> list[dict[str, Any]]:
        if not self.audit_path.exists():
            return []
        lines = self.audit_path.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines[-limit:]][::-1]
