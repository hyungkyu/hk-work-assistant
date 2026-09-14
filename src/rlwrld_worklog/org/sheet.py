"""The roster sheet, as rows per tab.

Read through Drive's XLSX export. Three things ruled out the alternatives:

* A CSV export returns only the **first tab**, and the roster needs two.
* The connector's document read **truncates** a large sheet -- which is why
  the legacy `roster_dump_seed2.py` existed at all, as a workaround.
* The XLSX export returns **every tab in one request**, so `roster_seed_2`
  (internal staff plus Virtual Lab professors) and `roster_seed_ext` (Virtual
  Lab students) arrive together.

It needs only `drive.readonly`, which this installation's token already
carries. No new scope and no re-authorisation: that was checked against the
stored token's scopes rather than assumed, after being wrongly asserted once.
"""

from __future__ import annotations

import hashlib
import io
from typing import Any

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# The roster HK pointed at. Overridable, because a sheet id is a deployment
# fact rather than a law, but defaulted so the batch needs no configuration to
# do the right thing.
ROSTER_SHEET_ID = "15QsaBGnDu3ACJ8ucqeO0fu5FhDiXGjhYmzKr0KANQPw"

# The names this code stores, and the tab names the sheet actually uses.
#
# The external tab is spelled `roaster_seed_ext` in the sheet -- "roaster",
# not "roster". That is the sheet's name for it, and the sheet is not ours to
# correct: renaming a tab breaks every formula and every other reader pointed
# at it. HK wrote it that way from the start; I read past it and used the
# spelling I expected, which is how the first sync died looking for a tab that
# was never there.
#
# So the stored `source` stays canonical (the CHECK constraint in 0007 names
# it) and the lookup accepts either spelling. A new alias goes in this list;
# nothing else changes.
INTERNAL_TAB = "roster_seed_2"
EXTERNAL_TAB = "roster_seed_ext"
TABS = (INTERNAL_TAB, EXTERNAL_TAB)

TAB_ALIASES: dict[str, tuple[str, ...]] = {
    INTERNAL_TAB: ("roster_seed_2", "roaster_seed_2"),
    EXTERNAL_TAB: ("roster_seed_ext", "roaster_seed_ext"),
}


def resolve_tabs(data: bytes, tabs: tuple[str, ...] = TABS) -> dict[str, str]:
    """Which sheet tab holds each roster, by the names the workbook has.

    Raises naming every tab in the workbook when one cannot be found, because
    a missing tab means somebody renamed or deleted it -- a real event that a
    batch must report rather than answering with zero people.
    """
    present = set(tab_names(data))
    found: dict[str, str] = {}
    for tab in tabs:
        for candidate in TAB_ALIASES.get(tab, (tab,)):
            if candidate in present:
                found[tab] = candidate
                break
        else:
            raise KeyError(
                f"no tab for {tab}: tried {', '.join(TAB_ALIASES.get(tab, (tab,)))}; "
                f"the workbook has {sorted(present)}"
            )
    return found


def export_workbook(credentials, sheet_id: str = ROSTER_SHEET_ID) -> bytes:
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaIoBaseDownload

    service = build("drive", "v3", credentials=credentials, cache_discovery=False)
    request = service.files().export_media(fileId=sheet_id, mimeType=XLSX_MIME)
    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, request, chunksize=4 * 1024 * 1024)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buffer.getvalue()


def workbook_digest(data: bytes) -> str:
    """The workbook's sha256, so an unchanged re-read is recognisable as one."""
    return hashlib.sha256(data).hexdigest()


def _openpyxl():
    """The XLSX reader, or an error that says how to get it.

    A missing dependency inside a scheduled batch is otherwise a traceback in
    a log nobody opens, and the batch reports failure without saying that one
    `pip install` fixes it. This happened on 2026-09-14: openpyxl was declared
    in pyproject.toml and never installed into the host virtualenv the batch
    actually runs from, and the run died on `import openpyxl`.
    """
    try:
        import openpyxl
    except ModuleNotFoundError as error:  # pragma: no cover - exercised by the test below
        raise RuntimeError(
            "openpyxl is required to read the roster workbook and is not installed "
            "in this environment. Install it into the environment that runs the "
            "batch: `.venv/bin/pip install openpyxl`"
        ) from error
    return openpyxl


