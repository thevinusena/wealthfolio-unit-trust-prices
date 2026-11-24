#!/usr/bin/env python3
"""Fetch CeylonFunds unit-trust bid prices and publish Wealthfolio CSVs.

The script deliberately uses only requests and Python's standard library.
It reads the Next.js Flight payload embedded in each fund page, extracts
individual price-point objects, validates them, and safely merges them with
previously saved history.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import logging
import os
import re
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "funds.json"
DOCS_DIR = ROOT / "docs"
LATEST_DIR = DOCS_DIR / "latest"
CSV_FIELDS = ["symbol", "date", "close", "currency"]
BASE_URL = "https://ceylonfunds.lk/funds/{slug}"
USER_AGENT = (
    "Wealthfolio-Unit-Trust-Price-Feed/1.0 "
    "(scheduled public price collection; see repository README)"
)
REQUEST_TIMEOUT = (10, 30)
MAX_ATTEMPTS = 3
DELAY_BETWEEN_FUNDS_SECONDS = 2.0

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S UTC",
)
LOG = logging.getLogger("unit-trust-prices")


class FundUpdateError(Exception):
    """An expected, fund-specific fetch, parse, or validation failure."""


@dataclass(frozen=True)
class PriceRecord:
    symbol: str
    date: str
    close: str
    currency: str = "LKR"


@dataclass
class FundResult:
    symbol: str
    name: str
    status: str
    message: str
    latest: PriceRecord | None
    record_count: int


def load_funds() -> list[dict[str, str]]:
    if not CONFIG_PATH.exists():
        raise FundUpdateError(f"Missing configuration file: {CONFIG_PATH}")

    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FundUpdateError(f"Cannot read funds.json: {exc}") from exc

    if not isinstance(data, list) or not data:
        raise FundUpdateError("funds.json must contain a non-empty JSON array.")

    funds: list[dict[str, str]] = []
    seen_symbols: set[str] = set()
    for index, item in enumerate(data, start=1):
        if not isinstance(item, dict):
            raise FundUpdateError(f"funds.json entry {index} must be an object.")

        symbol = str(item.get("symbol", "")).strip().upper()
        slug = str(item.get("slug", "")).strip()
        name = str(item.get("name", "")).strip()

        if not symbol or not re.fullmatch(r"[A-Z0-9._-]+", symbol):
            raise FundUpdateError(f"Invalid symbol in funds.json entry {index}.")
        if symbol in seen_symbols:
            raise FundUpdateError(f"Duplicate symbol in funds.json: {symbol}")
        if not slug or "/" in slug or ".." in slug:
            raise FundUpdateError(f"Invalid page slug for {symbol}.")
        if not name:
            raise FundUpdateError(f"Missing fund name for {symbol}.")

        seen_symbols.add(symbol)
        funds.append({"symbol": symbol, "slug": slug, "name": name})

    return funds


def fetch_page(session: requests.Session, url: str) -> str:
    last_error: Exception | None = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = session.get(url, timeout=REQUEST_TIMEOUT)

            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After", "")
                try:
                    wait_seconds = max(1.0, float(retry_after))
                except ValueError:
                    wait_seconds = 2.0 ** attempt
                raise _RetryableHTTPError(
                    f"HTTP 429 rate limited; retry after {wait_seconds:g}s",
                    wait_seconds,
                )

            if response.status_code in (500, 502, 503, 504):
                raise _RetryableHTTPError(
                    f"Temporary HTTP {response.status_code}", 2.0 ** attempt
                )

            response.raise_for_status()
            if not response.text.strip():
                raise FundUpdateError("The website returned an empty page.")
            return response.text

        except _RetryableHTTPError as exc:
            last_error = exc
            if attempt == MAX_ATTEMPTS:
                break
            LOG.warning("%s; retry %s/%s", exc, attempt, MAX_ATTEMPTS)
            time.sleep(exc.wait_seconds)
        except requests.RequestException as exc:
            last_error = exc
            if attempt == MAX_ATTEMPTS:
                break
            wait_seconds = 2.0 ** attempt
            LOG.warning(
                "Network error (%s); retry %s/%s in %.1fs",
                exc,
                attempt,
                MAX_ATTEMPTS,
                wait_seconds,
            )
            time.sleep(wait_seconds)

    raise FundUpdateError(f"Could not fetch {url}: {last_error}")


class _RetryableHTTPError(Exception):
    def __init__(self, message: str, wait_seconds: float) -> None:
        super().__init__(message)
        self.wait_seconds = wait_seconds


def extract_flight_chunks(page_html: str) -> list[str]:
    """Decode string arguments passed to self.__next_f.push([n, "..."])."""
    chunks: list[str] = []
    marker = re.compile(r"self\.__next_f\.push\(\s*\[\s*\d+\s*,", re.I)
    decoder = json.JSONDecoder()

    for match in marker.finditer(page_html):
        remainder = page_html[match.end() :].lstrip()
        try:
            value, _end = decoder.raw_decode(remainder)
        except json.JSONDecodeError:
            continue

        if isinstance(value, str):
            chunks.append(value)
        elif isinstance(value, list):
            # Some Next.js builds may pass an array payload rather than a string.
            chunks.append(json.dumps(value, ensure_ascii=False))

    return chunks


def decode_payload_text(page_html: str) -> str:
    chunks = extract_flight_chunks(page_html)
    if chunks:
        text = "\n".join(chunks)
    else:
        # Fallback supports pages where the payload marker differs slightly.
        text = html.unescape(page_html)

    text = html.unescape(text)

    # A second escaping layer is occasionally present inside Flight data.
    # Only remove escaped quote markers; do not decode arbitrary JS escapes.
    for _ in range(2):
        if '"price_date"' in text:
            break
        if '\\"' in text or '\\u0022' in text:
            text = text.replace('\\"', '"').replace('\\u0022', '"')
        else:
            break

    return text


def find_matching_object_end(text: str, start: int) -> int | None:
    """Return the exclusive end of a balanced JSON object starting at start."""
    if start >= len(text) or text[start] != "{":
        return None

    depth = 0
    in_string = False
    escaped = False

    for index in range(start, len(text)):
        char = text[index]

        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index + 1

    return None


def parse_iso_date(value: Any) -> str:
    if not isinstance(value, str) or len(value) < 10:
        raise ValueError("price_date is not a valid ISO date string")
    date_part = value[:10]
    # Preserve the date as published; do not timezone-shift midnight UTC.
    parsed = date.fromisoformat(date_part)
    return parsed.isoformat()


def extract_price_records(
    page_html: str, expected_fund_name: str, symbol: str
) -> tuple[list[PriceRecord], set[str]]:
    decoded = decode_payload_text(page_html)
    visible_html = html.unescape(page_html)

    haystack = f"{decoded}\n{visible_html}".casefold()
    expected_name = expected_fund_name.strip().casefold()
    # Some CeylonFunds headings omit the generic word "Fund" even when the
    # fund is commonly referred to with that suffix (notably Ceybank Century
    # Growth). Accept that exact page-heading variant, but do not use a loose
    # token-by-token match that could approve the wrong fund.
    acceptable_names = {expected_name}
    if expected_name.endswith(" fund"):
        acceptable_names.add(expected_name[:-5].rstrip())
    if not any(name and name in haystack for name in acceptable_names):
        raise FundUpdateError(
            f"Fund-name check failed for {symbol}: expected '{expected_fund_name}' "
            "(or its exact page-title form without a trailing 'Fund')."
        )

    currency_match = re.search(
        r'"currency"\s*:\s*"([A-Z]{3})"', decoded, flags=re.IGNORECASE
    )
    source_currency = currency_match.group(1).upper() if currency_match else "LKR"
    if source_currency != "LKR":
        raise FundUpdateError(
            f"Unexpected source currency for {symbol}: {source_currency} (expected LKR)."
        )

    records_by_date: dict[str, PriceRecord] = {}
    observed_sources: set[str] = set()
    marker = re.compile(r'"price_date"\s*:')

    for text in (decoded,):
        for match in marker.finditer(text):
            object_start = text.rfind("{", 0, match.start())
            if object_start < 0:
                continue
            object_end = find_matching_object_end(text, object_start)
            if object_end is None:
                continue

            object_text = text[object_start:object_end]
            try:
                item = json.loads(object_text, parse_float=Decimal)
            except (json.JSONDecodeError, InvalidOperation):
                continue

            if not isinstance(item, dict):
                continue
            if "price_date" not in item or "buying_price" not in item:
                continue

            try:
                price_date = parse_iso_date(item["price_date"])
                price = Decimal(str(item["buying_price"]))
            except (ValueError, InvalidOperation) as exc:
                raise FundUpdateError(
                    f"Invalid price record for {symbol} near {item.get('price_date')}: {exc}"
                ) from exc

            if not price.is_finite() or price <= 0:
                raise FundUpdateError(
                    f"Invalid non-positive or non-finite price for {symbol} on {price_date}."
                )

            price_text = format(price, "f")
            record = PriceRecord(symbol, price_date, price_text, "LKR")

            existing = records_by_date.get(price_date)
            if existing and Decimal(existing.close) != price:
                raise FundUpdateError(
                    f"Conflicting source values for {symbol} on {price_date}: "
                    f"{existing.close} versus {price_text}."
                )
            records_by_date[price_date] = record

            source = item.get("source")
            if isinstance(source, str) and source.strip():
                observed_sources.add(source.strip().lower())

    if not records_by_date:
        raise FundUpdateError(
            f"No price points found for {symbol}. The page structure may have changed."
        )

    if observed_sources and observed_sources != {"utasl"}:
        LOG.warning(
            "%s has unexpected/non-UTASL point source(s): %s",
            symbol,
            ", ".join(sorted(observed_sources)),
        )

    records = [records_by_date[key] for key in sorted(records_by_date)]
    return records, observed_sources


def read_existing_csv(path: Path, expected_symbol: str) -> dict[str, PriceRecord]:
    if not path.exists():
        return {}

    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames != CSV_FIELDS:
                raise FundUpdateError(
                    f"Existing {path.name} has incorrect columns. Expected: {CSV_FIELDS}"
                )

            records: dict[str, PriceRecord] = {}
            for line_number, row in enumerate(reader, start=2):
                symbol = (row.get("symbol") or "").strip().upper()
                date_text = (row.get("date") or "").strip()
                close_text = (row.get("close") or "").strip()
                currency = (row.get("currency") or "").strip().upper()

                if symbol != expected_symbol:
                    raise FundUpdateError(
                        f"Unexpected symbol {symbol!r} in {path.name}, line {line_number}."
                    )
                try:
                    parsed_date = date.fromisoformat(date_text).isoformat()
                    price = Decimal(close_text)
                except (ValueError, InvalidOperation) as exc:
                    raise FundUpdateError(
                        f"Invalid date/price in {path.name}, line {line_number}."
                    ) from exc
                if parsed_date != date_text or not price.is_finite() or price <= 0:
                    raise FundUpdateError(
                        f"Invalid date/price in {path.name}, line {line_number}."
                    )
                if currency != "LKR":
                    raise FundUpdateError(
                        f"Unexpected currency in {path.name}, line {line_number}: {currency}"
                    )

                record = PriceRecord(symbol, date_text, close_text, currency)
                previous = records.get(date_text)
                if previous and Decimal(previous.close) != price:
                    raise FundUpdateError(
                        f"Conflicting duplicate dates in existing {path.name}: {date_text}."
                    )
                records[date_text] = record

    except OSError as exc:
        raise FundUpdateError(f"Cannot read existing CSV {path}: {exc}") from exc

    return records


def merge_history(
    symbol: str,
    incoming: list[PriceRecord],
    existing: dict[str, PriceRecord],
) -> list[PriceRecord]:
    if not incoming:
        raise FundUpdateError(f"No incoming records for {symbol}; refusing to write.")

    if existing and len(incoming) < len(existing):
        raise FundUpdateError(
            f"Incoming history for {symbol} has {len(incoming)} rows but the saved file "
            f"has {len(existing)}. This may be a shortened/partial extraction; keeping old files."
        )

    incoming_latest = max(record.date for record in incoming)
    if existing:
        existing_latest = max(existing)
        if incoming_latest < existing_latest:
            raise FundUpdateError(
                f"Incoming latest date for {symbol} ({incoming_latest}) is older than "
                f"saved latest date ({existing_latest}); keeping old files."
            )

    merged = dict(existing)
    for record in incoming:
        old = merged.get(record.date)
        if old and Decimal(old.close) != Decimal(record.close):
            LOG.warning(
                "SOURCE VALUE CHANGED: %s %s: old=%s new=%s; replacing with current source value.",
                symbol,
                record.date,
                old.close,
                record.close,
            )
        merged[record.date] = record

    return [merged[key] for key in sorted(merged)]


def csv_text(records: list[PriceRecord]) -> str:
    from io import StringIO

    output = StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=CSV_FIELDS, lineterminator="\n")
    writer.writeheader()
    for record in records:
        writer.writerow(
            {
                "symbol": record.symbol,
                "date": record.date,
                "close": record.close,
                "currency": record.currency,
            }
        )
    return output.getvalue()


def atomic_write_text(path: Path, content: str) -> bool:
    """Write only when content differs; use atomic replacement."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            if path.read_text(encoding="utf-8") == content:
                return False
        except OSError:
            pass

    temporary_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = temporary.name
        os.replace(temporary_path, path)
        return True
    except OSError:
        if temporary_path:
            try:
                os.unlink(temporary_path)
            except OSError:
                pass
        raise


