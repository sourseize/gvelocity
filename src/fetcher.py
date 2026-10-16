"""
Data fetching and parsing for Velocity tracker
"""

import csv
import datetime
import io
import logging
import re
import requests
from typing import Optional, Tuple, List, Dict

from .config import FETCH_HEADERS
from .exceptions import NoDataForDateError

log = logging.getLogger(__name__)

PREAMBLE_SCAN_LINES = 25
DATE_FORMATS = ("%d/%b/%Y", "%Y-%m-%d", "%m/%d/%Y", "%d.%m.%Y", "%d-%b-%Y", "%b %d, %Y")
HEADER_ALIASES = {
    "ticker": ["ticker", "symbol"],
    "name": ["name", "company name", "company"],
    "sector": ["sector", "industry", "asset class"],
    "asset_class": ["asset class", "asset type"],
    "weight_pct": ["weight (%)", "weight %", "weight", "weight_pct"],
    "market_value": ["market value", "market_value"],
    "shares": ["shares", "quantity", "shares/quantity"],
    "price": ["price"],
    "currency": ["market currency", "currency"],
    "exchange": ["exchange"],
    "location": ["location"],
}


def _normalize_header(value: str) -> str:
    return (value or "").strip().strip('"').lower().replace("_", " ")


def _match_header(header_row: List[str], *aliases: str) -> Optional[int]:
    normalized = [_normalize_header(h) for h in header_row]
    for alias in aliases:
        alias_key = _normalize_header(alias)
        for idx, name in enumerate(normalized):
            if name == alias_key:
                return idx
    return None


def fetch_csv(url: str) -> Tuple[datetime.date, List[Dict]]:
    """
    Fetch and parse the holdings CSV from URL.

    Args:
        url: URL to fetch CSV from

    Returns:
        Tuple of (holdings_date, rows)

    Raises:
        requests.RequestException: For network errors
        NoDataForDateError: When no data available for date
        ValueError: For parsing errors
    """
    log.info("Fetching holdings CSV …")
    resp = requests.get(url, headers=FETCH_HEADERS, timeout=30)
    resp.raise_for_status()
    head = resp.text.lstrip()[:500].lower()
    if head.startswith("<") or "<html" in head:
        raise ValueError("server returned an HTML page instead of the holdings CSV "
                         "(blocked / anti-bot response, or the endpoint changed)")
    return parse_csv(resp.text)

