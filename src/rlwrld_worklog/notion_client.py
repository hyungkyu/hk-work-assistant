"""Read-only Notion API client with an explicit transient-retry policy.

The policy exists because of one production failure: a bare
``TimeoutError: The read operation timed out`` escaped this module during a
62-minute capture and killed the whole Notion source after 9,005 raw pages had
already been archived. Two rules follow from that.

  1. **Nothing bare escapes.** Every network or decoding failure this module
     can observe leaves as a :class:`NotionApiError`. A caller that isolates
     ``NotionApiError`` isolates every failure, so a transport hiccup can never
     again unwind past a collector's per-object boundary.
  2. **A retry is only for a request that never got an answer.** Timeouts,
     dropped connections, DNS and protocol faults, and HTTP 408/425/429/5xx are
     retried with ``Retry-After`` honoured or bounded exponential backoff.
     A definitive 4xx (400/401/403/404/...) is *not*: the API answered, and
     repeating the question only wastes the run's budget.

On exhaustion the error carries ``transient=True``, the retry class, the
attempt count and the status where one is known, which is what lets a collector
tell "this object is gone" apart from "this object is unresolved" and hold its
checkpoint accordingly. Messages name the method and path only: never the
token, never the query string.
"""

from __future__ import annotations

import http.client
import json
import math
import random
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Iterator


NOTION_API_VERSION = "2026-03-11"

DEFAULT_MAX_ATTEMPTS = 6
DEFAULT_TIMEOUT = 60.0
DEFAULT_BACKOFF_BASE = 1.0
DEFAULT_BACKOFF_CAP = 30.0
# A server may name a Retry-After far beyond a daily run's patience. It is
# honoured up to this clamp and no further; the request is simply retried
# sooner than asked rather than parking the whole capture.
DEFAULT_MAX_RETRY_AFTER = 60.0

#: HTTP statuses below 500 that mean "no answer yet", not "no".
TRANSIENT_STATUSES = frozenset({408, 425, 429})


def _http_retry_class(status: int) -> str | None:
    """The retry class for an HTTP status, or None if it is a final answer."""
    if status == 429:
        return "rate_limit"
    if status == 408:
        return "request_timeout"
    if status == 425:
        return "too_early"
    if status >= 500:
        return "server_error"
    return None


def _network_retry_class(error: BaseException) -> str | None:
    """The retry class for a transport failure, or None if it is permanent.

    ``URLError`` wraps its cause in ``.reason``; a bare ``TimeoutError`` (which
    is what ``socket.timeout`` is on this Python) or an ``http.client``
    protocol error arrives unwrapped. Both shapes are classified here.
    """
    reason: Any = error.reason if isinstance(error, urllib.error.URLError) else error
    # A certificate that does not verify will not verify on the next attempt.
    if isinstance(reason, ssl.SSLCertVerificationError):
        return None
    if isinstance(reason, TimeoutError):  # socket.timeout is an alias of this
        return "timeout"
    if isinstance(reason, ConnectionError):
        return "connection"
    if isinstance(reason, (socket.gaierror, socket.herror)):
        return "dns"
    if isinstance(reason, http.client.HTTPException):
        return "protocol"
    if isinstance(reason, OSError):
        return "network"
    return None


