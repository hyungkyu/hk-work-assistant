"""What a GPU-hour and a GiB-month cost at each cloud, and what that is in won.

The screen this feeds has to answer one question honestly: *is this number
still true, and what was it converted at?* Everything here follows from that.

**A price and its conversion are not the same fact.** Nebius publishes
$3.85/GPU-hour. It does not publish ₩5,400/GPU-hour -- we do, using a rate we
fetched separately, and the converted number is worth exactly as much as the
rate beside it. So :func:`build_payload` carries the original amount and the
converted one side by side, with the rate and both timestamps, and never
stores the converted number anywhere.

**A refresh that changes nothing must not look like a refresh that changed
everything.** :func:`content_hash` reduces a whole rate card to one value. When
it matches, :func:`apply_refresh` moves the date and the rate on the snapshot
already there and stops. A new snapshot is written only when a number actually
moved, which is what keeps the history a history of price changes rather than
a log of button presses.

**A provider that did not answer is not a provider with no GPUs.** Four of the
five rate cards are read out of a web page. When one of those pages is
redesigned the fetch returns nothing, and nothing is indistinguishable from
"they stopped selling GPUs" unless something records the difference. That is
what :class:`SourceResult` and the `cloud_price_run` table are for, and it is
why a failed provider's rows are *carried forward* from the previous snapshot
rather than treated as deletions.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterable, Mapping, Sequence

# The order the screen lists them in, and the only provider ids the rest of
# this module will accept. A typo in a fetcher becomes a KeyError here rather
# than a sixth provider nobody notices is empty.
PROVIDERS: dict[str, str] = {
    "aws": "AWS",
    "nebius": "Nebius",
    "kakao": "Kakao Cloud",
    "naver": "Naver Cloud",
    "vessl": "VESSL.ai",
}

CATEGORIES: dict[str, str] = {"gpu": "GPU", "storage": "스토리지"}

# Money is compared and hashed at the scale the column stores. Two amounts
# that differ below this are the same price, and must not read as a change.
AMOUNT_SCALE = 10


def amount_key(amount: Decimal | float | int | str) -> str:
    """The canonical spelling of an amount, for hashing and for comparison.

    Fixed point rather than :meth:`Decimal.normalize`, which renders 1000 as
    ``1E+3`` and would make the hash depend on how a fetcher happened to spell
    a round number.
    """
    return f"{Decimal(str(amount)):.{AMOUNT_SCALE}f}"


@dataclass(frozen=True)
class PriceRow:
    """One line of one provider's rate card, exactly as the provider states it.

    `amount` is the 원가 and is never converted here. `sku` is the provider's
    own identifier and is what identity across refreshes hangs on -- a label
    can be reworded without the price changing, and a reworded label must not
    read as a price change.
    """

    provider: str
    category: str
    sku: str
    label: str
    amount: Decimal
    currency: str
    unit: str
    region: str | None = None
    spec: Mapping[str, Any] = field(default_factory=dict)
    sort: int = 0

    def __post_init__(self) -> None:
        if self.provider not in PROVIDERS:
            raise ValueError(f"unknown provider: {self.provider!r}")
        if self.category not in CATEGORIES:
            raise ValueError(f"unknown category: {self.category!r}")
        if not self.sku:
            raise ValueError("a price row needs a sku")
        object.__setattr__(self, "amount", Decimal(str(self.amount)))

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.provider, self.category, self.sku)

    def identity(self) -> str:
        """Everything a change would have to move. The unit of the hash."""
        return "|".join(
            [
                self.provider,
                self.category,
                self.sku,
                amount_key(self.amount),
                self.currency,
                self.unit,
            ]
        )


@dataclass
class SourceResult:
    """What one provider's fetcher came back with, including having failed."""

    provider: str
    rows: list[PriceRow] = field(default_factory=list)
    outcome: str = "ok"
    detail: str | None = None
    source_url: str | None = None
    source_version: str | None = None
    # "I answered, and the answer is that nothing changed -- keep what you
    # have." Distinct from a failure, which also carries rows forward but is
    # an apology rather than a confirmation, and distinct from an empty
    # successful fetch, which would mean the provider withdrew everything.
    reused: bool = False

    @property
    def ok(self) -> bool:
        return self.outcome == "ok"

    @property
    def supplies_rows(self) -> bool:
        """Whether this result's rows should replace what we already hold."""
        return self.ok and not self.reused

    @classmethod
    def failure(cls, provider: str, detail: str, *, source_url: str | None = None) -> "SourceResult":
        return cls(provider=provider, outcome="failed", detail=detail, source_url=source_url)