def parse_csv(raw: str) -> Tuple[datetime.date, List[Dict]]:
    """
    Parse a BlackRock holdings CSV.

    Written to tolerate cross-fund format variation rather than assuming one fund's exact layout.
    Handles different preamble lengths, date formats, column variations, and trailing disclaimer text.

    Args:
        raw: Raw CSV text

    Returns:
        Tuple of (holdings_date, rows)

    Raises:
        NoDataForDateError: When no data available for date
        ValueError: For parsing errors
    """
    lines = raw.splitlines()
    date_str: Optional[str] = None

    # Scan for a date-bearing preamble line.
    for line in lines[:PREAMBLE_SCAN_LINES]:
        lower_line = line.lower()
        if "holdings as of" in lower_line or "as of" in lower_line:
            match = re.search(r"(\d{4}-\d{2}-\d{2}|\d{1,2}[./-][A-Za-z]{3,9}[./-]\d{4}|\d{1,2}/\d{1,2}/\d{4}|\d{1,2}\.\d{1,2}\.\d{4})", line)
            if match:
                date_str = match.group(1)
            else:
                parts = next(csv.reader([line]), [])
                for part in parts:
                    candidate = part.strip().strip('"')
                    if not candidate or candidate == "-":
                        continue
                    candidate_lower = candidate.lower()
                    if "holdings" in candidate_lower and "as of" in candidate_lower:
                        continue
                    date_str = candidate
                    break
            break

    if not date_str or date_str == "-":
        raise NoDataForDateError(
            f"Server returned no holdings data (date field was {date_str!r}) — "
            "this is BlackRock's placeholder response for a date it has no data for "
            "(weekend/holiday, outside retention window, or not yet published)"
        )

    # Try to parse date with various formats
    holdings_date = None
    for fmt in DATE_FORMATS:
        try:
            holdings_date = datetime.datetime.strptime(date_str, fmt).date()
            break
        except ValueError:
            continue

    if holdings_date is None:
        raise NoDataForDateError(
            f"Could not parse holdings date {date_str!r} against any known format "
            f"{DATE_FORMATS} — this may be a genuinely new date format rather than "
            "a no-data response; check manually before assuming it's just a "
            "weekend/holiday"
        )

    def first_field(line: str) -> str:
        return line.split(",", 1)[0].strip().strip('"')

    # Find header row by looking for a ticker-like field in a CSV row.
    header_idx = None
    for idx, line in enumerate(lines):
        if not line.strip():
            continue
        cols = [c.strip().strip('"') for c in next(csv.reader([line]), [])]
        if not cols:
            continue
        if any(_match_header(cols, *HEADER_ALIASES["ticker"]) is not None for _ in [0]):
            header_idx = idx
            break
    if header_idx is None:
        raise ValueError(
            "Ticker-like header row not found in CSV — this fund's CSV layout may "
            "differ from what this parser expects; check HOLDINGS_URL and "
            "inspect the raw response"
        )

    header_cols = [c.strip().strip('"') for c in lines[header_idx].split(",")]
    ticker_idx = _match_header(header_cols, *HEADER_ALIASES["ticker"])
    if ticker_idx is None:
        raise ValueError("Could not locate ticker column in CSV header")

    row_parser = {
        "ticker": ticker_idx,
        "name": _match_header(header_cols, *HEADER_ALIASES["name"]),
        "sector": _match_header(header_cols, *HEADER_ALIASES["sector"]),
        "asset_class": _match_header(header_cols, *HEADER_ALIASES["asset_class"]),
        "weight_pct": _match_header(header_cols, *HEADER_ALIASES["weight_pct"]),
        "market_value": _match_header(header_cols, *HEADER_ALIASES["market_value"]),
        "shares": _match_header(header_cols, *HEADER_ALIASES["shares"]),
        "price": _match_header(header_cols, *HEADER_ALIASES["price"]),
        "currency": _match_header(header_cols, *HEADER_ALIASES["currency"]),
        "exchange": _match_header(header_cols, *HEADER_ALIASES["exchange"]),
        "location": _match_header(header_cols, *HEADER_ALIASES["location"]),
    }

    if row_parser["weight_pct"] is None:
        raise ValueError("Could not locate weight column in CSV header")

    n_cols = len(header_cols)

    def to_float(s):
        try:
            return float(str(s).replace(",", "").strip())
        except (ValueError, AttributeError, TypeError):
            return None

    def clean(v) -> str:
        return (v or "").strip().strip('"')

    rows = []
    reader = csv.reader(io.StringIO("\n".join(lines[header_idx + 1:])))
    for raw_row in reader:
        # Stop when we hit trailing disclaimer text (rows with different column count)
        if len(raw_row) != n_cols:
            break

        row = dict(zip(header_cols, raw_row))

        ticker = clean(row.get(header_cols[ticker_idx])) if ticker_idx is not None else ""
        asset_class = ""
        if row_parser["asset_class"] is not None:
            asset_class = clean(row.get(header_cols[row_parser["asset_class"]])).lower()
        if not ticker or (asset_class and asset_class != "equity"):
            continue

        rows.append({
            "ticker":       ticker,
            "name":         clean(row.get(header_cols[row_parser["name"]])) if row_parser["name"] is not None else "",
            "sector":       clean(row.get(header_cols[row_parser["sector"]])) if row_parser["sector"] is not None else "",
            "weight_pct":   to_float(row.get(header_cols[row_parser["weight_pct"]])) if row_parser["weight_pct"] is not None else None,
            "market_value": to_float(row.get(header_cols[row_parser["market_value"]])) if row_parser["market_value"] is not None else None,
            "shares":       to_float(row.get(header_cols[row_parser["shares"]])) if row_parser["shares"] is not None else None,
            "price":        to_float(row.get(header_cols[row_parser["price"]])) if row_parser["price"] is not None else None,
            "currency":     clean(row.get(header_cols[row_parser["currency"]])) if row_parser["currency"] is not None else "",
            "exchange":     clean(row.get(header_cols[row_parser["exchange"]])) if row_parser["exchange"] is not None else "",
            "location":     clean(row.get(header_cols[row_parser["location"]])) if row_parser["location"] is not None else "",
        })

    if not rows:
        raise NoDataForDateError(
            f"Holdings date {holdings_date} parsed fine but zero equity rows were "
            "returned — treating as a no-data response rather than storing an empty snapshot"
        )

    log.info("Parsed %d equity rows for %s", len(rows), holdings_date)
    return holdings_date, rows