def load_latest_from_csv(path: Path, symbol: str) -> PriceRecord | None:
    try:
        rows = read_existing_csv(path, symbol)
    except FundUpdateError as exc:
        LOG.error("Cannot use saved history for %s: %s", symbol, exc)
        return None
    if not rows:
        return None
    return rows[max(rows)]


def write_combined_latest(funds: list[dict[str, str]]) -> bool:
    latest_rows: list[PriceRecord] = []
    for fund in funds:
        symbol = fund["symbol"]
        historical_path = DOCS_DIR / f"{symbol}.csv"
        latest = load_latest_from_csv(historical_path, symbol)
        if latest is None:
            LOG.warning(
                "Skipping docs/latest.csv: no saved historical data exists for %s.",
                symbol,
            )
            return False
        latest_rows.append(latest)

    return atomic_write_text(DOCS_DIR / "latest.csv", csv_text(latest_rows))


def write_index(funds: list[dict[str, str]], results: list[FundResult]) -> bool:
    result_by_symbol = {result.symbol: result for result in results}
    rows: list[str] = []

    for fund in funds:
        symbol = fund["symbol"]
        name = fund["name"]
        latest = load_latest_from_csv(DOCS_DIR / f"{symbol}.csv", symbol)
        result = result_by_symbol.get(symbol)
        if latest:
            latest_price = latest.close
            latest_date = latest.date
            days_old = (date.today() - date.fromisoformat(latest_date)).days
            freshness = f"Published {days_old} day(s) ago"
        else:
            latest_price = "—"
            latest_date = "—"
            freshness = "No verified price available"

        status = result.status if result else "Not processed"
        message = result.message if result else ""
        latest_link = f"latest/{symbol}.csv"
        history_link = f"{symbol}.csv"

        rows.append(
            "<tr>"
            f"<td>{html.escape(symbol)}</td>"
            f"<td>{html.escape(name)}</td>"
            f"<td>{html.escape(latest_date)}</td>"
            f"<td>{html.escape(latest_price)}</td>"
            f"<td>{html.escape(freshness)}</td>"
            f"<td>{html.escape(status)}"
            f"{('<br><small>' + html.escape(message) + '</small>') if message else ''}</td>"
            f'<td><a href="{html.escape(history_link)}">History CSV</a> · '
            f'<a href="{html.escape(latest_link)}">Latest CSV</a></td>'
            "</tr>"
        )

    fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    document = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Wealthfolio Unit Trust Prices</title>
