"""What a GPU-hour costs at five clouds, and what that is in won.

The parsers are pinned against fixtures cut from the real pages rather than
against the live sites: a test that goes to the internet fails when a marketing
team ships a redesign, which is information, but it is not information about
this code and it cannot be acted on at 3am.

The database half runs against a real Postgres when `WORKLOG_TEST_DATABASE_URL`
is set and is skipped otherwise, because a test that quietly passes with no
database behind it is worse than one that says it was skipped.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from rlwrld_worklog import cloud_pricing as cp
from rlwrld_worklog import cloud_pricing_sources as sources

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "cloud_pricing"

REQUIRES_DATABASE = pytest.mark.skipif(
    not os.environ.get("WORKLOG_TEST_DATABASE_URL"),
    reason="set WORKLOG_TEST_DATABASE_URL to a throwaway database",
)

NOW = datetime(2026, 9, 30, 9, 0, tzinfo=timezone.utc)


def row(**overrides: Any) -> cp.PriceRow:
    base: dict[str, Any] = {
        "provider": "nebius", "category": "gpu", "sku": "H100", "label": "NVIDIA HGX H100",
        "amount": Decimal("3.85"), "currency": "USD", "unit": "GPU-hour",
    }
    base.update(overrides)
    return cp.PriceRow(**base)


def fx(rate: str = "1350.0", *, as_of: datetime | None = None) -> cp.FxRate:
    return cp.FxRate(
        base="USD", quote="KRW", rate=Decimal(rate),
        as_of=as_of or datetime(2026, 9, 30, 0, 0, tzinfo=timezone.utc),
        source="test", fetched_at=NOW,
    )


# --- the price and its conversion are two different facts -------------------


def test_the_original_price_is_kept_exactly_as_the_provider_published_it() -> None:
    """The 원가. $0.0147/GiB-month does not survive a float, so it is a string."""
    payload = cp.build_payload(
        snapshot={"id": 1, "taken_at": NOW, "refreshed_at": NOW, "fx_rate": Decimal("1350"),
                  "fx_base": "USD", "fx_quote": "KRW", "fx_as_of": NOW, "fx_source": "t"},
        rows=[{"provider": "nebius", "category": "storage", "sku": "obj", "label": "Object",
               "amount": Decimal("0.0147"), "currency": "USD", "unit": "GiB/month",
               "region": None, "spec": {}}],
    )
    assert payload["rows"][0]["amount"] == "0.0147"


def test_a_won_price_converts_at_one_rather_than_reading_as_unconvertible() -> None:
    """Naver and Kakao publish in won. That is not a missing conversion."""
    assert fx().convert(Decimal("4309"), "KRW") == Decimal("4309")


def test_a_currency_we_have_no_rate_for_converts_to_nothing_not_to_itself() -> None:
    """A number in the 원화 column that is not won would be a lie told by a cell."""
    assert fx().convert(Decimal("10"), "JPY") is None


def test_the_rate_and_both_of_its_timestamps_reach_the_screen() -> None:
    """`as_of` is the quote's own time; `refreshed_at` is when we asked.

    A screen showing only the second overstates how fresh the conversion is.
    """
    quoted = datetime(2026, 9, 30, 0, 2, tzinfo=timezone.utc)
    payload = cp.build_payload(
        snapshot={"id": 1, "taken_at": NOW, "refreshed_at": NOW, "fx_rate": Decimal("1353.68"),
                  "fx_base": "USD", "fx_quote": "KRW", "fx_as_of": quoted,
                  "fx_source": "exchangerate-api"},
        rows=[],
    )
    assert payload["snapshot"]["fx"] == {
        "base": "USD", "quote": "KRW", "rate": "1353.6800",
        "as_of": quoted, "source": "exchangerate-api",
    }
    assert payload["snapshot"]["refreshed_at"] == NOW


def test_without_a_rate_the_prices_still_show_and_the_won_column_is_empty() -> None:
    """A rate card with no conversion is worth more than one converted at a guess."""
    payload = cp.build_payload(
        snapshot={"id": 1, "taken_at": NOW, "refreshed_at": NOW, "fx_rate": None},
        rows=[{"provider": "aws", "category": "gpu", "sku": "p5.48xlarge", "label": "p5.48xlarge",
               "amount": Decimal("55.04"), "currency": "USD", "unit": "Hrs",
               "region": None, "spec": {}}],
    )
    assert payload["rows"][0]["amount"] == "55.04"
    assert payload["rows"][0]["krw"] is None
    assert payload["snapshot"]["fx"] is None


# --- what counts as a change ------------------------------------------------


def test_the_same_prices_in_a_different_order_are_the_same_rate_card() -> None:
    """Otherwise a provider that reorders its table reads as a price change."""
    one = row(sku="A", amount=Decimal("1")), row(sku="B", amount=Decimal("2"))
    assert cp.content_hash(one) == cp.content_hash(tuple(reversed(one)))


def test_a_reworded_label_is_not_a_price_change() -> None:
    """The screen is about money. Identity hangs on the sku, not the wording."""
    before = [{"provider": "nebius", "category": "gpu", "sku": "H100", "label": "old wording",
               "amount": Decimal("3.85"), "currency": "USD", "unit": "GPU-hour"}]
    diff = cp.diff_rows([row(label="new wording")], before)
    assert diff.moved == 0


def test_a_moved_price_is_recorded_with_what_it_moved_from() -> None:
    before = [{"provider": "nebius", "category": "gpu", "sku": "H100", "label": "H100",
               "amount": Decimal("3.85"), "currency": "USD", "unit": "GPU-hour"}]
    diff = cp.diff_rows([row(amount=Decimal("4.50"))], before)
    change = diff.as_changes()[0]
    assert (change.type, change.before_amount, change.after_amount) == (
        "changed", Decimal("3.85"), Decimal("4.50"),
    )


def test_a_price_that_differs_below_the_stored_scale_is_the_same_price() -> None:
    before = [{"provider": "nebius", "category": "gpu", "sku": "H100", "label": "H100",
               "amount": Decimal("3.8500000000"), "currency": "USD", "unit": "GPU-hour"}]
    assert cp.diff_rows([row(amount=Decimal("3.85"))], before).moved == 0


def test_a_round_number_hashes_the_same_however_the_fetcher_spelled_it() -> None:
    """Decimal.normalize() renders 1000 as 1E+3, which would make the hash
    depend on the parser's spelling rather than on the price."""
    assert cp.amount_key(Decimal("1000")) == cp.amount_key(Decimal("1E+3"))


