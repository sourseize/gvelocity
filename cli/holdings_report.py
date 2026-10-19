#!/usr/bin/env python3
"""
holdings_report — standalone: download the fund's daily holdings CSVs and keep a yearly HTML
report of what changed between consecutive days (new entrants, removals, share-count changes).

    python cli/holdings_report.py                 # fetch what is missing, update the reports
    python cli/holdings_report.py --since 2026-01-01
    python cli/holdings_report.py --rebuild       # regenerate every Index-YYYY.html from the CSVs on disk
    python cli/holdings_report.py --no-fetch      # only (re)build the reports from the CSVs already on disk
    python cli/holdings_report.py --url http://127.0.0.1:8765/holdings.csv?x=1   # e.g. cli/mock_server.py

Files
    cli/data/holdings-YYYY-MM-DD.csv     the raw CSV of each holdings date (the file's own "as of" date)
    cli/report/Index-YYYY.html           one report per calendar year, newest day on top
    cli/report/index.html                entry page linking the yearly reports (for GitHub Pages)

Fetching
    The script looks at the CSVs already in cli/data and requests every weekday after the latest one
    up to today (BlackRock answers with a "no data" placeholder for holidays and for dates it has not
    published yet — those are simply skipped).  With no data folder / no files it starts one calendar
    year back (--since / --days override that).  Every CSV is validated with the application's
    parser before it is saved.

Report
    Two consecutive CSVs (by holdings date) make one day table, prepended to the year's report:
      ticker | change     where change is  added · removed · increase +x% (shares) · reduced -x% (shares)
    ETF creations / redemptions scale EVERY holding's share count by the same ratio each day; that
    fund-wide ratio (the median across holdings) is netted out, and only names whose share count moved
    by more than --min-change (default 1 %) relative to it are listed.  Days are grouped by month
    (month header, the day tables flow in 3-4 columns on a desktop, one column on a phone).

Self-contained: only the standard library and `requests` (cli/requirements.txt); nothing here
imports the application or touches data/velocity.db.  The fund is selected by `portfolioId` in
HOLDINGS_URL below (the same endpoint the application polls).
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import html
import io
import logging
import re
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests

log = logging.getLogger("holdings_report")

HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / "data"
REPORT_DIR = HERE / "report"
FILE_RE = re.compile(r"^holdings-(\d{4}-\d{2}-\d{2})\.csv$")

# ── the tracked fund: the iShares holdings CSV endpoint; `portfolioId` selects the fund ──
HOLDINGS_URL = (
    "https://www.blackrock.com/varnish-api/blk-one01-product-data/"
    "product-data/api/v1/get-fund-document?appType=PRODUCT_PAGE"
    "&appSubType=ISHARES&targetSite=us-ishares&locale=en_US"
    "&portfolioId=307283&userType=individual&component=holdings"
)
FETCH_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
    "Referer": "https://www.blackrock.com/",
}


# ── the holdings CSV ────────────────────────────────────────────────────────
class NoDataForDate(Exception):
    """BlackRock's placeholder response (`Holdings as of,"-"`, no rows) for a date it has no data for."""


DATE_FORMATS = ("%b %d, %Y", "%d/%b/%Y", "%Y-%m-%d", "%m/%d/%Y", "%d.%m.%Y", "%d-%b-%Y")
ALIASES = {"ticker": ("ticker", "symbol"), "name": ("name", "company name", "company"), "shares": ("shares", "quantity", "shares/quantity"),
           "asset_class": ("asset class", "asset type")}


