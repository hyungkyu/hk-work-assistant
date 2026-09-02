"""NotionClient transient-retry policy.

Nothing here touches the network: `urllib.request.urlopen` is replaced by a
scripted queue of outcomes. The token is a fabricated string, and several tests
exist only to prove it never reaches an error message.
"""

from __future__ import annotations

import http.client
import io
import json
import socket
import ssl
import sys
import urllib.error
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rlwrld_worklog.notion_client import (  # noqa: E402
    NotionApiError,
    NotionClient,
    _network_retry_class,
    _parse_retry_after,
)

TOKEN = "ntn_synthetic_do_not_use_0123456789"
PAGE_ID = "01234567-89ab-cdef-0123-456789abcdef"


class FakeResponse(io.BytesIO):
    """Just enough of an `http.client.HTTPResponse` for `urlopen`'s context."""

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_: Any) -> bool:
        return False


def ok(body: dict[str, Any]) -> FakeResponse:
    return FakeResponse(json.dumps(body).encode())


def http_error(status: int, *, code: str = "bad", headers: dict[str, str] | None = None) -> urllib.error.HTTPError:
    body = json.dumps({"code": code, "message": "synthetic"}).encode()
    return urllib.error.HTTPError(
        "https://api.notion.com/v1/pages/x?secret=should-never-be-quoted",
        status,
        "synthetic",
        headers or {},
        io.BytesIO(body),
    )


class Opener:
    """A scripted `urlopen`: each entry is raised if it is an exception."""

    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = list(outcomes)
        self.requests: list[Any] = []
        self.timeouts: list[float] = []

    def __call__(self, request: Any, timeout: float | None = None) -> Any:
        self.requests.append(request)
        self.timeouts.append(timeout)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    @property
    def attempts(self) -> int:
        return len(self.requests)


def build(
    monkeypatch: pytest.MonkeyPatch,
    outcomes: list[Any],
    **kwargs: Any,
) -> tuple[NotionClient, Opener, list[float]]:
    opener = Opener(outcomes)
    monkeypatch.setattr("urllib.request.urlopen", opener)
    sleeps: list[float] = []
    kwargs.setdefault("jitter", lambda _delay: 0.0)  # deterministic backoff
    client = NotionClient(TOKEN, sleeper=sleeps.append, **kwargs)
    return client, opener, sleeps


# ------------------------------------------------------- transient categories


@pytest.mark.parametrize(
    ("failure", "retry_class"),
    [
        (TimeoutError("The read operation timed out"), "timeout"),
        (socket.timeout("timed out"), "timeout"),
        (urllib.error.URLError(TimeoutError("The read operation timed out")), "timeout"),
        (urllib.error.URLError(ConnectionResetError(104, "Connection reset by peer")), "connection"),
        (urllib.error.URLError(socket.gaierror(-3, "Temporary failure in name resolution")), "dns"),
        (urllib.error.URLError(OSError(101, "Network is unreachable")), "network"),
        (http.client.RemoteDisconnected("Remote end closed connection"), "connection"),
        (http.client.IncompleteRead(b"partial"), "protocol"),
        (ConnectionResetError(104, "Connection reset by peer"), "connection"),
        (http_error(408, code="request_timeout"), "request_timeout"),
        (http_error(425, code="too_early"), "too_early"),
        (http_error(429, code="rate_limited"), "rate_limit"),
        (http_error(500, code="internal_server_error"), "server_error"),
        (http_error(502, code="bad_gateway"), "server_error"),
        (http_error(503, code="service_unavailable"), "server_error"),
        (http_error(504, code="gateway_timeout"), "server_error"),
    ],
)
def test_every_transient_category_is_retried_and_then_succeeds(
    monkeypatch: pytest.MonkeyPatch, failure: BaseException, retry_class: str
) -> None:
    client, opener, sleeps = build(monkeypatch, [failure, ok({"object": "page"})])

    assert client.call("GET", f"/pages/{PAGE_ID}") == {"object": "page"}
    assert opener.attempts == 2
    assert sleeps == [1.0], "one bounded backoff, no jitter under an injected jitter of zero"
    assert client.retry_counts == {retry_class: 1}
    assert client.transient_retries == 1
    assert client.exhausted_requests == 0


def test_the_production_failure_is_retried_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    """The exact bare error that escaped a 62-minute run and killed the source."""
    client, opener, _ = build(
        monkeypatch,
        [TimeoutError("The read operation timed out"), ok({"results": [], "has_more": False})],
    )

    assert client.call("GET", "/blocks/block-1/children")["results"] == []
    assert opener.attempts == 2