# --- a provider that did not answer -----------------------------------------


def test_a_failed_provider_keeps_the_prices_it_last_published() -> None:
    """The failure this module exists to prevent: a redesign reading as
    five deletions, then five re-additions on the next refresh."""
    existing = [{"provider": "naver", "category": "gpu", "sku": "gp1ls16-g3", "label": "gp1ls16-g3",
                 "amount": Decimal("4309"), "currency": "KRW", "unit": "시간",
                 "region": "KR", "spec": {}, "sort": 0}]
    results = [
        cp.SourceResult(provider="nebius", rows=[row()]),
        cp.SourceResult.failure("naver", "the page carried no tables"),
    ]
    carried = cp.carry_forward(results, existing)
    assert {(r.provider, r.sku) for r in carried} == {("nebius", "H100"), ("naver", "gp1ls16-g3")}
    assert cp.diff_rows(carried, existing).removed == []


def test_a_provider_that_says_nothing_changed_keeps_its_rows_too() -> None:
    """AWS answers with a version rather than 200MB when the edition holds.

    `reused` is what separates that from an empty successful fetch, which
    would mean AWS had withdrawn every machine it sells.
    """
    existing = [{"provider": "aws", "category": "gpu", "sku": "p5.48xlarge", "label": "p5.48xlarge",
                 "amount": Decimal("55.04"), "currency": "USD", "unit": "Hrs",
                 "region": "ap-northeast-2", "spec": {}, "sort": 0}]
    reused = cp.SourceResult(provider="aws", rows=[], outcome="ok", reused=True)
    assert not reused.supplies_rows
    carried = cp.carry_forward([reused], existing)
    assert [(r.provider, r.sku) for r in carried] == [("aws", "p5.48xlarge")]