def _num(s) -> Optional[float]:
    try:
        return float(str(s).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def parse_csv(raw: str) -> Tuple[dt.date, List[Dict]]:
    """(holdings date, [{ticker, name, shares}]) from an iShares holdings CSV: a preamble with
    `Fund Holdings as of,"<date>"`, the header row (found by its Ticker column), the rows, and a
    trailing disclaimer (rows with a different column count end the table).  Non-equity rows
    (cash, derivatives) are dropped.  Raises NoDataForDate for the placeholder, ValueError when
    the layout is not recognised."""
    if raw.lstrip()[:200].lower().startswith("<"):
        raise ValueError("the server returned an HTML page instead of the holdings CSV")
    lines = raw.splitlines()
    date_str = None
    for line in lines[:25]:
        if "as of" in line.lower():
            cells = [c.strip().strip('"') for c in next(csv.reader([line]), [])]
            date_str = next((c for c in cells[1:] if c), None) or (cells[0] if cells and "as of" not in cells[0].lower() else None)
            break
    if not date_str or date_str == "-":
        raise NoDataForDate("no holdings for this date")
    hdate = None
    for fmt in DATE_FORMATS:
        try:
            hdate = dt.datetime.strptime(date_str, fmt).date()
            break
        except ValueError:
            continue
    if hdate is None:
        raise ValueError(f"unrecognised holdings date {date_str!r}")

    def col(header: List[str], key: str) -> Optional[int]:
        norm = [h.strip().strip('"').lower() for h in header]
        return next((i for a in ALIASES[key] for i, h in enumerate(norm) if h == a), None)

    header_idx = header = None
    for i, line in enumerate(lines):
        cells = next(csv.reader([line]), [])
        if cells and col(cells, "ticker") is not None:
            header_idx, header = i, cells
            break
    if header is None:
        raise ValueError("no header row with a Ticker column")
    it, iname, ish, iac = col(header, "ticker"), col(header, "name"), col(header, "shares"), col(header, "asset_class")
    if ish is None:
        raise ValueError("no Shares column")
    rows = []
    for cells in csv.reader(io.StringIO("\n".join(lines[header_idx + 1:]))):
        if len(cells) != len(header):
            break
        ticker = cells[it].strip()
        if not ticker or ticker == "-" or (iac is not None and cells[iac].strip().lower() not in ("", "equity")):
            continue
        rows.append({"ticker": ticker, "name": cells[iname].strip() if iname is not None else "", "shares": _num(cells[ish])})
    if not rows:
        raise NoDataForDate("no equity rows")
    return hdate, rows


# ── files ───────────────────────────────────────────────────────────────────
def stored_dates(data_dir: Path) -> List[str]:
    """Holdings dates of the CSVs on disk, ascending."""
    if not data_dir.is_dir():
        return []
    return sorted(m.group(1) for p in data_dir.iterdir() if (m := FILE_RE.match(p.name)))


def csv_path(data_dir: Path, date: str) -> Path:
    return data_dir / f"holdings-{date}.csv"


def weekdays(start: dt.date, end: dt.date):
    d = start
    while d <= end:
        if d.weekday() < 5:
            yield d
        d += dt.timedelta(days=1)


# ── fetching ────────────────────────────────────────────────────────────────
def fetch_raw(base_url: str, day: dt.date, retries: int = 3, backoff: float = 2.0) -> Optional[str]:
    """The raw CSV text for `day`, or None on a network failure after retries."""
    sep = "&" if "?" in base_url else "?"
    url = f"{base_url}{sep}asOfDate={day:%Y%m%d}"
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, headers=FETCH_HEADERS, timeout=30)
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as e:
            log.warning("%s: network error (attempt %d/%d): %s", day, attempt, retries, e)
            if attempt < retries:
                time.sleep(backoff)
    log.error("%s: giving up after %d attempts", day, retries)
    return None


def fetch_missing(base_url: str, data_dir: Path, since: dt.date, until: dt.date, pause: float) -> List[str]:
    """Request every weekday in [since, until] whose holdings are not on disk yet; save each valid
    CSV under its own holdings date.  Returns the dates stored."""
    data_dir.mkdir(parents=True, exist_ok=True)
    have = set(stored_dates(data_dir))
    stored: List[str] = []
    days = list(weekdays(since, until))
    log.info("fetching %d weekdays %s → %s (%d files already on disk)", len(days), since, until, len(have))
    for day in days:
        if day.isoformat() in have:
            continue
        raw = fetch_raw(base_url, day)
        if raw is None:
            continue
        try:
            hdate, rows = parse_csv(raw)
        except NoDataForDate as e:
            log.info("%s: no data (%s)", day, e)
            time.sleep(pause)
            continue
        except ValueError as e:
            log.warning("%s: could not parse the response (%s) — skipped", day, e)
            time.sleep(pause)
            continue
        key = hdate.isoformat()
        if key in have:
            log.info("%s: server returned holdings of %s, already on disk", day, key)
        else:
            csv_path(data_dir, key).write_text(raw, encoding="utf-8")
            have.add(key)
            stored.append(key)
            log.info("%s: stored holdings of %s (%d rows)", day, key, len(rows))
        time.sleep(pause)
    return stored


# ── changes between two days ────────────────────────────────────────────────
def load_rows(path: Path) -> Dict[str, dict]:
    _, rows = parse_csv(path.read_text(encoding="utf-8"))
    return {r["ticker"]: r for r in rows}