<style>
body{font-family:system-ui,-apple-system,Segoe UI,sans-serif;max-width:1100px;margin:2rem auto;padding:0 1rem;color:#20242a}
h1{font-size:1.7rem}p{color:#555}table{border-collapse:collapse;width:100%;font-size:.95rem}
th,td{padding:.7rem;border-bottom:1px solid #ddd;text-align:left;vertical-align:top}
th{background:#f3f5f7}small{color:#8a2d2d}
a{color:#075bc7}.note{font-size:.85rem;margin-top:1.5rem}
</style>
</head>
<body>
<h1>Wealthfolio Unit Trust Price Feed</h1>
<p>Last collection attempt: <strong>__FETCHED_AT__</strong></p>
<p>Prices are redemption/bid prices from CeylonFunds. The publication date shown below is the source's actual price date, not the collection date.</p>
<table>
<thead><tr><th>Symbol</th><th>Fund</th><th>Latest price date</th><th>Latest bid price (LKR)</th><th>Price age</th><th>Fetch status</th><th>Downloads</th></tr></thead>
<tbody>
__ROWS__
</tbody>
</table>
<p><a href="latest.csv">Combined latest.csv</a></p>
<p class="note">Source: CeylonFunds.lk, which states that daily unit prices are published by UTASL. Data is reproduced from the published source and is not independently audited by this repository. Always verify against the fund manager before making investment decisions.</p>
</body></html>
"""
    document = document.replace("__FETCHED_AT__", html.escape(fetched_at))
    document = document.replace("__ROWS__", "\n".join(rows))
    return atomic_write_text(DOCS_DIR / "index.html", document)


def process_fund(
    session: requests.Session, fund: dict[str, str]
) -> FundResult:
    symbol = fund["symbol"]
    name = fund["name"]
    url = BASE_URL.format(slug=fund["slug"])
    history_path = DOCS_DIR / f"{symbol}.csv"

    try:
        LOG.info("Fetching %s from %s", symbol, url)
        page_html = fetch_page(session, url)
        incoming, observed_sources = extract_price_records(page_html, name, symbol)
        existing = read_existing_csv(history_path, symbol)

        merged = merge_history(symbol, incoming, existing)
        history_changed = atomic_write_text(history_path, csv_text(merged))

        latest = merged[-1]
        latest_changed = atomic_write_text(
            LATEST_DIR / f"{symbol}.csv", csv_text([latest])
        )

        source_text = ", ".join(sorted(observed_sources)) if observed_sources else "not supplied"
        message = (
            f"Fetched {len(incoming)} source rows; saved {len(merged)} total rows; "
            f"point source: {source_text}."
        )
        LOG.info(
            "%s OK: %s records; latest %s = %s LKR; history_changed=%s latest_changed=%s",
            symbol,
            len(merged),
            latest.date,
            latest.close,
            history_changed,
            latest_changed,
        )
        return FundResult(symbol, name, "SUCCESS", message, latest, len(merged))

    except FundUpdateError as exc:
        LOG.error("%s FAILED: %s", symbol, exc)
        old_latest = load_latest_from_csv(history_path, symbol)
        return FundResult(symbol, name, "FAILED", str(exc), old_latest, 0)
    except (OSError, ValueError, csv.Error) as exc:
        LOG.exception("%s FAILED unexpectedly: %s", symbol, exc)
        old_latest = load_latest_from_csv(history_path, symbol)
        return FundResult(symbol, name, "FAILED", str(exc), old_latest, 0)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fetch CeylonFunds bid prices and write Wealthfolio CSV files."
    )
    parser.add_argument(
        "--no-delay",
        action="store_true",
        help="Skip the normal 2-second pause between fund requests (testing only).",
    )
    args = parser.parse_args()

    try:
        funds = load_funds()
    except FundUpdateError as exc:
        LOG.error("Configuration error: %s", exc)
        return 1

    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    LATEST_DIR.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en,en-US;q=0.9",
        }
    )

    results: list[FundResult] = []
    for index, fund in enumerate(funds):
        results.append(process_fund(session, fund))
        if index < len(funds) - 1 and not args.no_delay:
            time.sleep(DELAY_BETWEEN_FUNDS_SECONDS)

    combined_written = write_combined_latest(funds)
    if combined_written:
        LOG.info("Updated docs/latest.csv")
    else:
        LOG.warning("docs/latest.csv was not regenerated because one or more funds lack saved history.")

    index_written = write_index(funds, results)
    if index_written:
        LOG.info("Updated docs/index.html")

    session.close()

    failures = [result for result in results if result.status != "SUCCESS"]
    LOG.info("Run summary: %s succeeded, %s failed.", len(results) - len(failures), len(failures))
    for result in results:
        LOG.info(
            "%s: %s | rows=%s | latest=%s",
            result.symbol,
            result.status,
            result.record_count,
            result.latest.date if result.latest else "unavailable",
        )

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