def _parse_retry_after(value: Any) -> float | None:
    """Seconds to wait from a ``Retry-After`` header: delta-seconds or HTTP-date."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        seconds = float(text)
    except ValueError:
        pass
    else:
        return max(0.0, seconds) if math.isfinite(seconds) else None
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError, OverflowError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


def _default_jitter(delay: float) -> float:
    """Decorrelate retries across concurrent callers. Injectable for tests."""
    return random.uniform(0.0, delay * 0.25)


class NotionApiError(RuntimeError):
    """A Notion API failure whose message never includes credentials.

    ``transient`` distinguishes the two outcomes a caller must treat
    differently: ``False`` is a definitive answer from the API (gone,
    forbidden, malformed request), ``True`` means the retry budget ran out
    without ever getting one, so the object's true state is still unknown.
    """

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: str | None = None,
        transient: bool = False,
        retry_class: str | None = None,
        attempts: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.transient = transient
        self.retry_class = retry_class
        self.attempts = attempts

    @property
    def resolution(self) -> str:
        """``"unresolved"`` when retries were exhausted, else ``"permanent"``."""
        return "unresolved" if self.transient else "permanent"


class NotionClient:
    def __init__(
        self,
        token: str,
        *,
        sleeper: Callable[[float], None] = time.sleep,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        api_version: str = NOTION_API_VERSION,
        timeout: float = DEFAULT_TIMEOUT,
        backoff_base: float = DEFAULT_BACKOFF_BASE,
        backoff_cap: float = DEFAULT_BACKOFF_CAP,
        max_retry_after: float = DEFAULT_MAX_RETRY_AFTER,
        jitter: Callable[[float], float] | None = None,
    ) -> None:
        if not token.strip():
            raise ValueError("Notion token must not be empty")
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int):
            raise ValueError("max_attempts must be an integer")
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        timeout = _positive("timeout", timeout)
        backoff_base = _positive("backoff_base", backoff_base)
        backoff_cap = _positive("backoff_cap", backoff_cap)
        if backoff_cap < backoff_base:
            raise ValueError("backoff_cap must be at least backoff_base")
        max_retry_after = _non_negative("max_retry_after", max_retry_after)
        self._token = token
        self._sleeper = sleeper
        self._max_attempts = max_attempts
        self._api_version = api_version
        self._timeout = timeout
        self._backoff_base = backoff_base
        self._backoff_cap = backoff_cap
        self._max_retry_after = max_retry_after
        self._jitter = jitter if jitter is not None else _default_jitter
        # Run accounting, reported in the manifest. `rate_limit_hits` keeps its
        # original meaning (429s seen); the rest are new transient accounting.
        self.rate_limit_hits = 0
        self.transient_retries = 0
        self.retry_counts: dict[str, int] = {}
        self.exhausted_requests = 0
        self.call_counts: dict[str, int] = {}

    @property
    def max_attempts(self) -> int:
        return self._max_attempts

    # ------------------------------------------------------------ internals

    def _sanitize(self, text: str) -> str:
        """Last-resort guard: a message must never carry the bearer token."""
        return text.replace(self._token, "<redacted>") if self._token else text

    def _count_retry(self, retry_class: str) -> None:
        self.retry_counts[retry_class] = self.retry_counts.get(retry_class, 0) + 1
        self.transient_retries += 1

    def _delay(self, attempt: int, *, retry_after: Any = None) -> float:
        """Retry-After when the server named one, else bounded exponential backoff."""
        hinted = _parse_retry_after(retry_after)
        if hinted is not None:
            return min(hinted, self._max_retry_after)
        base = min(self._backoff_cap, self._backoff_base * (2 ** (attempt - 1)))
        return max(0.0, base + self._jitter(base))

    # ----------------------------------------------------------------- call

    def call(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"https://api.notion.com/v1{path}"
        if query:
            url += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Notion-Version": self._api_version,
            "Content-Type": "application/json",
            "User-Agent": "rlwrld-worklog/0.1",
        }
        encoded = json.dumps(body).encode() if body is not None else None
        self.call_counts[path] = self.call_counts.get(path, 0) + 1

        status: int | None = None
        code: str | None = None
        retry_class: str | None = None
        detail = "no response"
        cause: BaseException | None = None

        for attempt in range(1, self._max_attempts + 1):
            request = urllib.request.Request(url, data=encoded, headers=headers, method=method)
            try:
                with urllib.request.urlopen(request, timeout=self._timeout) as response:
                    return json.loads(response.read())
            except urllib.error.HTTPError as error:
                # HTTPError is a URLError subclass, so it must be caught first.
                cause = error
                status = error.code
                code = _error_code(error)
                attempt_class = _http_retry_class(error.code)
                if attempt_class is None:
                    raise NotionApiError(
                        self._sanitize(
                            f"Notion request {method} {path} failed with HTTP {error.code}: "
                            f"{code or 'unknown'}"
                        ),
                        status=error.code,
                        code=code,
                        transient=False,
                        attempts=attempt,
                    ) from error
                retry_class = attempt_class
                detail = f"HTTP {error.code}: {code or 'unknown'}"
                if retry_class == "rate_limit":
                    self.rate_limit_hits += 1
                if attempt >= self._max_attempts:
                    break
                self._count_retry(retry_class)
                self._sleeper(self._delay(attempt, retry_after=error.headers.get("Retry-After")))
            except (OSError, http.client.HTTPException) as error:
                # Covers TimeoutError, ConnectionError, socket.gaierror, ssl
                # errors and URLError, all of which are OSError subclasses, plus
                # the http.client protocol errors that are not.
                cause = error
                status = None
                code = None
                attempt_class = _network_retry_class(error)
                if attempt_class is None:
                    raise NotionApiError(
                        self._sanitize(
                            f"Notion request {method} {path} failed: {_describe(error)}"
                        ),
                        transient=False,
                        attempts=attempt,
                    ) from error
                retry_class = attempt_class
                detail = f"{attempt_class} failure ({_describe(error)})"
                if attempt >= self._max_attempts:
                    break
                self._count_retry(retry_class)
                self._sleeper(self._delay(attempt))
            except (json.JSONDecodeError, UnicodeDecodeError) as error:
                # A body that is not the JSON it claims to be: a truncated read
                # or an intercepting proxy. Worth one more ask, and it must not
                # escape as a bare ValueError either.
                cause = error
                status = None
                code = None
                retry_class = "decode"
                detail = f"malformed response body ({type(error).__name__})"
                if attempt >= self._max_attempts:
                    break
                self._count_retry(retry_class)
                self._sleeper(self._delay(attempt))

        self.exhausted_requests += 1
        raise NotionApiError(
            self._sanitize(
                f"Notion request {method} {path} did not complete after {self._max_attempts} "
                f"attempts; last failure was {detail}. The object was left unfetched: it is "
                "recorded as an unresolved skip and the checkpoint is held below it, so the "
                "next run retries the same window. Retry later, or raise max_attempts or "
                "timeout if this repeats."
            ),
            status=status,
            code=code,
            transient=True,
            retry_class=retry_class,
            attempts=self._max_attempts,
        ) from cause

    # ------------------------------------------------------------ endpoints

    def iter_search(self, *, object_filter: str | None = None) -> Iterator[dict[str, Any]]:
        """Search everything the integration can see, newest edit first.

        `object_filter` is passed through to Notion's `filter.value`. Omitting
        it enumerates every object type the workspace exposes, which is what a
        complete daily capture needs: pages, databases and data sources.
        """
        cursor = None
        while True:
            body: dict[str, Any] = {
                "page_size": 100,
                "sort": {"direction": "descending", "timestamp": "last_edited_time"},
            }
            if object_filter:
                body["filter"] = {"property": "object", "value": object_filter}
            if cursor:
                body["start_cursor"] = cursor
            page = self.call("POST", "/search", body=body)
            yield page
            cursor = page.get("next_cursor") if page.get("has_more") else None
            if not cursor:
                return

    def iter_users(self) -> Iterator[dict[str, Any]]:
        cursor = None
        while True:
            page = self.call("GET", "/users", query={"page_size": 100, "start_cursor": cursor})
            yield page
            cursor = page.get("next_cursor") if page.get("has_more") else None
            if not cursor:
                return

    def retrieve_page(self, page_id: str) -> dict[str, Any]:
        return self.call("GET", f"/pages/{page_id}")

    def retrieve_data_source(self, data_source_id: str) -> dict[str, Any]:
        return self.call("GET", f"/data_sources/{data_source_id}")

    def retrieve_database(self, database_id: str) -> dict[str, Any]:
        return self.call("GET", f"/databases/{database_id}")

    def iter_block_children(self, block_id: str) -> Iterator[dict[str, Any]]:
        cursor = None
        while True:
            page = self.call(
                "GET",
                f"/blocks/{block_id}/children",
                query={"page_size": 100, "start_cursor": cursor},
            )
            yield page
            cursor = page.get("next_cursor") if page.get("has_more") else None
            if not cursor:
                return

    def iter_comments(self, block_id: str) -> Iterator[dict[str, Any]]:
        cursor = None
        while True:
            page = self.call(
                "GET",
                "/comments",
                query={"block_id": block_id, "page_size": 100, "start_cursor": cursor},
            )
            yield page
            cursor = page.get("next_cursor") if page.get("has_more") else None
            if not cursor:
                return

    def iter_property_items(self, page_id: str, property_id: str) -> Iterator[dict[str, Any]]:
        cursor = None
        while True:
            page = self.call(
                "GET",
                f"/pages/{page_id}/properties/{urllib.parse.quote(property_id, safe='')}",
                query={"page_size": 100, "start_cursor": cursor},
            )
            yield page
            cursor = page.get("next_cursor") if page.get("has_more") else None
            if not cursor:
                return


def _positive(name: str, value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number") from None
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{name} must be a finite positive number")
    return number


def _non_negative(name: str, value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number") from None
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{name} must be a finite non-negative number")
    return number


def _error_code(error: urllib.error.HTTPError) -> str | None:
    """Notion's machine-readable `code` from an error body, if it sent one."""
    try:
        raw = error.read()
    except Exception:  # pragma: no cover - the body is a courtesy, not a contract
        return None
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    code = value.get("code") if isinstance(value, dict) else None
    return str(code) if code else None


def _describe(error: BaseException) -> str:
    """A short, URL-free description of a transport failure.

    `URLError.__str__` renders only its reason, never the request URL, so no
    query string (and therefore no page id or cursor) can leak through here.
    """
    reason = error.reason if isinstance(error, urllib.error.URLError) else error
    text = str(reason).strip()
    name = type(reason).__name__ if isinstance(reason, BaseException) else type(error).__name__
    return f"{name}: {text}" if text else name