def test_a_rate_limit_still_counts_as_a_rate_limit_hit(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _, _ = build(
        monkeypatch,
        [http_error(429, headers={"Retry-After": "2"}), ok({"object": "page"})],
    )
    client.call("GET", f"/pages/{PAGE_ID}")

    assert client.rate_limit_hits == 1, "the original accounting is preserved"
    assert client.retry_counts == {"rate_limit": 1}


# ------------------------------------------------------------ permanent 4xx


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422])
def test_a_permanent_status_is_not_retried(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    client, opener, sleeps = build(monkeypatch, [http_error(status, code="object_not_found")])

    with pytest.raises(NotionApiError) as caught:
        client.call("GET", f"/pages/{PAGE_ID}")

    assert opener.attempts == 1, "a definitive answer is not asked for twice"
    assert sleeps == []
    assert caught.value.status == status
    assert caught.value.code == "object_not_found"
    assert caught.value.transient is False
    assert caught.value.resolution == "permanent"
    assert caught.value.attempts == 1
    assert client.transient_retries == 0
    assert client.exhausted_requests == 0


def test_a_certificate_failure_is_permanent_not_transient(monkeypatch: pytest.MonkeyPatch) -> None:
    failure = urllib.error.URLError(ssl.SSLCertVerificationError("certificate verify failed"))
    client, opener, _ = build(monkeypatch, [failure])

    with pytest.raises(NotionApiError) as caught:
        client.call("GET", f"/pages/{PAGE_ID}")

    assert opener.attempts == 1, "a bad certificate will not become good on attempt two"
    assert caught.value.transient is False


def test_an_unknown_url_error_becomes_a_notion_error_not_a_bare_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing may escape this module unwrapped -- that was the whole bug."""
    client, opener, _ = build(monkeypatch, [urllib.error.URLError("unknown url type: htp")])

    with pytest.raises(NotionApiError) as caught:
        client.call("GET", f"/pages/{PAGE_ID}")

    assert opener.attempts == 1
    assert caught.value.transient is False
    assert isinstance(caught.value.__cause__, urllib.error.URLError)


# -------------------------------------------------------------- Retry-After


def test_retry_after_seconds_is_honored(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _, sleeps = build(
        monkeypatch,
        [http_error(429, headers={"Retry-After": "7"}), ok({"object": "page"})],
    )
    client.call("GET", f"/pages/{PAGE_ID}")

    assert sleeps == [7.0], "the server's number wins over the backoff curve"


def test_retry_after_http_date_is_honored(monkeypatch: pytest.MonkeyPatch) -> None:
    when = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=12), usegmt=True)
    client, _, sleeps = build(
        monkeypatch,
        [http_error(503, headers={"Retry-After": when}), ok({"object": "page"})],
    )
    client.call("GET", f"/pages/{PAGE_ID}")

    assert 8.0 <= sleeps[0] <= 12.0, f"an HTTP-date is read as a delay, got {sleeps[0]}"


def test_retry_after_is_clamped_so_a_run_is_not_parked(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _, sleeps = build(
        monkeypatch,
        [http_error(429, headers={"Retry-After": "3600"}), ok({"object": "page"})],
        max_retry_after=30.0,
    )
    client.call("GET", f"/pages/{PAGE_ID}")

    assert sleeps == [30.0]


def test_a_past_http_date_means_retry_now(monkeypatch: pytest.MonkeyPatch) -> None:
    when = format_datetime(datetime.now(timezone.utc) - timedelta(seconds=60), usegmt=True)
    client, _, sleeps = build(
        monkeypatch,
        [http_error(429, headers={"Retry-After": when}), ok({"object": "page"})],
    )
    client.call("GET", f"/pages/{PAGE_ID}")

    assert sleeps == [0.0]


@pytest.mark.parametrize(
    ("header", "expected"),
    [("5", 5.0), ("0", 0.0), ("-3", 0.0), ("", None), ("later", None), ("inf", None), ("nan", None)],
)
def test_retry_after_parsing(header: str, expected: float | None) -> None:
    assert _parse_retry_after(header) == expected


def test_a_garbage_retry_after_falls_back_to_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _, sleeps = build(
        monkeypatch,
        [http_error(429, headers={"Retry-After": "whenever"}), ok({"object": "page"})],
    )
    client.call("GET", f"/pages/{PAGE_ID}")

    assert sleeps == [1.0]


# --------------------------------------------------------- backoff & counts


def test_backoff_is_exponential_and_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    outcomes = [TimeoutError("timed out")] * 5 + [ok({"object": "page"})]
    client, opener, sleeps = build(
        monkeypatch, outcomes, max_attempts=6, backoff_base=1.0, backoff_cap=8.0
    )
    client.call("GET", f"/pages/{PAGE_ID}")

    assert sleeps == [1.0, 2.0, 4.0, 8.0, 8.0], "doubles, then holds at the cap"
    assert opener.attempts == 6


def test_injected_jitter_is_added_to_the_bounded_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _, sleeps = build(
        monkeypatch,
        [TimeoutError("timed out"), TimeoutError("timed out"), ok({})],
        jitter=lambda delay: delay / 2,
    )
    client.call("GET", f"/pages/{PAGE_ID}")

    assert sleeps == [1.5, 3.0]


def test_exhaustion_makes_exactly_max_attempts_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    client, opener, sleeps = build(
        monkeypatch, [TimeoutError("The read operation timed out")] * 4, max_attempts=4
    )

    with pytest.raises(NotionApiError) as caught:
        client.call("GET", f"/pages/{PAGE_ID}")

    assert opener.attempts == 4
    assert len(sleeps) == 3, "no sleep after the final attempt"
    assert caught.value.attempts == 4
    assert client.transient_retries == 3
    assert client.exhausted_requests == 1


def test_max_attempts_of_one_never_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    client, opener, sleeps = build(monkeypatch, [TimeoutError("timed out")], max_attempts=1)

    with pytest.raises(NotionApiError):
        client.call("GET", f"/pages/{PAGE_ID}")

    assert opener.attempts == 1
    assert sleeps == []


def test_call_counts_count_the_call_not_the_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _, _ = build(monkeypatch, [TimeoutError("t"), TimeoutError("t"), ok({})])
    client.call("GET", f"/pages/{PAGE_ID}")

    assert client.call_counts == {f"/pages/{PAGE_ID}": 1}
    assert client.transient_retries == 2


# ------------------------------------------------------ exhaustion contract


def test_exhaustion_raises_an_actionable_chained_classified_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cause = TimeoutError("The read operation timed out")
    client, _, _ = build(monkeypatch, [cause] * 3, max_attempts=3)

    with pytest.raises(NotionApiError) as caught:
        client.call("GET", "/blocks/block-1/children")

    error = caught.value
    assert error.transient is True
    assert error.resolution == "unresolved"
    assert error.retry_class == "timeout"
    assert error.attempts == 3
    assert error.status is None, "a timeout never produced a status"
    assert error.__cause__ is cause, "the original exception is chained, not swallowed"
    message = str(error)
    assert "GET /blocks/block-1/children" in message
    assert "3 attempts" in message
    assert "checkpoint" in message, "the message says what the failure costs"
    assert "max_attempts" in message, "and what an operator can change"


def test_exhaustion_on_a_5xx_keeps_the_status_and_code(monkeypatch: pytest.MonkeyPatch) -> None:
    # A fresh error per attempt: an HTTPError body can only be read once, which
    # is exactly how a real transport delivers them.
    client, _, _ = build(
        monkeypatch,
        [http_error(503, code="service_unavailable") for _ in range(2)],
        max_attempts=2,
    )

    with pytest.raises(NotionApiError) as caught:
        client.call("GET", f"/pages/{PAGE_ID}")

    assert caught.value.status == 503
    assert caught.value.code == "service_unavailable"
    assert caught.value.retry_class == "server_error"
    assert caught.value.transient is True


# --------------------------------------------------------------- no leakage


@pytest.mark.parametrize(
    "outcomes",
    [
        [TimeoutError("The read operation timed out")] * 2,
        [http_error(429, code="rate_limited")] * 2,
        [http_error(403, code="restricted_resource")],
        [urllib.error.URLError("unknown url type")],
        [urllib.error.URLError(ssl.SSLCertVerificationError("verify failed"))],
    ],
)
def test_no_error_message_ever_carries_the_token_or_the_query(
    monkeypatch: pytest.MonkeyPatch, outcomes: list[Any]
) -> None:
    client, _, _ = build(monkeypatch, outcomes, max_attempts=2)

    with pytest.raises(NotionApiError) as caught:
        client.call("GET", "/comments", query={"block_id": PAGE_ID, "start_cursor": "cursor-1"})

    message = str(caught.value)
    assert TOKEN not in message
    assert "ntn_" not in message
    assert "Bearer" not in message
    assert "start_cursor" not in message and "cursor-1" not in message
    assert "?" not in message, "no query string reaches an operational message"
    assert "api.notion.com" not in message


def test_the_token_is_scrubbed_even_if_a_reason_somehow_contains_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _, _ = build(monkeypatch, [urllib.error.URLError(f"unknown url type: {TOKEN}")])

    with pytest.raises(NotionApiError) as caught:
        client.call("GET", f"/pages/{PAGE_ID}")

    assert TOKEN not in str(caught.value)
    assert "<redacted>" in str(caught.value)


# ------------------------------------------------------ configuration guards


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_attempts": 0},
        {"max_attempts": -1},
        {"max_attempts": 2.5},
        {"max_attempts": True},
        {"max_attempts": "six"},
        {"timeout": 0},
        {"timeout": -5},
        {"timeout": float("inf")},
        {"timeout": "sixty"},
        {"backoff_base": 0},
        {"backoff_cap": 0.5, "backoff_base": 1.0},
        {"max_retry_after": -1},
    ],
)
def test_invalid_configuration_is_refused_at_construction(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        NotionClient(TOKEN, **kwargs)


def test_an_empty_token_is_still_refused() -> None:
    with pytest.raises(ValueError):
        NotionClient("   ")


def test_the_configured_timeout_reaches_urlopen(monkeypatch: pytest.MonkeyPatch) -> None:
    client, opener, _ = build(monkeypatch, [ok({})], timeout=12.5)
    client.call("GET", f"/pages/{PAGE_ID}")

    assert opener.timeouts == [12.5]


def test_valid_configuration_is_accepted() -> None:
    client = NotionClient(TOKEN, max_attempts=1, timeout=1, backoff_base=0.5, backoff_cap=0.5)
    assert client.max_attempts == 1


# ---------------------------------------------------------- classification


def test_network_classification_of_bare_and_wrapped_causes() -> None:
    assert _network_retry_class(TimeoutError()) == "timeout"
    assert _network_retry_class(urllib.error.URLError(TimeoutError())) == "timeout"
    assert _network_retry_class(socket.gaierror()) == "dns"
    assert _network_retry_class(ssl.SSLCertVerificationError("x")) is None
    assert _network_retry_class(urllib.error.URLError("a plain string reason")) is None
    assert _network_retry_class(ValueError("not a transport error")) is None


def test_a_body_that_is_not_json_still_produces_a_clean_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = urllib.error.HTTPError(
        "https://api.notion.com/v1/pages/x", 403, "forbidden", {}, io.BytesIO(b"<html>nope</html>")
    )
    client, _, _ = build(monkeypatch, [error])

    with pytest.raises(NotionApiError) as caught:
        client.call("GET", f"/pages/{PAGE_ID}")

    assert caught.value.status == 403
    assert caught.value.code is None
    assert "unknown" in str(caught.value)


def test_pagination_retries_only_the_page_that_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    client, opener, _ = build(
        monkeypatch,
        [
            ok({"results": [{"id": "a"}], "has_more": True, "next_cursor": "c1"}),
            TimeoutError("The read operation timed out"),
            ok({"results": [{"id": "b"}], "has_more": False}),
        ],
    )

    pages = list(client.iter_block_children("block-1"))

    assert [page["results"][0]["id"] for page in pages] == ["a", "b"]
    assert opener.attempts == 3, "the first page is not re-fetched"
    assert client.call_counts["/blocks/block-1/children"] == 2


def test_a_malformed_body_is_retried_and_never_escapes_as_a_value_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, opener, sleeps = build(
        monkeypatch, [FakeResponse(b"<html>proxy says no</html>"), ok({"object": "page"})]
    )

    assert client.call("GET", f"/pages/{PAGE_ID}") == {"object": "page"}
    assert opener.attempts == 2
    assert sleeps == [1.0]
    assert client.retry_counts == {"decode": 1}


def test_a_body_that_never_becomes_json_exhausts_as_a_notion_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _, _ = build(
        monkeypatch, [FakeResponse(b"not json") for _ in range(2)], max_attempts=2
    )

    with pytest.raises(NotionApiError) as caught:
        client.call("GET", f"/pages/{PAGE_ID}")

    assert caught.value.transient is True
    assert caught.value.retry_class == "decode"
    assert isinstance(caught.value.__cause__, json.JSONDecodeError)
    assert TOKEN not in str(caught.value)
