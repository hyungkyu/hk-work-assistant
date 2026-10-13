"""One Google token, two callers, and neither may narrow it for the other.

2026-10-13: the backoffice's 새로고침 failed with 403 insufficient scopes on
the roster sheet while the terminal read the same sheet fine. The calendar
collector had refreshed the shared token last, asking only for calendar, and
the file then said calendar.readonly and nothing else.
"""

from __future__ import annotations

import json

import pytest

from rlwrld_worklog import google_auth

CAL = google_auth.CALENDAR_READONLY_SCOPE
DRIVE = google_auth.DRIVE_READONLY_SCOPE


class FakeCredentials:
    """Records what it was asked for; refreshing grants exactly that."""

    created_with: list[str] = []
    refreshed = 0

    def __init__(self, scopes, *, expired=False):
        self.scopes = list(scopes)
        self.expired = expired
        self.refresh_token = "r"
        self.valid = True

    @classmethod
    def from_authorized_user_file(cls, path, scopes):
        cls.created_with = list(scopes)
        saved = json.loads(open(path).read())
        return cls(scopes, expired=saved.get("_expired", False))

    def refresh(self, request):
        type(self).refreshed += 1
        self.expired = False

    def to_json(self):
        return json.dumps({"scopes": self.scopes, "refresh_token": "r"})


@pytest.fixture()
def fake(monkeypatch):
    import google.oauth2.credentials as module

    FakeCredentials.created_with = []
    FakeCredentials.refreshed = 0
    monkeypatch.setattr(module, "Credentials", FakeCredentials)
    return FakeCredentials


def _token(tmp_path, scopes, *, expired=False):
    path = tmp_path / "google-token.json"
    path.write_text(json.dumps({"scopes": scopes, "refresh_token": "r", "_expired": expired}))
    return path


def test_a_refresh_keeps_what_the_token_already_had(fake, tmp_path) -> None:
    """The calendar collector refreshing must not take drive away."""
    path = _token(tmp_path, [CAL, DRIVE], expired=True)
    google_auth.load_credentials(path, [CAL])
    assert sorted(fake.created_with) == sorted([CAL, DRIVE])
    assert sorted(json.loads(path.read_text())["scopes"]) == sorted([CAL, DRIVE])


def test_a_token_missing_this_callers_scope_is_refreshed_before_it_expires(fake, tmp_path) -> None:
    """The case that broke: unexpired, narrowed to calendar, asked for drive."""
    path = _token(tmp_path, [CAL], expired=False)
    google_auth.load_credentials(path, [DRIVE])
    assert fake.refreshed == 1, "an unexpired token without the scope is refreshed anyway"
    assert sorted(json.loads(path.read_text())["scopes"]) == sorted([CAL, DRIVE])


def test_a_token_that_already_has_the_scope_is_left_alone(fake, tmp_path) -> None:
    path = _token(tmp_path, [CAL, DRIVE], expired=False)
    google_auth.load_credentials(path, [DRIVE])
    assert fake.refreshed == 0
