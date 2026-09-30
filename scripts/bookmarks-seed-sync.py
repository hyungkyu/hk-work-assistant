#!/usr/bin/env python3
"""Put the seeded bookmarks into an install that already has a bookmarks.json.

`AdminStore.SEEDED_BOOKMARKS` is only the *initial content* of the file, which
is the right rule -- a deleted bookmark has to stay deleted -- but it means an
install that wrote the file once never sees an address added to the seeds
later. This script is the other half: it adds the seeds that are missing, and
leaves everything else exactly as it is.

    scripts/bookmarks-seed-sync.py              # add what is missing
    scripts/bookmarks-seed-sync.py --refresh    # also re-point moved seeds
    scripts/bookmarks-seed-sync.py --dry-run    # say what it would do

Missing is decided by URL, so running it twice adds nothing the second time.

`--refresh` is the narrow exception: for a seed whose id is already stored, it
writes the seed's label, group, url and note over the stored ones. That is how
a seed that moved house (Lightdash left its ts.net address for
lightdash.rlwrld.co) gets corrected. It also overwrites an operator's own edit
to that row, which is why it is off by default.

Nothing here deletes. A row the seeds no longer mention is left alone --
somebody may be using it, and this script is not the place to decide that.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rlwrld_worklog.admin_store import AdminStore  # noqa: E402

REFRESHABLE = ("group", "label", "url", "note")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config-root",
        type=Path,
        default=None,
        help="APP_CONFIG_ROOT override; the store never reads or writes anywhere else",
    )
    parser.add_argument(
        "--actor", default="seed-sync", help="Who the audit trail records for these writes"
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Also rewrite a stored seed's fields from the seed, for a seed that moved",
    )
    parser.add_argument("--dry-run", action="store_true", help="Report without writing")
    arguments = parser.parse_args(argv)

    store = (
        AdminStore(arguments.config_root)
        if arguments.config_root
        else AdminStore.from_environment()
    )
    stored = store.load_bookmarks()
    by_url = {entry["url"]: entry for entry in stored}
    by_id = {entry["id"]: entry for entry in stored}

    added: list[str] = []
    refreshed: list[str] = []

    for seed in AdminStore.SEEDED_BOOKMARKS:
        if seed["url"] in by_url:
            continue
        existing = by_id.get(seed["id"])
        if existing is not None:
            # Same entry, different address: only --refresh may move it, and
            # adding it again would leave two rows for one system.
            if not arguments.refresh:
                print(f"moved, left alone: {seed['label']} -> {seed['url']}")
                continue
            changes = {key: seed[key] for key in REFRESHABLE if existing[key] != seed[key]}
            if not changes:
                continue
            if not arguments.dry_run:
                store.update_bookmark(seed["id"], changes, actor=arguments.actor)
            refreshed.append(f"{seed['label']} ({', '.join(sorted(changes))})")
            continue
        if not arguments.dry_run:
            store.add_bookmark(
                seed["url"],
                label=seed["label"],
                group=seed["group"],
                note=seed["note"],
                actor=arguments.actor,
            )
        added.append(seed["label"])

    prefix = "would add" if arguments.dry_run else "added"
    print(f"{prefix} {len(added)}: {', '.join(added) if added else '-'}")
    if arguments.refresh:
        prefix = "would refresh" if arguments.dry_run else "refreshed"
        print(f"{prefix} {len(refreshed)}: {', '.join(refreshed) if refreshed else '-'}")
    print(f"stored now: {len(store.load_bookmarks())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