@dataclass(frozen=True)
class FxRate:
    """One quote, with the time the quote itself is for.

    `as_of` is the rate provider's timestamp and `fetched_at` is when we asked.
    They are usually hours apart, and a screen that shows only the second one
    is overstating how fresh the conversion is.
    """

    base: str
    quote: str
    rate: Decimal
    as_of: datetime | None
    source: str
    fetched_at: datetime

    def convert(self, amount: Decimal, currency: str) -> Decimal | None:
        """`amount` in won, or None when we have no rate that reaches won.

        A price already in the quote currency converts at 1: it is not
        "unconvertible", it is already there.
        """
        if currency == self.quote:
            return Decimal(str(amount))
        if currency != self.base:
            return None
        return Decimal(str(amount)) * self.rate


def content_hash(rows: Iterable[PriceRow]) -> str:
    """One value standing for a whole rate card.

    Sorted before hashing so that two fetchers returning the same prices in a
    different order agree -- otherwise a provider that reorders its table
    would read as a price change every single refresh.
    """
    joined = "\n".join(sorted(row.identity() for row in rows))
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()


@dataclass
class PriceChange:
    provider: str
    category: str
    sku: str
    label: str
    type: str
    before_amount: Decimal | None
    after_amount: Decimal | None
    currency: str | None
    unit: str | None


@dataclass
class PriceDiff:
    added: list[PriceRow] = field(default_factory=list)
    changed: list[tuple[PriceRow, Decimal]] = field(default_factory=list)
    removed: list[Mapping[str, Any]] = field(default_factory=list)

    @property
    def moved(self) -> int:
        return len(self.added) + len(self.changed) + len(self.removed)

    def as_changes(self) -> list[PriceChange]:
        out: list[PriceChange] = []
        for row in self.added:
            out.append(
                PriceChange(row.provider, row.category, row.sku, row.label, "added",
                            None, row.amount, row.currency, row.unit)
            )
        for row, before in self.changed:
            out.append(
                PriceChange(row.provider, row.category, row.sku, row.label, "changed",
                            before, row.amount, row.currency, row.unit)
            )
        for old in self.removed:
            out.append(
                PriceChange(
                    str(old["provider"]), str(old["category"]), str(old["sku"]),
                    str(old["label"]), "removed",
                    Decimal(str(old["amount"])), None,
                    str(old["currency"]), str(old["unit"]),
                )
            )
        return out


def diff_rows(incoming: Sequence[PriceRow], existing: Sequence[Mapping[str, Any]]) -> PriceDiff:
    """What moved between the rate card we have and the one we just fetched.

    Matched on (provider, category, sku). A row whose label was reworded but
    whose price held is not a change: the screen is about money.
    """
    before = {
        (str(row["provider"]), str(row["category"]), str(row["sku"])): row
        for row in existing
    }
    diff = PriceDiff()
    seen: set[tuple[str, str, str]] = set()
    for row in incoming:
        seen.add(row.key)
        old = before.get(row.key)
        if old is None:
            diff.added.append(row)
            continue
        same_amount = amount_key(old["amount"]) == amount_key(row.amount)
        if same_amount and str(old["currency"]) == row.currency and str(old["unit"]) == row.unit:
            continue
        diff.changed.append((row, Decimal(str(old["amount"]))))
    for key, old in before.items():
        if key not in seen:
            diff.removed.append(old)
    return diff


def carry_forward(
    results: Sequence[SourceResult], existing: Sequence[Mapping[str, Any]]
) -> list[PriceRow]:
    """Every price this refresh should stand behind.

    The fetched rows from providers that supplied a new rate card, plus the
    rows we already had from every other provider -- the ones that failed, and
    the ones that answered "still the same edition" without re-sending it.

    A provider whose page we could not read this minute has not changed its
    prices; dropping its rows would record a fabricated deletion and then, on
    the next successful refresh, a fabricated re-addition.
    """
    answered = {result.provider for result in results if result.supplies_rows}
    rows: list[PriceRow] = [
        row for result in results if result.supplies_rows for row in result.rows
    ]
    for old in existing:
        if str(old["provider"]) in answered:
            continue
        rows.append(
            PriceRow(
                provider=str(old["provider"]),
                category=str(old["category"]),
                sku=str(old["sku"]),
                label=str(old["label"]),
                amount=Decimal(str(old["amount"])),
                currency=str(old["currency"]),
                unit=str(old["unit"]),
                region=old.get("region"),
                spec=old.get("spec") or {},
                sort=int(old.get("sort") or 0),
            )
        )
    return rows