def test_an_empty_successful_fetch_does_read_as_a_withdrawal() -> None:
    """The other side of the same rule: `ok` with no rows and no `reused`
    means the provider really did stop publishing, and the diff says so."""
    existing = [{"provider": "vessl", "category": "gpu", "sku": "H100", "label": "H100",
                 "amount": Decimal("2.98"), "currency": "USD", "unit": "hr",
                 "region": None, "spec": {}, "sort": 0}]
    carried = cp.carry_forward([cp.SourceResult(provider="vessl", rows=[])], existing)
    assert carried == []
    assert len(cp.diff_rows(carried, existing).removed) == 1


# --- reading the pages ------------------------------------------------------


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_contact_us_is_not_a_price_and_free_is() -> None:
    """A machine you have to phone about is not a free machine."""
    assert sources.parse_amount("Contact us") is None
    assert sources.parse_amount("문의하기") is None
    assert sources.parse_amount("-") is None
    assert sources.parse_amount("") is None
    assert sources.parse_amount("Free") == Decimal(0)
    assert sources.parse_amount("$3.85") == Decimal("3.85")
    assert sources.parse_amount("from $1.55") == Decimal("1.55")
    assert sources.parse_amount("4,309 원") == Decimal("4309")


def test_nebius_gpu_and_storage_come_off_the_div_grid() -> None:
    rows = sources.parse_nebius(fixture("nebius.html"))
    gpu = {r.sku: r for r in rows if r.category == "gpu"}
    storage = {r.sku: r for r in rows if r.category == "storage"}
    assert gpu["NVIDIA HGX H100"].amount == Decimal("3.85")
    assert gpu["NVIDIA HGX H100"].spec["vCPU"] == "16"
    assert storage["WEKA filesystem"].amount == Decimal("0.1000")
    assert storage["WEKA filesystem"].unit == "GiB/month"
    # "Contact us" rows carry no price and must not arrive as zero.
    assert "NVIDIA GB300 NVL72" not in gpu


def test_the_nebius_spot_table_is_not_mixed_into_the_on_demand_column() -> None:
    """A comparison that puts spot beside on-demand in one column is worse
    than no comparison. The H100 spot price is $0.79; it must not be here."""
    rows = sources.parse_nebius(fixture("nebius.html"))
    assert all(r.amount != Decimal("0.79") for r in rows)


def test_naver_reads_the_hourly_won_price_off_the_gpu_table() -> None:
    rows = sources.parse_naver(fixture("naver_gpu.html"))
    found = {r.sku: r for r in rows}
    assert found["gp1ls16-g3"].amount == Decimal("4309")
    assert found["gp1ls16-g3"].currency == "KRW"
    # "1개 x 48GB" is two facts in one cell; they live in two columns now.
    assert found["gp1ls16-g3"].gpu_count == 1
    assert found["gp1ls16-g3"].gpu_memory_gb == Decimal("48")
    # The models Naver does not sell by the hour print a dash, not a zero.
    assert all(r.amount > 0 for r in rows)


def test_naver_publishes_no_storage_prices_to_read() -> None:
    """Naver renders storage prices in from its calculator after load, so the
    cells arrive empty. Reading them would publish zeros for storage that
    costs money."""
    rows = sources.parse_naver(fixture("naver_gpu.html"))
    assert {r.category for r in rows} == {"gpu"}


def test_vessl_reads_the_on_demand_column_and_the_storage_cards() -> None:
    rows = sources.parse_vessl(fixture("vessl.html"))
    gpu = {r.sku: r for r in rows if r.category == "gpu"}
    storage = {r.sku: r for r in rows if r.category == "storage"}
    assert gpu["NVIDIA H100 SXM"].amount == Decimal("2.98")
    assert gpu["NVIDIA H100 SXM"].gpu_memory_gb == Decimal("80")
    # The status chip is glued to the model name in the cell; it is not
    # part of what the machine is called.
    assert all("문의" not in sku and "바로 시작" not in sku for sku in gpu)
    assert storage["Warm-Cluster Storage"].amount == Decimal("0.20")


