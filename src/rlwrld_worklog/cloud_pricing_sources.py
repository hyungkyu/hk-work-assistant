"""Where the numbers come from: five rate cards and one exchange rate.

Only one of these five providers publishes a machine-readable price list as a
product. AWS does, and it is the sort of thing you can build on. The other four
publish a web page, so this module reads the page -- and a page can be
redesigned between one refresh and the next, which is the fact that shapes
everything here:

  1. **A fetcher never raises into the caller.** Every one returns a
     :class:`SourceResult`, and a failure is a result with `outcome="failed"`
     and a sentence saying what went wrong. The refresh then carries that
     provider's previous rows forward instead of recording a fabricated mass
     deletion. Four screens' worth of prices must not disappear because one
     marketing team shipped a redesign.

  2. **A parser that finds nothing says so.** Every fetcher checks it came back
     with rows and fails loudly when it did not. A silent zero is the failure
     this whole module is arranged to prevent: it looks exactly like a provider
     that stopped selling GPUs.

  3. **Only what the provider actually published.** "Contact us" is not a
     price and is skipped rather than guessed at; "Free" is a price and is
     recorded as zero. Nothing here converts currency -- conversion belongs to
     the reading, with the rate beside it (see :mod:`cloud_pricing`).

The HTML parsing is deliberately structural -- rows and cells, by table or by
grid class -- rather than regex over the whole page. When Nebius restyles its
table this breaks in the parser and says so, instead of matching a dollar sign
somewhere else on the page and reporting a confident wrong number.
"""

from __future__ import annotations

import csv
import io
import itertools
import json
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

from .cloud_pricing import FxRate, PriceRow, SourceResult

USER_AGENT = "rlwrld-worklog-cloud-pricing/1.0 (+internal backoffice)"

# The region every provider is read for. Seoul where the provider has one:
# comparing Seoul against Virginia would be comparing two different products.
AWS_REGION = "ap-northeast-2"

AWS_EC2_CSV = (
    "https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonEC2/current/"
    f"{AWS_REGION}/index.csv"
)
AWS_S3_JSON = (
    "https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonS3/current/"
    f"{AWS_REGION}/index.json"
)
NEBIUS_URL = "https://nebius.com/prices"
KAKAO_URL = "https://www.kakaocloud.com/pricing/calculator"
NAVER_GPU_URL = "https://www.ncloud.com/product/compute/gpuServer"
VESSL_URL = "https://vessl.ai/pricing"
FX_URL = "https://open.er-api.com/v6/latest/USD"

# The GPU instance families worth listing. A prefix list rather than "anything
# with a GPU attribute": AWS marks some non-GPU machines with one, and a
# rate card nobody can read is not a rate card.
AWS_GPU_PREFIXES = (
    "p3.", "p3dn.", "p4d.", "p4de.", "p5.", "p5e.", "p5en.", "p6.", "p6e.",
    "g4dn.", "g5.", "g6.", "g6e.", "gr6.",
)


class SourceError(RuntimeError):
    """A fetch or a parse that could not produce a rate card."""


# ------------------------------------------------------------------ fetching


def _open(url: str, *, timeout: float = 30.0):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        return urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as error:
        raise SourceError(f"{url} answered HTTP {error.code}") from error
    except Exception as error:  # URLError, socket timeout, ssl, ...
        raise SourceError(f"{url} could not be reached: {error}") from error


def _get_text(url: str, *, timeout: float = 30.0) -> str:
    with _open(url, timeout=timeout) as response:
        return response.read().decode("utf-8", "replace")


def _get_json(url: str, *, timeout: float = 60.0) -> Any:
    with _open(url, timeout=timeout) as response:
        try:
            return json.load(response)
        except ValueError as error:
            raise SourceError(f"{url} did not return JSON: {error}") from error


# --------------------------------------------------------------- html tables


def _strip_noise(markup: str) -> str:
    markup = re.sub(r"<script.*?</script>", "", markup, flags=re.S | re.I)
    markup = re.sub(r"<style.*?</style>", "", markup, flags=re.S | re.I)
    return markup.replace(" ", " ").replace("﻿", "")


def _cell_text(parts: Iterable[str]) -> str:
    return re.sub(r"\s+", " ", "".join(parts)).strip()


