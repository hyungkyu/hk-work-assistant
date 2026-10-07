"""One roster sync: a workbook in, an observation per tab written.

The same steps `worklog org sync` runs, held here so the backoffice's refresh
button and the batch cannot drift apart. HK asked for the button on
2026-10-07 (리프레시 버튼을 누르면 최신 조직도 정보를 가져오는거야) -- it is
still the code doing the sync, only started by a person instead of a timer,
which keeps the 2026-09-11 rule (이건 코드여야지, 네가 하면 안됨) intact.
"""

from __future__ import annotations

from typing import Any

from .sheet import ROSTER_SHEET_ID, TABS, raw_row_counts, read_all, summarize, workbook_digest
from .store import write_observation


def roster_sheet_url(sheet_id: str = ROSTER_SHEET_ID) -> str:
    """Where a person edits the roster -- the only place the chart comes from."""
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit"


class UnusableRoster(ValueError):
    """The workbook is missing a tab, or a tab came back with nobody in it."""


def sync_workbook(
    database_url: str,
    data: bytes,
    *,
    apply: bool = True,
    reobserve: bool = False,
    refuse_empty: bool = False,
) -> dict[str, Any]:
    """Write one observation per tab from an exported workbook.

    `refuse_empty` is for callers that cannot be watched: an empty tab would
    be recorded as everybody in it leaving, and a sheet that was cleared by
    mistake is far likelier than a lab that emptied overnight. It is checked
    before anything is written, so a refusal changes nothing.
    """
    digest = workbook_digest(data)
    try:
        by_tab = read_all(data, TABS)
    except KeyError as error:
        # A renamed or deleted tab; the message names every tab there is.
        raise UnusableRoster(error.args[0] if error.args else str(error)) from error
    if refuse_empty:
        empty = [tab for tab in TABS if not by_tab.get(tab)]
        if empty:
            raise UnusableRoster(f"the roster tab {', '.join(empty)} has no one in it; nothing was changed")
    raw_counts = raw_row_counts(data, TABS)
    results = [
        write_observation(
            database_url,
            by_tab[tab],
            source=tab,
            digest=digest,
            dry_run=not apply,
            skip_unchanged=not reobserve,
            rows_in_sheet=raw_counts.get(tab),
        ).as_dict()
        for tab in TABS
    ]
    return {"workbook_sha256": digest, "summary": summarize(by_tab), "tabs": results}
