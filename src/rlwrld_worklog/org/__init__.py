"""The organisation: who the people are, and which accounts are theirs.

The roster is a dimension, not a source. It has no manifest, no coverage grid
and no checkpoint, because "the org chart was not collected on 2026-08-15" is
not a fact about the world -- it would be a red square on a screen that means
nothing. What it has instead is observations: every sync records what the
sheet said and when we read it, and nothing is ever overwritten.

Three decisions this package exists to keep, all HK's:

* **Hire and leave dates are never stored** (2026-09-04, sensitive). Validity
  comes from when we read the sheet, which is a fact about us rather than
  about the person.
* **Employee numbers are never stored.** People are identified by name and
  nickname; externals (Virtual Lab students) by the slurm/NCloud name that
  `roster_seed_ext` carries.
* **A person's row is never deleted.** Someone who disappears from the sheet
  gets a status, not a deletion: deleting them would make every past activity
  of theirs unattributable.

The three axes stay separate, and one of them is deliberately
double-counting. A 방문 연구원 is `affiliation=student` *and*
`access_level=staff_equivalent`, because they reach both the student space
and the internal one -- counting them in both is the definition, not a
deduplication bug to fix later (HK, 2026-09-04). So headcount has two honest
answers, and they never go in one column.
"""

from .normalize import (  # noqa: F401
    HEADER_ALIASES,
    IDENTITY_FIELDS,
    normalize_header,
    normalize_rows,
    team_path,
)
from .plan import duplicates, person_id, plan, team_id, team_rows  # noqa: F401