class _TableParser(HTMLParser):
    """Real `<table>` markup into a list of tables of rows of cell text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self._table: list[list[str]] | None = None
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table":
            self._depth += 1
            if self._depth == 1:
                self._table = []
        elif tag == "tr" and self._table is not None:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []
        elif tag == "br" and self._cell is not None:
            self._cell.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag == "table":
            if self._depth == 1 and self._table is not None:
                self.tables.append(self._table)
                self._table = None
            self._depth = max(0, self._depth - 1)
        elif tag == "tr" and self._row is not None:
            if self._row and self._table is not None:
                self._table.append(self._row)
            self._row = None
        elif tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(_cell_text(self._cell))
            self._cell = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)


class _GridParser(HTMLParser):
    """A table built out of divs, with rows and cells named by class.

    Nebius lays its price tables out this way. Reading them by class is still
    structural -- a cell is a cell -- so a restyle breaks here and is reported,
    rather than silently matching the wrong dollar amount.
    """

    def __init__(self, row_token: str, cell_token: str) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._row_token = row_token
        self._cell_token = cell_token
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._row_depth = 0
        self._cell_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        classes = dict(attrs).get("class") or ""
        if self._cell is not None:
            self._cell_depth += 1
            return
        if self._row is not None and self._cell_token in classes:
            self._cell = []
            self._cell_depth = 1
            return
        if self._row is not None:
            self._row_depth += 1
            return
        if self._row_token in classes:
            self._row = []
            self._row_depth = 1

    def handle_endtag(self, tag: str) -> None:
        if self._cell is not None:
            self._cell_depth -= 1
            if self._cell_depth == 0 and self._row is not None:
                self._row.append(_cell_text(self._cell))
                self._cell = None
            return
        if self._row is not None:
            self._row_depth -= 1
            if self._row_depth == 0:
                if self._row:
                    self.rows.append(self._row)
                self._row = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)


def read_tables(markup: str) -> list[list[list[str]]]:
    parser = _TableParser()
    parser.feed(_strip_noise(markup))
    return parser.tables


def read_grid(markup: str, row_token: str, cell_token: str) -> list[list[str]]:
    parser = _GridParser(row_token, cell_token)
    parser.feed(_strip_noise(markup))
    return parser.rows


def visible_text(markup: str) -> str:
    text = re.sub(r"<[^>]+>", " ", _strip_noise(markup))
    return re.sub(r"\s+", " ", text).strip()


# ------------------------------------------------------------ reading prices

_FREE = re.compile(r"^\s*free\s*\**\s*$", re.I)
_NUMBER = re.compile(r"-?[\d,]+(?:\.\d+)?")


def parse_amount(text: str) -> Decimal | None:
    """The number in a price cell, or None when the cell states no price.

    "Contact us", "문의", "-" are not prices and must not become zero: a
    machine you have to phone about is not a free machine. "Free" is a price,
    and is zero.
    """
    if text is None:
        return None
    cleaned = text.strip()
    if not cleaned:
        return None
    if _FREE.match(cleaned):
        return Decimal(0)
    match = _NUMBER.search(cleaned)
    if not match:
        return None
    try:
        value = Decimal(match.group(0).replace(",", ""))
    except InvalidOperation:
        return None
    return None if value < 0 else value


def _qualifier(text: str) -> str | None:
    """"from $1.55" is a floor, not a rate. The screen should be able to say so."""
    lowered = (text or "").lower()
    if lowered.startswith("from") or "부터" in lowered:
        return "from"
    return None


# ----------------------------------------------------------------------- AWS


def parse_aws_csv(stream: Iterator[str]) -> tuple[list[PriceRow], str]:
    reader = csv.reader(stream)
    meta: dict[str, str] = {}
    for _ in range(5):
        try:
            line = next(reader)
        except StopIteration as error:
            raise SourceError("the AWS price list ended inside its header") from error
        if line:
            meta[line[0]] = line[1] if len(line) > 1 else ""
    try:
        header = next(reader)
    except StopIteration as error:
        raise SourceError("the AWS price list has no column header") from error
    index = {name: position for position, name in enumerate(header)}

    required = (
        "TermType", "Product Family", "Instance Type", "Tenancy", "Operating System",
        "Pre Installed S/W", "License Model", "CapacityStatus", "Unit", "PricePerUnit",
        "Currency", "GPU", "vCPU", "Memory", "Volume API Name", "Storage Media",
    )
    missing = [name for name in required if name not in index]
    if missing:
        raise SourceError(f"the AWS price list is missing columns: {', '.join(missing)}")

    def field(row: Sequence[str], name: str) -> str:
        position = index[name]
        return row[position] if position < len(row) else ""

    rows: list[PriceRow] = []
    seen: set[tuple[str, str]] = set()
    for row in reader:
        if not row or field(row, "TermType") != "OnDemand":
            continue
        family = field(row, "Product Family")
        if family == "Compute Instance":
            instance = field(row, "Instance Type")
            if not instance.startswith(AWS_GPU_PREFIXES):
                continue
            if field(row, "Tenancy") != "Shared" or field(row, "Operating System") != "Linux":
                continue
            if field(row, "Pre Installed S/W") != "NA":
                continue
            if field(row, "CapacityStatus") != "Used":
                continue
            if field(row, "License Model") != "No License required":
                continue
            amount = parse_amount(field(row, "PricePerUnit"))
            if amount is None or ("gpu", instance) in seen:
                continue
            seen.add(("gpu", instance))
            rows.append(
                PriceRow(
                    provider="aws", category="gpu", sku=instance, label=instance,
                    amount=amount, currency=field(row, "Currency") or "USD",
                    unit=field(row, "Unit") or "Hrs", region=AWS_REGION,
                    spec={
                        "GPU": field(row, "GPU"),
                        "vCPU": field(row, "vCPU"),
                        "메모리": field(row, "Memory"),
                    },
                )
            )
        elif family == "Storage":
            volume = field(row, "Volume API Name")
            amount = parse_amount(field(row, "PricePerUnit"))
            if not volume or amount is None or ("storage", volume) in seen:
                continue
            seen.add(("storage", volume))
            rows.append(
                PriceRow(
                    provider="aws", category="storage", sku=f"ebs-{volume}",
                    label=f"EBS {volume}", amount=amount,
                    currency=field(row, "Currency") or "USD",
                    unit=field(row, "Unit") or "GB-Mo", region=AWS_REGION,
                    spec={"매체": field(row, "Storage Media")},
                )
            )
    return rows, meta.get("Version", "")


def _aws_s3_rows() -> list[PriceRow]:
    document = _get_json(AWS_S3_JSON, timeout=60)
    products = document.get("products") or {}
    terms = (document.get("terms") or {}).get("OnDemand") or {}
    rows: list[PriceRow] = []
    for sku, product in products.items():
        if product.get("productFamily") != "Storage":
            continue
        attributes = product.get("attributes") or {}
        usage = str(attributes.get("usagetype") or sku)
        for term in (terms.get(sku) or {}).values():
            for dimension in (term.get("priceDimensions") or {}).values():
                # Only the first tier. The volume discounts below are real, but
                # a rate card with six rows per storage class is a pricing
                # calculator, and this screen is a comparison.
                if str(dimension.get("beginRange", "0")) != "0":
                    continue
                price = (dimension.get("pricePerUnit") or {}).get("USD")
                amount = parse_amount(str(price)) if price is not None else None
                if amount is None:
                    continue
                storage_class = str(attributes.get("storageClass") or "S3")
                rows.append(
                    PriceRow(
                        provider="aws", category="storage", sku=f"s3-{usage}",
                        label=f"S3 {storage_class}", amount=amount, currency="USD",
                        unit=str(dimension.get("unit") or "GB-Mo"), region=AWS_REGION,
                        spec={"등급": storage_class},
                    )
                )
    return rows


def fetch_aws(*, known_version: str | None = None, timeout: float = 180.0) -> SourceResult:
    """AWS, from the official bulk price list.

    The EC2 file is 200MB, so it is streamed and filtered a row at a time
    rather than parsed into memory, and skipped entirely when AWS says it is
    still the edition we already hold. The version is in the file's own header,
    which costs the first few kilobytes to read.
    """
    response = _open(AWS_EC2_CSV, timeout=timeout)
    with response:
        stream = io.TextIOWrapper(response, encoding="utf-8", newline="")
        if known_version:
            # Peek at the header before committing to the rest of the download.
            # The version is on line four, so this costs a few kilobytes.
            head = [next(stream, "") for _ in range(6)]
            version = ""
            for line in head:
                if line.startswith('"Version"'):
                    version = line.split(",", 1)[-1].strip().strip('"')
            if version and version == known_version:
                # Answered, and the answer is "unchanged". `reused` is what
                # tells the refresh to keep the rows it already has rather
                # than read this as AWS having withdrawn every machine.
                return SourceResult(
                    provider="aws", rows=[], outcome="ok", reused=True,
                    detail=f"AWS 가격표 판({version})이 그대로라 내려받지 않았습니다",
                    source_url=AWS_EC2_CSV, source_version=version,
                )
            # Same wrapper, chained behind the lines already taken off it: a
            # second wrapper would start mid-row, and readlines() would pull
            # 200MB into memory to save reading it once.
            rows, version = parse_aws_csv(itertools.chain(head, stream))
        else:
            rows, version = parse_aws_csv(stream)

    if not rows:
        raise SourceError(
            "the AWS price list parsed but held no GPU instances -- "
            "the column layout or the instance families have moved"
        )
    rows.extend(_aws_s3_rows())
    return SourceResult(
        provider="aws", rows=rows, source_url=AWS_EC2_CSV, source_version=version
    )


# -------------------------------------------------------------------- Nebius


def parse_nebius(markup: str) -> list[PriceRow]:
    """The price tables on the Nebius pricing page.

    The page lays its tables out as div grids and marks the sections with a
    header row starting "Item". The on-demand GPU table is the first of those;
    the spot table and the future-dated column beside it are deliberately left
    alone, because a comparison screen that mixes on-demand and spot prices in
    one column is worse than no screen.
    """
    grid = read_grid(markup, "pc-highlight-table-block__row", "pc-highlight-table-block__cell")
    if not grid:
        raise SourceError("the Nebius price tables were not found on the page")

    sections: list[tuple[list[str], list[list[str]]]] = []
    for row in grid:
        if row and row[0].strip().lower() == "item":
            sections.append((row, []))
        elif sections:
            sections[-1][1].append(row)

    rows: list[PriceRow] = []
    for header, body in sections:
        joined = " ".join(header).lower()
        is_gpu = "gpu-hour" in joined and "spot" not in joined
        is_storage = header[1:2] == ["Price"] and "unit" in joined
        if not (is_gpu or is_storage):
            continue
        for line in body:
            if len(line) < 3:
                continue
            name = line[0]
            price_cell = line[3] if is_gpu else line[1]
            amount = parse_amount(price_cell)
            if amount is None:
                continue
            if is_gpu:
                rows.append(
                    PriceRow(
                        provider="nebius", category="gpu", sku=name, label=name,
                        amount=amount, currency="USD", unit="GPU-hour",
                        spec={
                            "vCPU": line[1], "RAM(GB)": line[2],
                            **({"기준": "from"} if _qualifier(price_cell) else {}),
                        },
                    )
                )
            else:
                rows.append(
                    PriceRow(
                        provider="nebius", category="storage", sku=name, label=name,
                        amount=amount, currency="USD", unit=line[2] or "GiB/month",
                    )
                )
    if not rows:
        raise SourceError("the Nebius page was read but no price rows came out of it")
    return rows


def fetch_nebius(*, timeout: float = 30.0) -> SourceResult:
    return SourceResult(
        provider="nebius",
        rows=parse_nebius(_get_text(NEBIUS_URL, timeout=timeout)),
        source_url=NEBIUS_URL,
    )


# --------------------------------------------------------------- Kakao Cloud


def _next_data(markup: str, url: str) -> Any:
    match = re.search(
        r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', markup, re.S
    )
    if not match:
        raise SourceError(f"{url} no longer carries a __NEXT_DATA__ block")
    try:
        return json.loads(match.group(1))
    except ValueError as error:
        raise SourceError(f"{url} carried a __NEXT_DATA__ block that is not JSON") from error


def parse_kakao(document: str) -> list[PriceRow]:
    """The rate card the Kakao Cloud calculator page ships with.

    Takes either the page itself or the `__NEXT_DATA__` JSON lifted out of it,
    so a test can pin the parser against the rate card without carrying 250KB
    of marketing page beside it.

    The calculator is a client-side app, but the whole rate card travels with
    the page as JSON, which is a better source than the rendered table: it
    carries the unit and the machine's specification as fields rather than as
    layout. A GPU machine is one Kakao gives an `accelerator` -- their marker,
    not our guess about the instance name.
    """
    text = document.lstrip()
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except ValueError as error:
            raise SourceError("the Kakao Cloud rate card is not JSON") from error
    else:
        data = _next_data(document, KAKAO_URL)
    try:
        queries = data["props"]["pageProps"]["dehydratedState"]["queries"]
        catalogue = queries[0]["state"]["data"]
    except (KeyError, IndexError, TypeError) as error:
        raise SourceError("the Kakao Cloud rate card is no longer where it was in the page") from error

    rows: list[PriceRow] = []
    seen: set[tuple[str, str]] = set()
    for group in catalogue or []:
        section = str(group.get("title") or "")
        if section not in ("Compute", "Storage"):
            continue
        for formula in group.get("formulas") or []:
            product = str(formula.get("title") or "")
            for field_spec in formula.get("inputs") or []:
                for item in field_spec.get("items") or []:
                    pricing = (item.get("productPricing") or [{}])[0]
                    metadata = pricing.get("metadata") or {}
                    accelerator = str(metadata.get("accelerator") or "").strip()
                    unit_price = pricing.get("unitPrice")
                    if unit_price is None:
                        continue
                    amount = parse_amount(str(unit_price))
                    if amount is None or amount == 0:
                        continue
                    name = str(item.get("title") or "")
                    if not name:
                        continue
                    if section == "Compute":
                        if not accelerator:
                            continue
                        category, sku = "gpu", name
                        spec = {
                            "accelerator": accelerator,
                            "vCPU": metadata.get("cpu"),
                            "메모리(GB)": metadata.get("memory"),
                        }
                        label = name
                    else:
                        category = "storage"
                        sku = f"{product}:{name}"
                        label = f"{product} {name}"
                        spec = {}
                    if (category, sku) in seen:
                        continue
                    seen.add((category, sku))
                    rows.append(
                        PriceRow(
                            provider="kakao", category=category, sku=sku, label=label,
                            amount=amount, currency="KRW",
                            unit=str(metadata.get("unit") or "시간"),
                            spec={k: v for k, v in spec.items() if v},
                        )
                    )
    if not rows:
        raise SourceError("the Kakao Cloud rate card was read but held no GPU or storage prices")
    return rows


def fetch_kakao(*, timeout: float = 30.0) -> SourceResult:
    return SourceResult(
        provider="kakao",
        rows=parse_kakao(_get_text(KAKAO_URL, timeout=timeout)),
        source_url=KAKAO_URL,
    )


# --------------------------------------------------------------- Naver Cloud

_NAVER_HEAD = ("서버스펙코드", "GPU", "vCPU")


def parse_naver(markup: str) -> list[PriceRow]:
    """The GPU tables on the Naver Cloud GPU server page.

    Only the GPU tables come back. Naver renders its storage prices into the
    page from its own calculator after load, so the storage tables arrive with
    every price cell empty -- and an empty cell is not a price. Reading them
    would mean publishing zeros for storage that actually costs money, so this
    fetcher does not try, and the screen shows Naver with no storage rows
    rather than with wrong ones.
    """
    tables = read_tables(markup)
    if not tables:
        raise SourceError("the Naver Cloud GPU page carried no tables")

    rows: list[PriceRow] = []
    seen: set[str] = set()
    for table in tables:
        header = table[0] if table else []
        if len(header) < 6 or tuple(header[:3]) != _NAVER_HEAD:
            continue
        try:
            hourly = header.index("시간 요금")
        except ValueError:
            continue
        for line in table[1:]:
            if len(line) <= hourly:
                continue
            amount = parse_amount(line[hourly])
            # A dash means "ask us", which this page uses for the models it
            # does not sell by the hour. Not a price.
            if amount is None or amount == 0:
                continue
            code = line[0]
            if not code or code in seen:
                continue
            seen.add(code)
            rows.append(
                PriceRow(
                    provider="naver", category="gpu", sku=code, label=code,
                    amount=amount, currency="KRW", unit="시간", region="KR",
                    spec={"GPU": line[1], "vCPU": line[2], "메모리": line[3]},
                )
            )
    if not rows:
        raise SourceError("the Naver Cloud GPU page was read but no hourly prices came out of it")
    return rows


def fetch_naver(*, timeout: float = 40.0) -> SourceResult:
    return SourceResult(
        provider="naver",
        rows=parse_naver(_get_text(NAVER_GPU_URL, timeout=timeout)),
        source_url=NAVER_GPU_URL,
        detail="GPU 시간 요금만. 스토리지 요금은 네이버가 계산기에서 그려 넣어 페이지에 값이 없습니다.",
    )


# ----------------------------------------------------------------- VESSL.ai

_VESSL_STORAGE = re.compile(
    r"(Warm|Cold)\s+([A-Za-z][A-Za-z ]*Storage)\s+\$([\d.]+)\s*(GiB\s*/\s*[^\s<]+)"
)


def parse_vessl(markup: str) -> list[PriceRow]:
    """The VESSL.ai pricing page: a GPU table and two storage cards."""
    rows: list[PriceRow] = []
    for table in read_tables(markup):
        header = table[0] if table else []
        if not header or "GPU 모델" not in header[0]:
            continue
        try:
            column = header.index("온디맨드")
        except ValueError:
            continue
        for line in table[1:]:
            if len(line) <= column:
                continue
            amount = parse_amount(line[column])
            if amount is None or amount == 0:
                continue
            # The model cell carries a status chip glued to the name
            # ("NVIDIA B300문의"); the price is what identifies the row.
            name = re.sub(r"(문의|바로 시작)$", "", line[0]).strip()
            rows.append(
                PriceRow(
                    provider="vessl", category="gpu", sku=name, label=name,
                    amount=amount, currency="USD", unit="hr",
                    spec={"VRAM": line[1], "아키텍처": line[2]},
                )
            )

    section = visible_text(markup)
    marker = section.find("스토리지 요금")
    for tier, name, price, unit in _VESSL_STORAGE.findall(
        section[marker:] if marker >= 0 else section
    ):
        amount = parse_amount(price)
        if amount is None:
            continue
        rows.append(
            PriceRow(
                provider="vessl", category="storage", sku=f"{tier}-{name}".strip(),
                label=f"{tier} {name}".strip(), amount=amount, currency="USD",
                unit=re.sub(r"\s+", "", unit), spec={"등급": tier},
            )
        )

    if not rows:
        raise SourceError("the VESSL.ai pricing page was read but no prices came out of it")
    return rows


def fetch_vessl(*, timeout: float = 30.0) -> SourceResult:
    return SourceResult(
        provider="vessl",
        rows=parse_vessl(_get_text(VESSL_URL, timeout=timeout)),
        source_url=VESSL_URL,
    )


# ------------------------------------------------------------ exchange rates


def fetch_fx(*, quote: str = "KRW", timeout: float = 20.0, now: datetime | None = None) -> FxRate:
    """One USD quote, with the time the quote itself is for.

    `as_of` is the rate provider's own timestamp, not ours. They are usually
    hours apart, and a screen claiming a rate is as fresh as the moment
    somebody pressed refresh would be overstating it.
    """
    document = _get_json(FX_URL, timeout=timeout)
    rates = document.get("rates") or {}
    if quote not in rates:
        raise SourceError(f"the exchange rate source did not quote {quote}")
    try:
        rate = Decimal(str(rates[quote]))
    except InvalidOperation as error:
        raise SourceError(f"the {quote} rate was not a number") from error
    stamp = document.get("time_last_update_unix")
    as_of = (
        datetime.fromtimestamp(int(stamp), tz=timezone.utc)
        if isinstance(stamp, (int, float))
        else None
    )
    return FxRate(
        base=str(document.get("base_code") or "USD"),
        quote=quote,
        rate=rate,
        as_of=as_of,
        source=str(document.get("provider") or FX_URL),
        fetched_at=now or datetime.now(timezone.utc),
    )


# ------------------------------------------------------------------ the pull


FETCHERS: dict[str, Callable[..., SourceResult]] = {
    "aws": fetch_aws,
    "nebius": fetch_nebius,
    "kakao": fetch_kakao,
    "naver": fetch_naver,
    "vessl": fetch_vessl,
}


def fetch_all(
    *,
    known_versions: Mapping[str, str] | None = None,
    only: Sequence[str] | None = None,
) -> list[SourceResult]:
    """Ask every provider, and let each one fail on its own.

    One provider's bad afternoon must not cost the other four. Each failure
    comes back as a result rather than an exception, and :func:`carry_forward`
    in :mod:`cloud_pricing` keeps that provider's last known prices on the
    screen with the reason printed beside them.
    """
    versions = dict(known_versions or {})
    results: list[SourceResult] = []
    for provider, fetcher in FETCHERS.items():
        if only and provider not in only:
            continue
        try:
            if provider == "aws":
                results.append(fetcher(known_version=versions.get("aws")))
            else:
                results.append(fetcher())
        except SourceError as error:
            results.append(SourceResult.failure(provider, str(error)))
        except Exception as error:  # a parser bug must not take the refresh down
            results.append(
                SourceResult.failure(provider, f"{type(error).__name__}: {error}")
            )
    return results