def _looks_like_a_header(values: list[str]) -> bool:
    """Does this row name columns, rather than hold somebody's data?

    A row is the header when at least two of its cells are names this code
    knows. Two, not one, because a data row can happen to contain a word that
    is also a column name.

    Taking the *first non-empty row* instead is what made the student tab read
    as empty on 2026-09-14: that tab opens with a title line above the column
    names, so the title became the header, every subsequent row mapped onto
    one meaningless key, no row had a name, and normalising dropped all of
    them. Zero students, no error -- the worst shape a failure can take.
    """
    from .normalize import normalize_header

    return sum(1 for value in values if value and normalize_header(value)) >= 2


def rows_from_workbook(data: bytes, tab: str) -> list[dict]:
    """Rows of one tab, from the row that actually names the columns."""
    openpyxl = _openpyxl()

    book = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    if tab not in book.sheetnames:
        # Named, with what is actually there. A renamed tab is a real event --
        # somebody edited the sheet -- and the batch must say which tab it
        # could not find rather than reporting zero people.
        raise KeyError(f"no such tab: {tab} (found: {book.sheetnames})")
    sheet = book[tab]
    rows = [
        ["" if cell is None else str(cell).strip() for cell in row]
        for row in sheet.iter_rows(values_only=True)
    ]

    header_at = None
    for index, values in enumerate(rows):
        if _looks_like_a_header(values):
            header_at = index
            break
    if header_at is None:
        # Said out loud. A tab whose columns cannot be recognised is a tab
        # somebody restructured, and answering with an empty list would file
        # its people as having ceased to exist.
        raise KeyError(
            f"no header row in {tab}: none of its {len(rows)} rows names two "
            "columns this code knows. The sheet's columns may have been renamed"
        )

    header = rows[header_at]
    out: list[dict] = []
    for values in rows[header_at + 1 :]:
        if not any(values):
            continue
        out.append({header[index]: values[index] for index in range(min(len(header), len(values)))})
    return out


def tab_names(data: bytes) -> list[str]:
    openpyxl = _openpyxl()

    return openpyxl.load_workbook(io.BytesIO(data), read_only=True).sheetnames


def read_all(data: bytes, tabs: tuple[str, ...] = TABS) -> dict[str, list[dict]]:
    """Every roster tab's rows, normalised, keyed by the name we store.

    The workbook's own tab names are resolved first, so a tab spelled
    differently in the sheet still lands under the source name the database
    knows. Import is local so this module can be read and tested without the
    normaliser being involved in the file format.
    """
    from .normalize import normalize_rows

    resolved = resolve_tabs(data, tabs)
    found: dict[str, list[dict]] = {}
    for tab in tabs:
        found[tab] = normalize_rows(rows_from_workbook(data, resolved[tab]), source=tab)
    return found


def raw_row_counts(data: bytes, tabs: tuple[str, ...] = TABS) -> dict[str, int]:
    """How many rows each tab held, before normalising dropped any.

    Reported next to the people count so "the tab was empty" and "every row
    was dropped" cannot look the same from the outside.
    """
    resolved = resolve_tabs(data, tabs)
    return {tab: len(rows_from_workbook(data, resolved[tab])) for tab in tabs}


def summarize(records_by_tab: dict[str, list[dict]]) -> dict[str, Any]:
    """Headcount, both ways, because both are correct.

    `people` counts persons. `by_access` counts what each person can reach,
    and a 방문 연구원 appears under `student` affiliation and
    `staff_equivalent` access at once -- that is the definition HK gave, not a
    double count to be collapsed.
    """
    affiliation: dict[str, int] = {}
    access: dict[str, int] = {}
    status: dict[str, int] = {}
    total = 0
    for records in records_by_tab.values():
        for record in records:
            total += 1
            for bucket, key in (
                (affiliation, "affiliation"),
                (access, "access_level"),
                (status, "status"),
            ):
                value = str(record.get(key) or "unknown")
                bucket[value] = bucket.get(value, 0) + 1
    return {
        "rows": total,
        "by_affiliation": dict(sorted(affiliation.items())),
        "by_access": dict(sorted(access.items())),
        "by_status": dict(sorted(status.items())),
    }