def test_kakao_takes_its_gpus_from_the_accelerator_field_not_from_the_name() -> None:
    rows = sources.parse_kakao(fixture("kakao_calc.json"))
    gpu = {r.sku: r for r in rows if r.category == "gpu"}
    assert gpu["gn1i.xlarge"].amount == Decimal("648")
    assert gpu["gn1i.xlarge"].spec["accelerator"] == "T4 x1"
    assert gpu["gn1i.xlarge"].currency == "KRW"
    # A general-purpose instance has no accelerator and is not a GPU price,
    # however much it looks like one sitting in the same dropdown.
    assert "m3az.large" not in gpu
    assert any(r.category == "storage" for r in rows)


def test_a_page_that_parsed_to_nothing_is_an_error_not_an_empty_rate_card() -> None:
    """A silent zero looks exactly like a provider that stopped selling GPUs."""
    for parse in (sources.parse_nebius, sources.parse_naver, sources.parse_vessl):
        with pytest.raises(sources.SourceError):
            parse("<html><body><p>redesigned</p></body></html>")
    with pytest.raises(sources.SourceError):
        sources.parse_kakao("{}")


def test_the_aws_price_list_is_filtered_to_gpu_machines_and_storage() -> None:
    rows, version = sources.parse_aws_csv(iter(fixture("aws_ec2.csv").splitlines(keepends=True)))
    gpu = {r.sku: r for r in rows if r.category == "gpu"}
    storage = {r.sku: r for r in rows if r.category == "storage"}
    assert version == "20260925174521"
    assert gpu["p5.48xlarge"].amount == Decimal("75.9552000000")
    assert gpu["p5.48xlarge"].gpu_count == 8
    assert storage["ebs-gp3"].amount == Decimal("0.0912000000")
    # Windows, dedicated tenancy, reserved terms and non-GPU machines are all
    # priced in the same file and none of them belongs on this screen.
    assert "m5.large" not in gpu
    assert all(r.currency == "USD" for r in rows)


def test_an_aws_file_whose_columns_moved_is_reported_rather_than_misread() -> None:
    header = '"SKU","TermType","Nope"\n'
    lines = ['"FormatVersion","v1.0"\n'] * 5 + [header]
    with pytest.raises(sources.SourceError):
        sources.parse_aws_csv(iter(lines))


# --- the refresh rule -------------------------------------------------------


def _migrate(url: str) -> None:
    from rlwrld_worklog.ledger.load import apply_migrations

    apply_migrations(
        database_url=url,
        migrations_dir=Path(__file__).resolve().parents[1] / "sql" / "migrations",
        dry_run=False,
    )


def _clear(url: str) -> None:
    import psycopg

    with psycopg.connect(url) as connection:
        with connection.cursor() as cursor:
            cursor.execute("TRUNCATE cloud_price_snapshot RESTART IDENTITY CASCADE")
        connection.commit()


@pytest.fixture()
def database() -> str:
    url = os.environ["WORKLOG_TEST_DATABASE_URL"]
    _migrate(url)
    _clear(url)
    return url


@REQUIRES_DATABASE
def test_a_refresh_that_changes_nothing_moves_the_date_and_not_the_history(database: str) -> None:
    """HK's rule, and the reason the history is worth opening: 없으면
    날짜/환율만 바꾸고 그냥 업데이트."""
    first = cp.apply_refresh(
        database, [cp.SourceResult(provider="nebius", rows=[row()])], fx("1350"), now=NOW,
    )
    assert first["changed"] is True

    later = NOW + timedelta(hours=6)
    second = cp.apply_refresh(
        database, [cp.SourceResult(provider="nebius", rows=[row()])], fx("1362.5"), now=later,
    )
    assert second["changed"] is False
    assert second["snapshot_id"] == first["snapshot_id"]

    payload = cp.read_current(database)
    # The same snapshot, with a new reading time and a new rate on it.
    assert payload["snapshot"]["id"] == first["snapshot_id"]
    assert payload["snapshot"]["taken_at"] == NOW
    assert payload["snapshot"]["refreshed_at"] == later
    assert payload["snapshot"]["fx"]["rate"] == "1362.5000"
    assert len(payload["history"]) == 1


