"""The review screen's routes: authenticated, read-only except the decision.

HK, 2026-09-21: 네가 페어링을 한것을 가정하되, 나는 수정할 수 있게 하는거지.

The one mutation here is that correction. Everything else reads what
`worklog pairs --apply` already wrote, and these tests hold that line: a route
that could rebuild the candidates would make "what did he actually see when he
chose this" unanswerable, which is the thing the correction data is for.

Called the same way the other API suites call routes -- directly, with a
minimal request stub -- which still exercises authentication, CSRF and
argument validation.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import HTTPException

from rlwrld_worklog import admin_web, voice_web
from rlwrld_worklog.admin_web import SESSION_COOKIE


class FakeRequest:
    def __init__(self, *, cookies: dict[str, str] | None = None, headers=None) -> None:
        self.cookies = cookies or {}
        self.headers: dict[str, str] = headers or {}


@pytest.fixture()
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_CONFIG_ROOT", str(tmp_path / "config"))
    yield tmp_path


@pytest.fixture()
def owner(config):
    token, csrf = admin_web.store().create_session(
        subject="owner",
        email="hyungkyu.ryu@rlwrld.ai",
        role="super_admin",
        auth_method="google",
    )
    return FakeRequest(cookies={SESSION_COOKIE: token}), csrf


READ_ROUTES = (
    (voice_web.pairs_route, {"person_name": "류형규"}),
    (voice_web.agreement_route, {"person_name": "류형규"}),
    (voice_web.audit_route, {"person_name": "류형규"}),
)


@pytest.mark.parametrize("route,kwargs", READ_ROUTES)
def test_anonymous_callers_are_refused(config, route: Any, kwargs) -> None:
    with pytest.raises(HTTPException) as error:
        route(FakeRequest(), **kwargs)
    assert error.value.status_code == 401


def test_choosing_is_refused_without_a_session(config) -> None:
    with pytest.raises(HTTPException) as error:
        voice_web.choose_route(
            FakeRequest(),
            voice_web.ChooseRequest(answer_ledger_id="a", pair_id=None),
        )
    assert error.value.status_code == 401


def test_choosing_is_refused_without_csrf(config, owner) -> None:
    """A decision is a write, and writes carry CSRF like every other one here."""
    request, _ = owner
    with pytest.raises(HTTPException) as error:
        voice_web.choose_route(
            request, voice_web.ChooseRequest(answer_ledger_id="a", pair_id=None)
        )
    assert error.value.status_code == 403
    assert error.value.detail == "invalid CSRF token"


@pytest.mark.parametrize("route,kwargs", READ_ROUTES)
def test_a_missing_database_says_so(config, owner, monkeypatch, route, kwargs) -> None:
    """Not an empty queue.

    "The database is not configured" and "there is nothing to review" are
    different answers, and a screen that showed them the same way would have
    him waiting on a batch that was never going to fill anything.
    """
    request, _ = owner
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(HTTPException) as error:
        route(request, **kwargs)
    assert error.value.status_code == 503


def test_an_unknown_person_is_not_an_empty_queue(config, owner, monkeypatch) -> None:
    """A misspelt name must not read as "nothing to review"."""
    request, _ = owner
    monkeypatch.setenv("DATABASE_URL", "postgresql://example/none")
    monkeypatch.setattr(
        voice_web, "_person_id", _raise_not_found, raising=False
    )
    with pytest.raises(HTTPException) as error:
        voice_web.pairs_route(request, person_name="없는사람")
    assert error.value.status_code == 404


def _raise_not_found(name: str) -> str:
    raise HTTPException(status_code=404, detail=f"찾지 못한 사람: {name}")


def test_the_decision_reaches_the_store_with_the_person_who_made_it(
    config, owner, monkeypatch
) -> None:
    """Who decided is part of the record, not an implementation detail.

    A correction is evidence about the ranking, and evidence with no author is
    worth less than evidence with one.
    """
    request, csrf = owner
    request.headers["x-csrf-token"] = csrf
    monkeypatch.setenv("DATABASE_URL", "postgresql://example/none")
    monkeypatch.setattr(voice_web, "_person_id", lambda name: "p1", raising=False)

    seen: dict[str, Any] = {}

    def fake_choose(url, answer_ledger_id, pair_id, *, actor):
        seen.update(
            {"url": url, "answer": answer_ledger_id, "pair": pair_id, "actor": actor}
        )
        return True

    import rlwrld_worklog.blocks as blocks_module

    monkeypatch.setattr(blocks_module, "choose_pair", fake_choose)

    result = voice_web.choose_route(
        request, voice_web.ChooseRequest(answer_ledger_id="a1", pair_id="c1")
    )

    assert result["ok"] is True
    assert seen["answer"] == "a1"
    assert seen["pair"] == "c1"
    assert seen["actor"] == "hyungkyu.ryu@rlwrld.ai"


def test_none_of_these_is_a_decision_not_a_missing_field(
    config, owner, monkeypatch
) -> None:
    """`pair_id: null` has to survive the request model.

    "None of these was the question" is the most informative correction he can
    make -- it says the whole candidate set was wrong. If the model rejected a
    null the screen would have no way to say it, and the queue would only ever
    collect agreement.
    """
    request, csrf = owner
    request.headers["x-csrf-token"] = csrf
    monkeypatch.setenv("DATABASE_URL", "postgresql://example/none")

    seen: dict[str, Any] = {}

    def fake_choose(url, answer_ledger_id, pair_id, *, actor):
        seen["pair"] = pair_id
        return True

    import rlwrld_worklog.blocks as blocks_module

    monkeypatch.setattr(blocks_module, "choose_pair", fake_choose)

    voice_web.choose_route(
        request, voice_web.ChooseRequest(answer_ledger_id="a1", pair_id=None)
    )
    assert seen["pair"] is None


def test_an_answer_that_is_not_there_is_a_404(config, owner, monkeypatch) -> None:
    request, csrf = owner
    request.headers["x-csrf-token"] = csrf
    monkeypatch.setenv("DATABASE_URL", "postgresql://example/none")

    import rlwrld_worklog.blocks as blocks_module

    monkeypatch.setattr(
        blocks_module,
        "choose_pair",
        lambda *args, **kwargs: False,
    )
    with pytest.raises(HTTPException) as error:
        voice_web.choose_route(
            request, voice_web.ChooseRequest(answer_ledger_id="gone", pair_id=None)
        )
    assert error.value.status_code == 404


def test_no_route_here_rebuilds_the_candidates() -> None:
    """The read-only contract, stated as a test rather than as a comment.

    HK, 2026-09-11: 이건 코드여야지, 네가 하면 안됨. The candidates come from a
    batch. A button that rebuilt them would change what the queue shows
    between the moment he read it and the moment he clicked, and the record of
    what he chose would no longer say what he chose it over.
    """
    source = __import__("pathlib").Path(voice_web.__file__).read_text(encoding="utf-8")
    for forbidden in ("propose_pairs", "build_blocks", "embed_corpus"):
        assert forbidden not in source, (
            f"{forbidden} is reachable from a route; the screen would then be "
            "rebuilding what it is asking him to judge"
        )
