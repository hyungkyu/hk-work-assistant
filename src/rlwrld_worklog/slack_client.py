from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Mapping, Protocol


class SlackApiError(RuntimeError):
    """A Slack API failure whose message never includes credentials."""

    def __init__(self, message: str, *, method: str | None = None, code: str | None = None) -> None:
        super().__init__(message)
        self.method = method
        self.code = code


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: dict[str, Any]


class Transport(Protocol):
    def get(self, url: str, headers: Mapping[str, str], params: Mapping[str, Any]) -> HttpResponse: ...


class UrllibTransport:
    def get(self, url: str, headers: Mapping[str, str], params: Mapping[str, Any]) -> HttpResponse:
        encoded = urllib.parse.urlencode({key: value for key, value in params.items() if value is not None})
        request = urllib.request.Request(f"{url}?{encoded}", headers=dict(headers), method="GET")
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                raw_body = response.read()
                return HttpResponse(response.status, dict(response.headers), json.loads(raw_body))
        except urllib.error.HTTPError as error:
            raw_body = error.read()
            try:
                body = json.loads(raw_body)
            except (json.JSONDecodeError, UnicodeDecodeError):
                body = {"ok": False, "error": "http_error"}
            return HttpResponse(error.code, dict(error.headers), body)


class SlackClient:
    def __init__(
        self,
        token: str,
        *,
        transport: Transport | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        max_attempts: int = 6,
    ) -> None:
        if not token.startswith("xoxp-"):
            raise ValueError("Slack collector requires a user OAuth token beginning with xoxp-")
        self._token = token
        self._transport = transport or UrllibTransport()
        self._sleeper = sleeper
        self._max_attempts = max_attempts
        self._last_response_headers: dict[str, str] = {}
        self.rate_limit_hits = 0
        self.call_counts: dict[str, int] = {}

    @property
    def last_response_headers(self) -> dict[str, str]:
        return dict(self._last_response_headers)

    def call(self, method: str, **params: Any) -> dict[str, Any]:
        url = f"https://slack.com/api/{method}"
        headers = {"Authorization": f"Bearer {self._token}", "User-Agent": "rlwrld-worklog/0.1"}
        self.call_counts[method] = self.call_counts.get(method, 0) + 1
        for attempt in range(1, self._max_attempts + 1):
            response = self._transport.get(url, headers, params)
            if response.status == 429:
                self.rate_limit_hits += 1
                if attempt == self._max_attempts:
                    raise SlackApiError(
                        f"Slack method {method} remained rate limited",
                        method=method,
                        code="ratelimited",
                    )
                retry_after = max(1, int(response.headers.get("Retry-After", "1")))
                self._sleeper(float(retry_after))
                continue
            if response.status >= 400:
                raise SlackApiError(
                    f"Slack method {method} failed with HTTP {response.status}",
                    method=method,
                    code="http_error",
                )
            if not response.body.get("ok"):
                code = response.body.get("error", "unknown_error")
                raise SlackApiError(f"Slack method {method} failed: {code}", method=method, code=str(code))
            self._last_response_headers = {str(key).lower(): str(value) for key, value in response.headers.items()}
            return response.body
        raise AssertionError("unreachable")

    def iter_pages(
        self,
        method: str,
        *,
        result_key: str,
        limit: int = 200,
        **params: Any,
    ) -> Iterator[dict[str, Any]]:
        cursor: str | None = None
        while True:
            body = self.call(method, limit=limit, cursor=cursor, **params)
            if not isinstance(body.get(result_key, []), list):
                raise SlackApiError(
                    f"Slack method {method} returned an invalid {result_key} value",
                    method=method,
                    code="invalid_response",
                )
            yield body
            cursor = body.get("response_metadata", {}).get("next_cursor") or None
            if not cursor:
                return

    def iter_search_messages(self, query: str) -> Iterator[dict[str, Any]]:
        cursor = "*"
        while True:
            body = self.call(
                "search.messages",
                query=query,
                count=100,
                cursor=cursor,
                highlight=False,
                sort="timestamp",
                sort_dir="asc",
            )
            matches = body.get("messages", {}).get("matches", [])
            if not isinstance(matches, list):
                raise SlackApiError(
                    "Slack method search.messages returned an invalid matches value",
                    method="search.messages",
                    code="invalid_response",
                )
            yield body
            cursor = body.get("response_metadata", {}).get("next_cursor") or ""
            if not cursor:
                return
