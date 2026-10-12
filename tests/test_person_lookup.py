"""A name is not a key, and the batch stopped the night that mattered.

HK, 2026-10-12: 수집은 정상적으로 매일 되고 있는거 맞자?

Collection was. The block build and the embedding were not: two roster rows
are both called 류형규 -- a duplicate waiting on a merge nobody has approved
-- so the nightly lookup was ambiguous and both commands exited non-zero.
Nothing looked broken from outside. The precedent search kept answering from
the index it already had, which is the same quiet failure as the nine days in
September when the embedding was a command nobody ran.

So: every command that takes a person also takes `--person-id`, and an
ambiguous name says which ids it found instead of only that it failed.
"""

from __future__ import annotations

import os
from argparse import Namespace
from pathlib import Path

import pytest

from rlwrld_worklog import cli, digest

REQUIRES_DATABASE = pytest.mark.skipif(
    not os.environ.get("WORKLOG_TEST_DATABASE_URL"),
    reason="set WORKLOG_TEST_DATABASE_URL to a throwaway database",
)


def test_every_person_command_accepts_an_id() -> None:
    """Including the two the nightly batch runs.

    Asserted on the parser rather than by reading the help text, because the
    failure this prevents is a flag that was never added to one of them --
    and that one would be the command that breaks at 03:00.
    """
    import argparse

    parser = cli.build_parser()
    commands = next(
        action
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    ).choices
    for command in ("blocks", "embed", "pairs", "precedents", "reconcile"):
        flags = {
            option
            for entry in commands[command]._actions
            for option in entry.option_strings
        }
        assert "--person-id" in flags, f"{command} cannot be told which person"
        assert "--person-name" in flags


def test_a_name_is_no_longer_required_by_the_parser() -> None:
    """`--person-name` was required, so `--person-id` alone was rejected."""
    parser = cli.build_parser()
    parsed = parser.parse_args(["blocks", "--person-id", "p_abc", "--apply"])
    assert parsed.person_id == "p_abc"
    assert parsed.person_name is None


def test_an_ambiguous_name_names_the_candidates(monkeypatch) -> None:
    """"찾지 못함" was the wrong word for "found two".

    The night this happened, the log said the name was not found. It was
    found twice. Those lead to opposite actions -- one is a roster that lost
    somebody, the other is a duplicate waiting on a merge -- and the message
    sent me looking for the first.
    """
    monkeypatch.setattr(
        digest,
        "resolve_people",
        lambda url, names: {
            "resolved": {},
            "unresolved": [],
            "ambiguous": {names[0]: ["류형규 (p_aaa)", "류형규 (p_bbb)"]},
        },
    )
    with pytest.raises(SystemExit) as error:
        cli._resolve_one_person(
            "postgresql://example/none", Namespace(person_name="류형규", person_id=None)
        )

    message = str(error.value)
    assert "p_aaa" in message and "p_bbb" in message
    assert "--person-id" in message, "say how to pick one"


def test_a_resolved_name_still_works(monkeypatch) -> None:
    monkeypatch.setattr(
        digest,
        "resolve_people",
        lambda url, names: {"resolved": {names[0]: "p_one"}, "unresolved": [], "ambiguous": {}},
    )
    found = cli._resolve_one_person(
        "postgresql://example/none", Namespace(person_name="아무개", person_id=None)
    )
    assert found == "p_one"


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "digest-tick.sh"


def test_the_nightly_batch_prefers_the_id() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert "WORKLOG_BLOCKS_PERSON_ID" in source
    assert '"${person_flag[@]}"' in source
    assert "--person-name" in source, "the name still works when no id is set"


def test_the_batch_script_is_valid_shell() -> None:
    import subprocess

    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0