def day_changes(prev: Dict[str, dict], cur: Dict[str, dict], min_change: float) -> List[Tuple[str, str, str, str]]:
    """[(ticker, name, kind, text)] sorted: added, removed, then share changes by size.  Share changes
    are measured against the fund-wide flow ratio (median shares_today / shares_yesterday)."""
    out: List[Tuple[str, str, str, str]] = []
    for t in sorted(set(cur) - set(prev)):
        out.append((t, cur[t]["name"], "added", "added"))
    for t in sorted(set(prev) - set(cur)):
        out.append((t, prev[t]["name"], "removed", "removed"))
    ratios = []
    common = [t for t in cur if t in prev and (prev[t].get("shares") or 0) > 0 and (cur[t].get("shares") or 0) > 0]
    for t in common:
        ratios.append(cur[t]["shares"] / prev[t]["shares"])
    flow = sorted(ratios)[len(ratios) // 2] if ratios else 1.0
    moves = []
    for t in common:
        rel = cur[t]["shares"] / (prev[t]["shares"] * flow) - 1       # change net of the fund-wide flow
        if abs(rel) >= min_change:
            delta = cur[t]["shares"] - prev[t]["shares"]
            kind = "increase" if rel > 0 else "reduced"
            moves.append((abs(rel), t, cur[t]["name"], kind, f"{kind} {rel:+.1%} ({delta:+,.0f})"))
    for _, t, name, kind, text in sorted(moves, key=lambda m: -m[0]):
        out.append((t, name, kind, text))
    return out


# ── HTML ────────────────────────────────────────────────────────────────────
CSS = """
body{font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;margin:16px;color:#222;background:#fff}
h1{font-size:1.3em;margin:0 0 4px}h2{font-size:1.05em;margin:0 0 10px;padding-top:8px;border-top:1px solid #ddd}
p.note{color:#666;font-size:.85em;margin:0 0 18px}
.month{margin:0 0 28px}
.days{column-count:1;column-gap:24px}
@media(min-width:760px){.days{column-count:2}}
@media(min-width:1100px){.days{column-count:3}}
@media(min-width:1500px){.days{column-count:4}}
table{border-collapse:collapse;width:100%;margin:0 0 14px;break-inside:avoid;page-break-inside:avoid;font-size:.92em}
caption{text-align:left;font-weight:600;padding:0 0 3px}
th,td{border:1px solid #ddd;padding:2px 6px;text-align:left;vertical-align:top}
th{background:#eee}
tr:nth-child(even) td{background:#f6f6f6}
td.added{color:#0a7d32}td.removed{color:#b3261e}td.increase{color:#1a5fb4}td.reduced{color:#a05a00}
td.none{color:#888;font-style:italic}
"""
MONTHS_MARK = "<!-- months -->"


def day_table(date: str, changes: List[Tuple[str, str, str, str]]) -> str:
    rows = "".join(f'<tr><td title="{html.escape(name)}">{html.escape(t)}</td><td class="{kind}">{html.escape(text)}</td></tr>'
                   for t, name, kind, text in changes) or '<tr><td colspan="2" class="none">no changes</td></tr>'
    return (f'<table id="day-{date}"><caption>{date}</caption><tr><th>Ticker</th><th>Change</th></tr>{rows}</table>\n')


def month_section(ym: str, tables: str = "") -> str:
    label = dt.date.fromisoformat(ym + "-01").strftime("%B %Y")
    return (f'<section class="month" id="m-{ym}"><h2>{label}</h2><div class="days">\n<!-- days:{ym} -->\n{tables}</div></section>\n')


def new_index(year: int, min_change: float) -> str:
    return (f'<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>Holdings changes {year}</title><style>{CSS}</style></head><body>\n'
            f'<h1>Holdings changes {year}</h1>\n'
            f'<p class="note">Each table compares one holdings day with the previous one: new entrants, removals, and share-count '
            f'changes of more than {min_change:.0%} net of the fund-wide creation/redemption flow (the number in brackets is the raw '
            f'share delta). Newest day first.</p>\n{MONTHS_MARK}\n</body></html>\n')


def prepend_day(doc: str, date: str, table: str) -> str:
    """Insert the day table at the top of its month (creating the month section at the top when new)."""
    ym = date[:7]
    mark = f"<!-- days:{ym} -->\n"
    if mark in doc:
        return doc.replace(mark, mark + table, 1)
    return doc.replace(MONTHS_MARK + "\n", MONTHS_MARK + "\n" + month_section(ym, table), 1)


def home_page(years: List[int], latest: str) -> str:
    items = "".join(f'<li><a href="Index-{y}.html">{y}</a></li>' for y in sorted(years, reverse=True))
    return (f'<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>Holdings changes</title><style>{CSS}ul{{padding-left:18px}}li{{margin:4px 0}}</style></head><body>\n'
            f'<h1>Holdings changes</h1><p class="note">Daily changes of the fund\'s holdings, one report per year. Latest holdings day: {latest}.</p>'
            f'<ul>{items}</ul></body></html>\n')


def build_reports(data_dir: Path, report_dir: Path, min_change: float, rebuild: bool = False) -> List[str]:
    """Add every missing day table to its year's report; returns the dates added."""
    dates = stored_dates(data_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    docs: Dict[int, str] = {}
    added: List[str] = []
    cache: Dict[str, Dict[str, dict]] = {}

    def rows_of(d: str) -> Dict[str, dict]:
        if d not in cache:
            cache[d] = load_rows(csv_path(data_dir, d))
        return cache[d]

    for prev, cur in zip(dates, dates[1:]):
        year = int(cur[:4])
        if year not in docs:
            path = report_dir / f"Index-{year}.html"
            docs[year] = path.read_text(encoding="utf-8") if path.exists() and not rebuild else new_index(year, min_change)
        if f'id="day-{cur}"' in docs[year]:
            continue
        table = day_table(cur, day_changes(rows_of(prev), rows_of(cur), min_change))
        docs[year] = prepend_day(docs[year], cur, table)
        added.append(cur)
        cache.pop(prev, None)
    for year, doc in docs.items():
        (report_dir / f"Index-{year}.html").write_text(doc, encoding="utf-8")
    years = sorted({int(d[:4]) for d in dates[1:]})
    (report_dir / "index.html").write_text(home_page(years, dates[-1]), encoding="utf-8")
    return added


# ── main ────────────────────────────────────────────────────────────────────
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=HOLDINGS_URL, help="holdings CSV endpoint (asOfDate=YYYYMMDD is appended)")
    ap.add_argument("--data-dir", type=Path, default=DATA_DIR, help=f"where the CSVs live (default {DATA_DIR})")
    ap.add_argument("--report-dir", type=Path, default=REPORT_DIR, help=f"where the reports go (default {REPORT_DIR})")
    ap.add_argument("--since", type=dt.date.fromisoformat, metavar="YYYY-MM-DD",
                    help="first date to request when no CSV is on disk yet (default: one calendar year back)")
    ap.add_argument("--days", type=int, metavar="N", help="instead of --since: start N calendar days back")
    ap.add_argument("--until", type=dt.date.fromisoformat, metavar="YYYY-MM-DD", help="last date to request (default: today)")
    ap.add_argument("--pause", type=float, default=0.5, help="seconds between requests (default 0.5)")
    ap.add_argument("--min-change", type=float, default=0.01, help="share-count change (net of fund flow) worth listing (default 0.01 = 1%%)")
    ap.add_argument("--no-fetch", action="store_true", help="do not download; only update the reports from the CSVs on disk")
    ap.add_argument("--rebuild", action="store_true", help="regenerate every Index-YYYY.html from scratch")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")
    if a.since and a.days:
        ap.error("--since and --days are exclusive")

    today = dt.date.today()
    until = a.until or today
    if not a.no_fetch:
        have = stored_dates(a.data_dir)
        if have:
            since = dt.date.fromisoformat(have[-1]) + dt.timedelta(days=1)
        elif a.since:
            since = a.since
        elif a.days:
            since = today - dt.timedelta(days=a.days)
        else:
            since = today.replace(year=today.year - 1) if not (today.month == 2 and today.day == 29) else today - dt.timedelta(days=365)
        if since > until:
            log.info("nothing to fetch: latest CSV on disk is %s", have[-1] if have else "—")
        else:
            stored = fetch_missing(a.url, a.data_dir, since, until, a.pause)
            log.info("stored %d new CSV file(s)%s", len(stored), f": {stored[0]} → {stored[-1]}" if stored else "")

    dates = stored_dates(a.data_dir)
    if len(dates) < 2:
        log.info("%d CSV file(s) on disk — a report needs two consecutive days", len(dates))
        return 0
    added = build_reports(a.data_dir, a.report_dir, a.min_change, rebuild=a.rebuild)
    years = sorted({d[:4] for d in dates[1:]})
    log.info("reports: %s (%d day table(s) added%s)", ", ".join(f"Index-{y}.html" for y in years), len(added),
             f": {added[0]} → {added[-1]}" if added else "")
    return 0


if __name__ == "__main__":
    sys.exit(main())