@REQUIRES_DATABASE
def test_a_changed_price_makes_a_new_snapshot_and_leaves_the_old_one_readable(
    database: str,
) -> None:
    """기존과 달라진게 있으면 기존 문서는 히스토리로 열람 가능하게."""
    first = cp.apply_refresh(
        database, [cp.SourceResult(provider="nebius", rows=[row()])], fx("1350"), now=NOW,
    )
    later = NOW + timedelta(days=1)
    second = cp.apply_refresh(
        database,
        [cp.SourceResult(provider="nebius", rows=[row(amount=Decimal("4.50"))])],
        fx("1400"),
        now=later,
    )
    assert second["changed"] is True
    assert second["snapshot_id"] != first["snapshot_id"]
    assert (second["added"], second["updated"], second["removed"]) == (0, 1, 0)

    current = cp.read_current(database)
    assert current["snapshot"]["id"] == second["snapshot_id"]
    assert current["rows"][0]["amount"] == "4.5"
    assert [entry["id"] for entry in current["history"]] == [
        second["snapshot_id"], first["snapshot_id"],
    ]

    # The superseded rate card keeps its own prices *and* its own rate: a
    # history re-converted at today's rate would quietly rewrite itself.
    old = cp.read_snapshot(database, first["snapshot_id"])
    assert old["rows"][0]["amount"] == "3.85"
    assert old["snapshot"]["fx"]["rate"] == "1350.0000"
    assert old["rows"][0]["krw"] == "5197.50"
    assert old["changes"][0]["type"] == "added"


@REQUIRES_DATABASE
def test_the_won_column_is_the_price_times_the_rate_beside_it(database: str) -> None:
    cp.apply_refresh(
        database,
        [cp.SourceResult(provider="naver", rows=[
            row(provider="naver", sku="gp1ls16-g3", label="gp1ls16-g3",
                amount=Decimal("4309"), currency="KRW", unit="시간"),
            row(amount=Decimal("3.85")),
        ])],
        fx("1353.68"),
        now=NOW,
    )
    payload = cp.read_current(database)
    by_sku = {r["sku"]: r for r in payload["rows"]}
    assert by_sku["H100"]["krw"] == "5211.67"        # 3.85 * 1353.68
    assert by_sku["gp1ls16-g3"]["krw"] == "4309.00"  # already won


@REQUIRES_DATABASE
def test_a_provider_that_failed_is_named_on_the_screen_with_its_rows_intact(
    database: str,
) -> None:
    cp.apply_refresh(
        database,
        [cp.SourceResult(provider="nebius", rows=[row()]),
         cp.SourceResult(provider="naver", rows=[
             row(provider="naver", sku="gp1ls16-g3", label="gp1ls16-g3",
                 amount=Decimal("4309"), currency="KRW", unit="시간")])],
        fx(),
        now=NOW,
    )
    result = cp.apply_refresh(
        database,
        [cp.SourceResult(provider="nebius", rows=[row()]),
         cp.SourceResult.failure("naver", "the page carried no tables")],
        fx(),
        now=NOW + timedelta(hours=1),
    )
    assert result["changed"] is False
    payload = cp.read_current(database)
    runs = {run["provider"]: run for run in payload["runs"]}
    assert runs["naver"]["outcome"] == "failed"
    assert "no tables" in runs["naver"]["detail"]
    # Named as failed, and still carrying the prices it last published --
    # counted off the rows the snapshot actually holds, not off the fetch.
    assert runs["naver"]["row_count"] == 1
    assert any(r["provider"] == "naver" for r in payload["rows"])


