"""Find the Notion page that is a meeting's own note.

HK, 2026-09-16: "회의는 제미나이 노트도 있지만, 해당 일자의 미팅 노트가 남는
경우가 많아. 패턴을 찾아서 매핑해볼래?"

What the real `M Meetings` database showed when I looked:

* The `Meeting date` property is reliable. The date written into the title is
  not -- one page titled `[09/09(수)] ...` carries `Meeting date` 2026-09-10.
  So the date property picks the candidates and the title never does.
* Titles come in at least five shapes: `[09/09(수)] 이름`, `09/09 이름`,
  `2026-09-09 이름`, `이름 미팅`, and the bare name. The date decoration is
  stripped before anything is compared.
* Duplicate titles exist -- `DEEP ROBOTICS` appears four times. Two candidates
  that both look right mean the answer is not known, and a note attached to the
  wrong meeting is worse than a meeting with no note. In that case: nothing.

Nothing here reads Notion or Google. It takes page payloads the ledger already
holds and answers one question: which of these, if any, is this meeting's note.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Iterable

# `[09/09(수)]`, `09/09`, `2026-09-09`, `9.9` and friends, only at the front.
_DATE_PREFIX = re.compile(
    r"""^\s*
    [\[\(]?\s*
    (?:\d{4}[-./])?\d{1,2}[-./]\d{1,2}
    \s*(?:\([^)]{1,3}\))?
    \s*[\]\)]?
    \s*[-–—:·]?\s*
    """,
    re.VERBOSE,
)

_MEETING_WORDS = ("미팅", "회의", "meeting", "sync", "정기", "주간", "weekly")

# Property names that hold the day the meeting happened, across the databases
# that keep meeting notes.
_DATE_PROPERTY_NAMES = ("meeting date", "회의 날짜", "회의일", "date", "날짜", "when")


def _property_map(raw: Any) -> dict[str, Any]:
    properties = (raw or {}).get("properties") if isinstance(raw, dict) else None
    return properties if isinstance(properties, dict) else {}


def meeting_date(raw: Any) -> str | None:
    """The day the page says the meeting happened, as `YYYY-MM-DD`.

    Read from the date property, never from the title -- the two disagree in
    real data and the property is the one people actually set.
    """
    properties = _property_map(raw)
    named = {str(name).strip().lower(): value for name, value in properties.items()}
    for wanted in _DATE_PROPERTY_NAMES:
        value = named.get(wanted)
        if not isinstance(value, dict):
            continue
        date = value.get("date")
        start = date.get("start") if isinstance(date, dict) else None
        if isinstance(start, str) and start:
            return start[:10]
    return None


def page_title(raw: Any) -> str | None:
    """The page's own title, from whichever property carries it."""
    for value in _property_map(raw).values():
        if not isinstance(value, dict) or value.get("type") != "title":
            continue
        parts = [
            str(item.get("plain_text") or "")
            for item in value.get("title") or []
            if isinstance(item, dict)
        ]
        found = " ".join("".join(parts).split())
        if found:
            return found
    return None


def strip_date_prefix(title: str | None) -> str:
    """`[09/09(수)] 딥로보틱스` -> `딥로보틱스`."""
    if not isinstance(title, str):
        return ""
    return _DATE_PREFIX.sub("", title).strip()


def _normal(text: str) -> str:
    text = unicodedata.normalize("NFKC", strip_date_prefix(text)).lower()
    for word in _MEETING_WORDS:
        text = text.replace(word, " ")
    return re.sub(r"[^0-9a-z가-힣]+", "", text)


def _tokens(text: str) -> set[str]:
    text = unicodedata.normalize("NFKC", strip_date_prefix(text)).lower()
    parts = re.split(r"[^0-9a-z가-힣]+", text)
    return {part for part in parts if part and part not in _MEETING_WORDS}


def similarity(meeting: str, title: str) -> float:
    """How much a calendar title and a page title are the same meeting.

    1.0 for the same string once decoration is gone, a containment score when
    one name sits inside the other, and token overlap otherwise. Deliberately
    blunt: this number only has to separate "clearly the same" from
    "not clearly", because anything in between is answered with silence.
    """
    left, right = _normal(meeting), _normal(title)
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    if left in right or right in left:
        return 0.9
    a, b = _tokens(meeting), _tokens(title)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# Below this a page is not the meeting's note. Above it, and clear of the next
# best candidate, it is.
MATCH_FLOOR = 0.6
# How far ahead the winner has to be. Two pages within this of each other are
# two answers, which is no answer.
MATCH_MARGIN = 0.15


def candidates(pages: Iterable[tuple[str, dict[str, Any]]], day: str) -> list[tuple[str, dict]]:
    """The pages whose `Meeting date` is this day."""
    return [(pid, raw) for pid, raw in pages if meeting_date(raw) == day]


def match_note(summary: str | None, day: str | None, pages) -> str | None:
    """The page id of this meeting's note, or None when it is not certain.

    Returning None is a real answer here. A day's meetings that all match
    nothing is a report that lost nothing; one meeting carrying another
    meeting's decisions is a report that cannot be trusted at all.
    """
    if not summary or not day:
        return None
    pool = candidates(pages.items() if isinstance(pages, dict) else pages, day)
    if not pool:
        return None
    scored = sorted(
        ((similarity(summary, page_title(raw) or ""), pid) for pid, raw in pool),
        reverse=True,
    )
    best, page_id = scored[0]
    if best < MATCH_FLOOR:
        return None
    if len(scored) > 1 and best - scored[1][0] < MATCH_MARGIN:
        return None
    return page_id