# --------------------------------------------------------------- the payload


def _row_payload(row: Mapping[str, Any], fx: FxRate | None) -> dict[str, Any]:
    amount = Decimal(str(row["amount"]))
    converted = fx.convert(amount, str(row["currency"])) if fx else None
    return {
        "provider": row["provider"],
        "category": row["category"],
        "sku": row["sku"],
        "label": row["label"],
        "region": row.get("region"),
        "spec": row.get("spec") or {},
        "unit": row["unit"],
        # The 원가, as a string: a price is a decimal, and JSON numbers are
        # binary floats. $0.0147/GiB-month does not survive a round trip
        # through one, and a rate card that quietly rounds is worse than none.
        "amount": amount_key(amount).rstrip("0").rstrip(".") or "0",
        "currency": row["currency"],
        "krw": (f"{converted:.2f}" if converted is not None else None),
    }


def build_payload(
    *,
    snapshot: Mapping[str, Any] | None,
    rows: Sequence[Mapping[str, Any]],
    runs: Sequence[Mapping[str, Any]] = (),
    history: Sequence[Mapping[str, Any]] = (),
    generated: datetime | None = None,
) -> dict[str, Any]:
    """Everything the screen draws, in one shape.

    Pure: rows in, one dict out, no database. The renderer is pinned against
    this rather than against Postgres.
    """
    now = generated or datetime.now(timezone.utc)
    fx: FxRate | None = None
    if snapshot and snapshot.get("fx_rate") is not None:
        fx = FxRate(
            base=str(snapshot.get("fx_base") or "USD"),
            quote=str(snapshot.get("fx_quote") or "KRW"),
            rate=Decimal(str(snapshot["fx_rate"])),
            as_of=snapshot.get("fx_as_of"),
            source=str(snapshot.get("fx_source") or ""),
            fetched_at=snapshot.get("refreshed_at") or now,
        )

    payload_rows = [_row_payload(row, fx) for row in rows]
    by_provider: dict[str, dict[str, int]] = {}
    for row in payload_rows:
        counts = by_provider.setdefault(row["provider"], {"gpu": 0, "storage": 0})
        counts[row["category"]] += 1

    return {
        "providers": [
            {
                "id": pid,
                "label": label,
                "counts": by_provider.get(pid, {"gpu": 0, "storage": 0}),
            }
            for pid, label in PROVIDERS.items()
        ],
        "categories": [{"id": cid, "label": label} for cid, label in CATEGORIES.items()],
        "snapshot": (
            {
                "id": snapshot["id"],
                # When these numbers first appeared...
                "taken_at": snapshot["taken_at"],
                # ...and when we last confirmed they are still published.
                "refreshed_at": snapshot["refreshed_at"],
                "fx": (
                    {
                        "base": fx.base,
                        "quote": fx.quote,
                        "rate": f"{fx.rate:.4f}",
                        "as_of": fx.as_of,
                        "source": fx.source,
                    }
                    if fx
                    else None
                ),
            }
            if snapshot
            else None
        ),
        "runs": [
            {
                "provider": run["provider"],
                "outcome": run["outcome"],
                "detail": run.get("detail"),
                "source_url": run.get("source_url"),
                "row_count": run.get("row_count", 0),
                "fetched_at": run.get("fetched_at"),
            }
            for run in runs
        ],
        "rows": payload_rows,
        "history": [
            {
                "id": entry["id"],
                "taken_at": entry["taken_at"],
                "refreshed_at": entry["refreshed_at"],
                "changes": entry.get("changes", 0),
            }
            for entry in history
        ],
        "generated": now,
    }


# -------------------------------------------------------------- the database

_ROWS_SQL = """
    SELECT provider, category, sku, label, region, spec, amount, currency, unit, sort
    FROM cloud_price
    WHERE snapshot_id = %s
    ORDER BY provider, category, sort, sku
"""

_RUNS_SQL = """
    SELECT provider, outcome, detail, source_url, source_version, row_count, fetched_at
    FROM cloud_price_run
    WHERE snapshot_id = %s
    ORDER BY provider
"""

_HISTORY_SQL = """
    SELECT s.id, s.taken_at, s.refreshed_at,
           (SELECT count(*) FROM cloud_price_change c WHERE c.snapshot_id = s.id) AS changes
    FROM cloud_price_snapshot s
    ORDER BY s.taken_at DESC, s.id DESC
    LIMIT %s
"""