@REQUIRES_DATABASE
def test_before_any_refresh_the_screen_says_so_rather_than_showing_nothing(
    database: str,
) -> None:
    payload = cp.read_current(database)
    assert payload["snapshot"] is None
    assert payload["rows"] == []
    assert [p["id"] for p in payload["providers"]] == list(cp.PROVIDERS)


# --- which chip, how many, how much memory ----------------------------------


def test_the_longer_model_name_wins_over_the_one_inside_it() -> None:
    """GB300 is not a B300 and an L40S is not an L4."""
    assert sources.parse_gpu_model("NVIDIA GB300 NVL72") == "GB300"
    assert sources.parse_gpu_model("NVIDIA L40S with Intel CPU") == "L40S"
    assert sources.parse_gpu_model("NVIDIA HGX H100") == "H100"


def test_a_model_name_is_not_read_out_of_a_machine_code() -> None:
    """`gp1l4-g3` is a Naver spec code that happens to contain "l4"."""
    assert sources.parse_gpu_model("gp1l4-g3") is None
    assert sources.parse_gpu_model("L4 (KVM기반)") == "L4"
    # A CPU is not an accelerator, however many numbers are in its name.
    assert sources.parse_gpu_model("AMD EPYC 7R13 Processor") is None


def test_memory_and_count_come_off_the_shapes_the_providers_use() -> None:
    assert sources.parse_gpu_memory_gb("80GB") == Decimal("80")
    assert sources.parse_gpu_memory_gb("141GB HBM3e") == Decimal("141")
    assert sources.parse_gpu_memory_gb("1개 x 48GB") == Decimal("48")
    assert sources.parse_gpu_memory_gb("") is None
    assert sources.parse_gpu_count("A100 x1") == 1
    assert sources.parse_gpu_count("8개 x 80GB") == 8
    # "288GB x N" is Nebius saying "as many as you ask for".
    assert sources.parse_gpu_count("288GB × N") is None


def test_aws_names_the_chip_from_the_family_and_divides_its_memory() -> None:
    """AWS publishes the count and the memory of the whole machine, never the
    model. p5.48xlarge is 8 x H100 and 640GB across them -- 80GB each."""
    rows, _ = sources.parse_aws_csv(iter(fixture("aws_ec2.csv").splitlines(keepends=True)))
    gpu = {r.sku: r for r in rows if r.category == "gpu"}
    assert (gpu["p5.48xlarge"].gpu_model, gpu["p5.48xlarge"].gpu_count) == ("H100", 8)
    assert gpu["p5.48xlarge"].gpu_memory_gb == Decimal("80")
    assert gpu["g4dn.xlarge"].gpu_model == "T4"
    assert gpu["g6e.xlarge"].gpu_model == "L40S"


def test_an_aws_family_nobody_told_us_about_has_no_model_rather_than_a_guess() -> None:
    assert sources.AWS_GPU_MODEL.get("p9") is None


def test_naver_takes_the_chip_from_the_heading_above_the_table() -> None:
    """The table never names the model; the section heading does."""
    rows = sources.parse_naver(fixture("naver_gpu.html"))
    found = {r.sku: r for r in rows}
    assert found["gp1ls16-g3"].gpu_model == "L40S"
    assert (found["gp1ls16-g3"].gpu_count, found["gp1ls16-g3"].gpu_memory_gb) == (1, Decimal("48"))
    assert found["gp1l4-g3"].gpu_model == "L4"
    assert all(r.gpu_model for r in rows), "every Naver row sits under a heading"


def test_the_naver_heading_is_the_word_before_the_bracket_not_the_sentence() -> None:
    """The paragraph above the L40S table ends "... A100, H200 L40S (KVM기반)";
    a heading pattern that spans spaces swallows the sentence with it."""
    rows = sources.parse_naver(fixture("naver_gpu.html"))
    assert {r.gpu_model for r in rows} <= set(sources.GPU_MODELS)


