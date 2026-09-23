"""One rule for "which part of this id is the timestamp".

The bug this file exists for: the converters write
"{workspace}:{channel}:{ts}" and a reply names its parent by the bare ts, so
four separate queries joined two different spellings and matched nothing. Each
of them reported the mismatch as missing data -- 1,674 threads "without a
parent", a nightly sweep re-fetching what was already there -- which is the
expensive kind of wrong, because it looks like an answer.
"""

from __future__ import annotations

import pytest

from rlwrld_worklog.slack_ids import ts_expr, ts_of


@pytest.mark.parametrize(
    ("source_entity_id", "expected"),
    [
        ("T012AB:C345:1789638401.411289", "1789638401.411289"),
        # A bare ts, which the older fixtures and some tests still write.
        ("1789638401.411289", "1789638401.411289"),
        # Two segments, should a converter ever drop the workspace.
        ("C345:1789638401.411289", "1789638401.411289"),
    ],
)
def test_the_timestamp_is_what_follows_the_last_colon(source_entity_id, expected):
    assert ts_of(source_entity_id) == expected


def test_the_sql_and_the_python_spell_the_same_rule():
    """Both halves exist, and the SQL names the column it was given.

    The Python side is for ids already in hand; the SQL side is what the joins
    use. They are kept next to each other so a change to one is visibly a
    change to the other.
    """
    assert ts_expr("parent.source_entity_id") == (
        "regexp_replace(parent.source_entity_id, '^.*:', '')"
    )