def _dicts(cursor, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
    cursor.execute(sql, params)
    columns = [column.name for column in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _current_snapshot(cursor) -> dict[str, Any] | None:
    rows = _dicts(
        cursor,
        "SELECT id, taken_at, refreshed_at, fx_base, fx_quote, fx_rate, fx_as_of,"
        "       fx_source, content_hash"
        "  FROM cloud_price_snapshot WHERE is_current LIMIT 1",
        (),
    )
    return rows[0] if rows else None


def read_current(database_url: str, *, history_limit: int = 30) -> dict[str, Any]:
    """The snapshot the screen opens on, or an empty payload before any refresh."""
    import psycopg

    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            snapshot = _current_snapshot(cursor)
            if snapshot is None:
                return build_payload(snapshot=None, rows=[], runs=[], history=[])
            rows = _dicts(cursor, _ROWS_SQL, (snapshot["id"],))
            runs = _dicts(cursor, _RUNS_SQL, (snapshot["id"],))
            history = _dicts(cursor, _HISTORY_SQL, (history_limit,))
    return build_payload(snapshot=snapshot, rows=rows, runs=runs, history=history)


def read_snapshot(database_url: str, snapshot_id: int) -> dict[str, Any]:
    """One past rate card, read the same way the current one is.

    This is what "기존 문서는 히스토리로 열람 가능" means concretely: a
    superseded snapshot keeps its rows and its exchange rate, so opening it
    shows the prices *and* the conversion that were on screen at the time --
    not today's rate applied to yesterday's prices.
    """
    import psycopg

    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            found = _dicts(
                cursor,
                "SELECT id, taken_at, refreshed_at, fx_base, fx_quote, fx_rate, fx_as_of,"
                "       fx_source, content_hash"
                "  FROM cloud_price_snapshot WHERE id = %s",
                (snapshot_id,),
            )
            if not found:
                raise LookupError(f"no such snapshot: {snapshot_id}")
            snapshot = found[0]
            rows = _dicts(cursor, _ROWS_SQL, (snapshot_id,))
            runs = _dicts(cursor, _RUNS_SQL, (snapshot_id,))
            changes = _dicts(
                cursor,
                "SELECT provider, category, sku, label, type, before_amount, after_amount,"
                "       currency, unit"
                "  FROM cloud_price_change WHERE snapshot_id = %s"
                " ORDER BY provider, category, sku",
                (snapshot_id,),
            )
    payload = build_payload(snapshot=snapshot, rows=rows, runs=runs, history=[])
    payload["changes"] = [
        {
            **{k: change[k] for k in ("provider", "category", "sku", "label", "type", "currency", "unit")},
            "before": (None if change["before_amount"] is None else amount_key(change["before_amount"]).rstrip("0").rstrip(".")),
            "after": (None if change["after_amount"] is None else amount_key(change["after_amount"]).rstrip("0").rstrip(".")),
        }
        for change in changes
    ]
    return payload


def previous_versions(database_url: str) -> dict[str, str]:
    """What each provider called its rate card last time it answered.

    AWS publishes a version on a 202MB file. Knowing the version we already
    hold is what lets the fetcher decide not to download it again.
    """
    import psycopg

    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            snapshot = _current_snapshot(cursor)
            if snapshot is None:
                return {}
            runs = _dicts(cursor, _RUNS_SQL, (snapshot["id"],))
    return {
        str(run["provider"]): str(run["source_version"])
        for run in runs
        if run["outcome"] == "ok" and run["source_version"]
    }


def apply_refresh(
    database_url: str,
    results: Sequence[SourceResult],
    fx: FxRate | None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Write one refresh, and decide whether it deserves a new snapshot.

    The rule HK asked for, in one place:

    * something moved  -> a new snapshot, and the old one stays readable
    * nothing moved    -> the snapshot already there keeps its rows and gets
                          today's date and today's exchange rate

    All of it in one transaction, because a half-written refresh would leave
    the history describing a table that does not exist.
    """
    import psycopg
    from psycopg.types.json import Jsonb

    taken_at = now or datetime.now(timezone.utc)

    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            snapshot = _current_snapshot(cursor)
            existing = _dicts(cursor, _ROWS_SQL, (snapshot["id"],)) if snapshot else []

            rows = carry_forward(results, existing)
            digest = content_hash(rows)
            diff = diff_rows(rows, existing)

            # Nothing moved: keep the snapshot, move the clock and the rate.
            # This is the branch that stops the history filling with identical
            # rate cards every time somebody presses the button.
            if snapshot is not None and digest == str(snapshot["content_hash"]):
                cursor.execute(
                    "UPDATE cloud_price_snapshot SET refreshed_at = %s, fx_rate = %s,"
                    "       fx_as_of = %s, fx_source = %s, fx_base = %s, fx_quote = %s"
                    " WHERE id = %s",
                    (
                        taken_at,
                        fx.rate if fx else None,
                        fx.as_of if fx else None,
                        fx.source if fx else None,
                        fx.base if fx else "USD",
                        fx.quote if fx else "KRW",
                        snapshot["id"],
                    ),
                )
                _write_runs(cursor, snapshot["id"], results, rows, taken_at, replace=True)
                connection.commit()
                return {
                    "changed": False,
                    "snapshot_id": snapshot["id"],
                    "added": 0, "updated": 0, "removed": 0,
                    "rows": len(rows),
                    "refreshed_at": taken_at,
                    "runs": _run_summary(results),
                }

            cursor.execute("UPDATE cloud_price_snapshot SET is_current = false WHERE is_current")
            cursor.execute(
                "INSERT INTO cloud_price_snapshot"
                " (taken_at, refreshed_at, fx_base, fx_quote, fx_rate, fx_as_of, fx_source,"
                "  content_hash, is_current)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, true) RETURNING id",
                (
                    taken_at, taken_at,
                    fx.base if fx else "USD",
                    fx.quote if fx else "KRW",
                    fx.rate if fx else None,
                    fx.as_of if fx else None,
                    fx.source if fx else None,
                    digest,
                ),
            )
            snapshot_id = cursor.fetchone()[0]

            for row in rows:
                cursor.execute(
                    "INSERT INTO cloud_price"
                    " (snapshot_id, provider, category, sku, label, region, spec, amount,"
                    "  currency, unit, sort)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        snapshot_id, row.provider, row.category, row.sku, row.label,
                        row.region, Jsonb(dict(row.spec)), row.amount, row.currency,
                        row.unit, row.sort,
                    ),
                )

            for change in diff.as_changes():
                cursor.execute(
                    "INSERT INTO cloud_price_change"
                    " (snapshot_id, provider, category, sku, label, type, before_amount,"
                    "  after_amount, currency, unit)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        snapshot_id, change.provider, change.category, change.sku,
                        change.label, change.type, change.before_amount,
                        change.after_amount, change.currency, change.unit,
                    ),
                )

            _write_runs(cursor, snapshot_id, results, rows, taken_at, replace=False)
        connection.commit()

    return {
        "changed": True,
        "snapshot_id": snapshot_id,
        "added": len(diff.added),
        "updated": len(diff.changed),
        "removed": len(diff.removed),
        "rows": len(rows),
        "refreshed_at": taken_at,
        "runs": _run_summary(results),
    }


def _write_runs(
    cursor,
    snapshot_id: int,
    results: Sequence[SourceResult],
    rows: Sequence[PriceRow],
    when: datetime,
    *,
    replace: bool,
) -> None:
    """Record what each provider did on this refresh.

    `row_count` is counted off the rows the snapshot actually ends up holding,
    not off what the fetcher returned. A provider that failed, or that said
    "same edition as last time", contributes no rows of its own and would
    otherwise be filed as nought -- which on screen reads as a provider with
    nothing to sell, beside a table that is plainly listing its machines.

    On the unchanged branch the runs are replaced rather than added to: the
    question the table answers is "did this provider answer *last time we
    asked*", and keeping the previous attempt's verdict beside the new one
    would make it unanswerable.
    """
    if replace:
        cursor.execute("DELETE FROM cloud_price_run WHERE snapshot_id = %s", (snapshot_id,))
    held: dict[str, int] = {}
    for row in rows:
        held[row.provider] = held.get(row.provider, 0) + 1
    for result in results:
        cursor.execute(
            "INSERT INTO cloud_price_run"
            " (snapshot_id, provider, outcome, detail, source_url, source_version,"
            "  row_count, fetched_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                snapshot_id, result.provider, result.outcome, result.detail,
                result.source_url, result.source_version,
                held.get(result.provider, 0), when,
            ),
        )


def _run_summary(results: Sequence[SourceResult]) -> list[dict[str, Any]]:
    return [
        {
            "provider": result.provider,
            "outcome": result.outcome,
            "detail": result.detail,
            "rows": len(result.rows),
        }
        for result in results
    ]
