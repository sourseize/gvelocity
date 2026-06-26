"""
Data fetching and parsing for Velocity tracker
"""

import csv
import datetime
import io
import logging
import requests
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional, Tuple, List, Dict

from .config import (
    FETCH_HEADERS, GROQ_ENDPOINT, GROQ_MODEL,
    NEW_ENTRY_LOOKBACK_DAYS, DEFAULT_REPORT_DIR
)
from .exceptions import NoDataForDateError

log = logging.getLogger(__name__)

PREAMBLE_SCAN_LINES = 15
DATE_FORMATS = ("%d/%b/%Y", "%Y-%m-%d", "%m/%d/%Y", "%d.%m.%Y", "%d-%b-%Y", "%b %d, %Y")

def fetch_csv(url: str, etf: str = "") -> Tuple[datetime.date, List[Dict]]:
    """
    Fetch and parse CSV from URL.

    Args:
        url: URL to fetch CSV from
        etf: ETF ticker for logging

    Returns:
        Tuple of (holdings_date, rows)

    Raises:
        requests.RequestException: For network errors
        NoDataForDateError: When no data available for date
        ValueError: For parsing errors
    """
    log.info("Fetching %s CSV …", etf or "ETF")
    resp = requests.get(url, headers=FETCH_HEADERS, timeout=30)
    resp.raise_for_status()
    return parse_csv(resp.text)

def parse_csv(raw: str) -> Tuple[datetime.date, List[Dict]]:
    """
    Parse an iShares holdings CSV.

    Written to tolerate cross-fund format variation rather than assuming IWMO's exact layout.
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

    # Scan for "Holdings as of" line
    for line in lines[:PREAMBLE_SCAN_LINES]:
        if "Holdings as of" in line:
            parts = next(csv.reader([line]), [])
            if len(parts) >= 2:
                date_str = parts[1].strip()
            break

    if not date_str or date_str == "-":
        raise NoDataForDateError(
            f"Server returned no holdings data (date field was {date_str!r}) — "
            "this is iShares' placeholder response for a date it has no data for "
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

    # Find header row
    header_idx = next((i for i, l in enumerate(lines) if first_field(l) == "Ticker"), None)
    if header_idx is None:
        raise ValueError(
            "Ticker header row not found in CSV — this fund's CSV layout may "
            "differ from what this parser expects; check ETF_CONFIG's url and "
            "inspect the raw response"
        )

    header_cols = [c.strip().strip('"') for c in lines[header_idx].split(",")]
    required = {"Ticker", "Asset Class", "Weight (%)"}
    missing = required - set(header_cols)
    if missing:
        raise ValueError(
            f"CSV header is missing expected column(s) {sorted(missing)} — "
            f"got columns: {header_cols}. This fund's schema doesn't match "
            "what parse_csv expects; it needs updating for this fund."
        )

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

        ticker = clean(row.get("Ticker"))
        asset_class = clean(row.get("Asset Class")).lower()
        if not ticker or asset_class != "equity":
            continue

        rows.append({
            "ticker":       ticker,
            "name":         clean(row.get("Name")),
            "sector":       clean(row.get("Sector")),
            "weight_pct":   to_float(row.get("Weight (%)")),
            "market_value": to_float(row.get("Market Value")),
            "shares":       to_float(row.get("Shares")),
            "price":        to_float(row.get("Price")),
            "currency":     clean(row.get("Market Currency")),
            "exchange":     clean(row.get("Exchange")),
            "location":     clean(row.get("Location")),
        })

    if not rows:
        raise NoDataForDateError(
            f"Holdings date {holdings_date} parsed fine but zero equity rows were "
            "returned — treating as a no-data response rather than storing an empty snapshot"
        )

    log.info("Parsed %d equity rows for %s", len(rows), holdings_date)
    return holdings_date, rows