def test_vessl_and_nebius_and_kakao_name_the_chip_they_sell() -> None:
    vessl = {r.sku: r for r in sources.parse_vessl(fixture("vessl.html")) if r.category == "gpu"}
    assert vessl["NVIDIA H100 SXM"].gpu_model == "H100"
    assert vessl["NVIDIA H100 SXM"].gpu_memory_gb == Decimal("80")

    nebius = {r.sku: r for r in sources.parse_nebius(fixture("nebius.html")) if r.category == "gpu"}
    assert nebius["NVIDIA HGX H100"].gpu_model == "H100"
    # Nebius publishes no GPU memory, and we do not supply one.
    assert nebius["NVIDIA HGX H100"].gpu_memory_gb is None

    kakao = {r.sku: r for r in sources.parse_kakao(fixture("kakao_calc.json")) if r.category == "gpu"}
    assert (kakao["gn1i.xlarge"].gpu_model, kakao["gn1i.xlarge"].gpu_count) == ("T4", 1)
    assert kakao["gn1i.xlarge"].gpu_memory_gb is None


def test_a_description_is_not_a_price_change() -> None:
    """A provider that starts naming a chip it used to leave blank has not
    changed its prices, and the 요금 history must not fill up with that."""
    before = row()
    after = row(gpu_model="H100", gpu_memory_gb=Decimal("80"))
    assert cp.content_hash([before]) == cp.content_hash([after])


@REQUIRES_DATABASE
def test_a_description_still_lands_on_a_snapshot_whose_prices_held(database: str) -> None:
    """The bug this would otherwise be: the hash is over price alone, so a
    newly-parsed GPU model arrives on an "unchanged" refresh -- and the rows,
    never rewritten on that branch, would keep the blank for as long as the
    price held. Which is every row already in the table."""
    first = cp.apply_refresh(
        database, [cp.SourceResult(provider="nebius", rows=[row()])], fx(), now=NOW,
    )
    second = cp.apply_refresh(
        database,
        [cp.SourceResult(provider="nebius", rows=[
            row(gpu_model="H100", gpu_count=1, gpu_memory_gb=Decimal("80"))])],
        fx(),
        now=NOW + timedelta(hours=1),
    )
    assert second["changed"] is False
    assert second["snapshot_id"] == first["snapshot_id"]
    shown = cp.read_current(database)["rows"][0]
    assert (shown["gpu_model"], shown["gpu_count"], shown["gpu_memory_gb"]) == ("H100", 1, "80")


# --- comparing five clouds on one unit --------------------------------------


def test_a_machine_price_is_divided_by_the_cards_it_buys() -> None:
    """AWS sells eight H100s on one invoice line and Nebius sells one. Putting
    the two hourly figures side by side makes packaging look like price."""
    assert cp.per_gpu_hour(Decimal("75.9552"), "Hrs", 8) == Decimal("9.4944")
    assert cp.per_gpu_hour(Decimal("3.85"), "GPU-hour", 1) == Decimal("3.85")
    assert cp.per_gpu_hour(Decimal("4309"), "시간", 1) == Decimal("4309")


def test_a_row_we_cannot_put_in_that_unit_has_no_number_rather_than_a_guess() -> None:
    # Count unknown: dividing by an assumed 1 would publish a machine price
    # as a card price, which is the exact error the column exists to prevent.
    assert cp.per_gpu_hour(Decimal("10"), "Hrs", None) is None
    assert cp.per_gpu_hour(Decimal("10"), "Hrs", 0) is None
    # Storage is a real price and not rankable against a GPU-hour.
    assert cp.per_gpu_hour(Decimal("0.0912"), "GB-Mo", 1) is None


def test_the_comparison_figure_reaches_the_screen_in_won() -> None:
    payload = cp.build_payload(
        snapshot={"id": 1, "taken_at": NOW, "refreshed_at": NOW, "fx_rate": Decimal("1360"),
                  "fx_base": "USD", "fx_quote": "KRW", "fx_as_of": NOW, "fx_source": "t"},
        rows=[{"provider": "aws", "category": "gpu", "sku": "p5.48xlarge",
               "label": "p5.48xlarge", "amount": Decimal("75.9552"), "currency": "USD",
               "unit": "Hrs", "region": None, "spec": {}, "gpu_model": "H100",
               "gpu_count": 8, "gpu_memory_gb": Decimal("80")}],
    )
    row = payload["rows"][0]
    assert row["per_gpu"] == "9.4944"
    assert row["per_gpu_krw"] == "12912.38"     # 9.4944 * 1360
    # The whole-machine figures are still there beside it.
    assert row["krw"] == "103299.07"


def test_a_round_memory_keeps_its_zero() -> None:
    """80 rendered as "8" for as long as the value came from a parser rather
    than from a numeric(10,2) column that padded it back."""
    payload = cp.build_payload(
        snapshot=None,
        rows=[{"provider": "vessl", "category": "gpu", "sku": "H100", "label": "H100",
               "amount": Decimal("2.98"), "currency": "USD", "unit": "hr",
               "gpu_memory_gb": Decimal("80")}],
    )
    assert payload["rows"][0]["gpu_memory_gb"] == "80"


def test_nebius_and_vessl_quote_one_card_and_say_so() -> None:
    """Both publish a per-card rate -- Nebius in the unit itself, VESSL in a
    calculator that multiplies the listed figure by a separate 수량. Without a
    count neither could be compared at all."""
    nebius = {r.sku: r for r in sources.parse_nebius(fixture("nebius.html")) if r.category == "gpu"}
    assert nebius["NVIDIA HGX H100"].gpu_count == 1
    vessl = {r.sku: r for r in sources.parse_vessl(fixture("vessl.html")) if r.category == "gpu"}
    assert vessl["NVIDIA H100 SXM"].gpu_count == 1


def test_every_provider_has_a_page_a_person_can_open() -> None:
    """The citation is the published page, not the file the fetcher read."""
    assert set(cp.PROVIDER_PAGE) == set(cp.PROVIDERS)
    assert all(url.startswith("https://") for url in cp.PROVIDER_PAGE.values())
    # AWS is read from a CSV nobody can check by eye; it is not the citation.
    assert "pricing.us-east-1.amazonaws.com" not in cp.PROVIDER_PAGE["aws"]


def test_the_page_reaches_the_screen_beside_each_provider() -> None:
    payload = cp.build_payload(snapshot=None, rows=[])
    pages = {p["id"]: p["page"] for p in payload["providers"]}
    assert pages["nebius"] == "https://nebius.com/prices"
    assert all(pages[pid] for pid in cp.PROVIDERS)


def test_an_aws_row_carries_the_region_its_price_is_for() -> None:
    """AWS charges differently by region, so a price without one is half a
    fact. Today every AWS row is Seoul; the column is what makes that
    visible rather than assumed."""
    rows, _ = sources.parse_aws_csv(iter(fixture("aws_ec2.csv").splitlines(keepends=True)))
    assert {r.region for r in rows} == {sources.AWS_REGION}
    assert sources.AWS_REGION == "ap-northeast-2"


def test_a_vessl_sku_does_not_change_when_vessl_rewords_its_status_chip() -> None:
    """2026-10-06: "바로 시작" became "셀프서브", and because the chip was
    stripped by matching a list of known words it stayed glued to the name.
    Every sku changed, and a day on which no price moved was written into the
    price history as six deletions and three additions.

    The product name is Latin and the chip is Hangul, so the cut is structural
    and does not depend on a vocabulary somebody else maintains.
    """
    def one(chip: str) -> str:
        markup = (
            "<table><tr><th>GPU 모델</th><th>VRAM</th><th>아키텍처</th>"
            "<th>온디맨드</th></tr>"
            f"<tr><td>NVIDIA H100 SXM{chip}</td><td>80GB</td><td>Hopper</td>"
            "<td>$2.98/시간</td></tr></table>"
        )
        return sources.parse_vessl(markup)[0].sku

    assert one("바로 시작") == "NVIDIA H100 SXM"
    assert one("셀프서브") == "NVIDIA H100 SXM"
    assert one("무엇이든새로운말") == "NVIDIA H100 SXM"
    assert one("") == "NVIDIA H100 SXM"
