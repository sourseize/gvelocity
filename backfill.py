#!/usr/bin/env python3
"""
backfill.py — one-off historical populator for Velocity (Modular Version)
=========================================================================

Pulls a configured ETF's historical holdings via iShares' `asOfDate` CSV
parameter for the trailing N months (default 12), stores them in that ETF's
own SQLite DB, then replays the day-by-day analysis/report pipeline so the
monthly HTML archive and JSON logs come out looking exactly as if Velocity
had been running the whole time. Also regenerates the top-level hub page.

IMPORTANT — why this isn't a bulk fetch-then-analyze:
Velocity's new-entry/exit/mover detection (detect_new_entries, detect_exits,
recent_dates, etc. in velocity.py) always looks at "the N most recent dates
in the whole snapshots table" — it does NOT filter by "on or before the date
being analyzed". That's fine for live daily cron (today IS the most recent
row), but it means analysis MUST happen immediately after each day's
snapshot is stored, in strict chronological order, before the next day's
snapshot goes in. This script does exactly that: fetch → store → analyze →
log, one day at a time, oldest first.

Usage (run from the same folder as velocity.sh / velocity.py):
  python3 backfill.py                       # IWMO, last 12 months, wipes first
  python3 backfill.py --etf QUAL            # a different configured ETF
  python3 backfill.py --months 6            # last 6 months
  python3 backfill.py --no-wipe             # keep existing rows, just fill gaps
  python3 backfill.py --dry-run             # show the date list, fetch nothing
  python3 backfill.py --db PATH --report-dir PATH

Safe to re-run: store_snapshot() already dedups on the actual trading date
parsed out of each CSV, and append_day_to_log() dedups by date too.
"""

import argparse
import datetime
import logging
import sys
import time
from pathlib import Path
from typing import Optional

import requests

# Import our modular components
sys.path.insert(0, str(Path(__file__).parent))

from src.config import ETF_CONFIG, FETCH_HEADERS, DEFAULT_REPORT_DIR
from src.database import get_db, default_db_path, etf_report_dir, store_snapshot
from src.fetcher import parse_csv, NoDataForDateError
from src.reporter import (
    get_all_months, load_month_log, generate_monthly_page,
    generate_hub_index, append_day_to_log
)
from src.analyzer import compute_metrics

import velocity  # This imports our main velocity module for additional functions

# Re-export commonly used items from velocity module
FETCH_HEADERS = velocity.FETCH_HEADERS if hasattr(velocity, 'FETCH_HEADERS') else FETCH_HEADERS
DEFAULT_REPORT_DIR = velocity.DEFAULT_REPORT_DIR if hasattr(velocity, 'DEFAULT_REPORT_DIR') else DEFAULT_REPORT_DIR

def build_day_data(conn, today: str) -> dict:
    """Build today's analysis data using modular functions."""
    from src.database import detect_new_entries, detect_exits, detect_weight_movers, recent_dates
    from src.config import NEW_ENTRY_LOOKBACK_DAYS
    
    new_tickers = detect_new_entries(conn, today, NEW_ENTRY_LOOKBACK_DAYS)
    exits = detect_exits(conn, today)
    movers = detect_weight_movers(conn, today, NEW_ENTRY_LOOKBACK_DAYS)

    top30 = [r["ticker"] for r in conn.execute(
        "SELECT ticker FROM holdings WHERE snapshot_date=? ORDER BY weight_pct DESC LIMIT 30",
        (today,)
    )]
    to_score = list(set(new_tickers + top30))
    scored = {t: compute_metrics(conn, t, today) for t in to_score}

    top20 = [dict(r) for r in conn.execute(
        """SELECT ticker,name,sector,weight_pct,price,currency
           FROM holdings WHERE snapshot_date=? ORDER BY weight_pct DESC LIMIT 20""",
        (today,)
    )]
    sectors = [dict(r) for r in conn.execute(
        """SELECT sector, COUNT(*) as count, SUM(weight_pct) as total_weight
           FROM holdings WHERE snapshot_date=?
           GROUP BY sector ORDER BY total_weight DESC""",
        (today,)
    )]

    from src.database import find_rising_stars
    rising_stars = find_rising_stars(conn, today, scored)

    return {
        "date": today,
        "new_tickers": new_tickers,
        "exits": exits,
        "movers": movers,
        "scored": scored,
        "top20": top20,
        "sectors": sectors,
        "rising_stars": rising_stars,
        "n_snapshots": len(recent_dates(conn, 9999)),
        "lookback": NEW_ENTRY_LOOKBACK_DAYS,
    }

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("backfill")


def fetch_csv_for_date(base_url: str, date_obj: datetime.date, retries: int = 3, backoff: float = 5.0):
    """Fetch+parse an ETF's holdings CSV as of a given calendar date.
    Returns (holdings_date, rows) or (None, None) on failure/no data.
    """
    url = f"{base_url}&asOfDate={date_obj:%Y%m%d}"
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, headers=FETCH_HEADERS, timeout=30)
            resp.raise_for_status()
        except requests.RequestException as e:
            log.warning("Network error attempt %d/%d for %s: %s", attempt, retries, date_obj, e)
            if attempt < retries:
                time.sleep(backoff)
                continue
            log.error("Giving up on %s after %d network-error attempts", date_obj, retries)
            return None, None

        try:
            hdate, rows = parse_csv(resp.text)
        except NoDataForDateError as e:
            # Expected for weekends/holidays/dates outside iShares' retention
            # window — not transient, so don't retry, just move on.
            log.info("No data for %s: %s", date_obj, e)
            return None, None
        except ValueError as e:
            log.warning("Parse error for %s: %s — skipping", date_obj, e)
            return None, None

        return hdate, rows

    return None, None


def business_days(start: datetime.date, end: datetime.date) -> list[datetime.date]:
    """Return list of business days (Mon-Fri) between start and end inclusive."""
    days = []
    d = start
    while d <= end:
        if d.weekday() < 5:  # Mon-Fri
            days.append(d)
        d += datetime.timedelta(days=1)
    return days


def wipe_existing(conn, fund_dir: Path) -> None:
    """Wipe existing data from database and report files."""
    log.info("Wiping existing snapshots/holdings/weekly_closes rows …")
    conn.execute("DELETE FROM holdings")
    conn.execute("DELETE FROM snapshots")
    conn.execute("DELETE FROM weekly_closes")
    conn.execute("DELETE FROM portfolio_state")
    conn.commit()

    removed = 0
    for pattern in ("????-??.json", "????-??.html"):
        for p in fund_dir.glob(pattern):
            p.unlink()
            removed += 1
    idx = fund_dir / "index.html"
    if idx.exists():
        idx.unlink()
        removed += 1
    log.info("Wiped DB rows and removed %d existing report file(s)", removed)


def regenerate_all_pages(conn, fund_dir: Path, latest_date: str, etf: str, etf_name: str) -> None:
    """Regenerate all monthly HTML pages."""
    all_months = get_all_months(fund_dir)
    if not all_months:
        log.error("No month logs were written — nothing to render")
        return

    live_ym = all_months[-1]
    latest_day_data = build_day_data(conn, latest_date)

    for idx, ym in enumerate(all_months):
        prev_ym = all_months[idx - 1] if idx > 0 else None
        next_ym = all_months[idx + 1] if idx < len(all_months) - 1 else None
        is_current = (ym == live_ym)
        entries = load_month_log(fund_dir, ym)
        html = generate_monthly_page(
            ym=ym,
            entries=entries,
            prev_ym=prev_ym,
            next_ym=next_ym,
            is_current=is_current,
            report_dir=fund_dir,
            day_data=latest_day_data if is_current else None,
            live_ym=live_ym,
            etf=etf,
            etf_name=etf_name,
        )
        filename = "index.html" if is_current else f"{ym}.html"
        (fund_dir / filename).write_text(html, encoding="utf-8")
        log.info("Wrote %s (%d day entries)", filename, len(entries))


def backfill_etf(etf: str, args, report_root: Path) -> Optional[str]:
    """Runs the full backfill for a single ETF. Returns the last stored date,
    or None if nothing was stored."""
    etf_cfg  = ETF_CONFIG[etf]
    etf_url  = etf_cfg["url"]
    etf_name = etf_cfg["name"]

    db_path  = Path(args.db) if args.db else default_db_path(etf)
    fund_dir = etf_report_dir(report_root, etf)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    fund_dir.mkdir(parents=True, exist_ok=True)

    # Backfill is a historical replay, so it must never create or mutate live
    # portfolio state. Keep paper trading disabled even if the caller reuses
    # the same args object for other flows.
    args.is_backfill = True
    args.no_paper_trade = True

    end_date = (
        datetime.datetime.strptime(args.end_date, "%Y-%m-%d").date()
        if args.end_date else datetime.date.today()
    )

    # iShares publishes holdings with a lag — requesting asOfDate for today
    # (or often yesterday) is guaranteed to hit the "-" no-data placeholder.
    # Clamp to a safe cutoff rather than wasting requests / risking a
    # mislabeled response.
    PUBLISH_LAG_DAYS = 2
    safe_end = datetime.date.today() - datetime.timedelta(days=PUBLISH_LAG_DAYS)
    if end_date > safe_end:
        log.warning(
            "Clamping end date from %s to %s — iShares publishes holdings with "
            "roughly a %d-day lag, so more recent dates will never have data yet",
            end_date, safe_end, PUBLISH_LAG_DAYS,
        )
        end_date = safe_end

    start_date = (end_date - datetime.timedelta(days=args.months * 31))
    dates = business_days(start_date, end_date)

    log.info("[%s] Backfill range: %s → %s (%d weekdays)", etf, start_date, end_date, len(dates))

    if args.dry_run:
        for d in dates:
            print(f"{etf} {d.isoformat()}")
        log.info("[%s] Dry run — no requests made", etf)
        return None

    conn = get_db(db_path)

    if not args.no_wipe:
        wipe_existing(conn, fund_dir)

    fetched, skipped = 0, 0
    last_stored_date = None

    for i, d in enumerate(dates, 1):
        hdate, rows = fetch_csv_for_date(etf_url, d)
        if not hdate:
            skipped += 1
            time.sleep(args.sleep)
            continue

        store_snapshot(conn, hdate, rows)
        today = hdate.isoformat()

        # Always analyze + log, even if this asOfDate collapsed onto a
        # trading date we already have (idempotent — append_day_to_log
        # dedups by date), UNLESS we've literally just done this same date
        # back to back (holiday collapse), in which case skip re-analysis.
        if today != last_stored_date:
            day_data = build_day_data(conn, today)
            append_day_to_log(fund_dir, day_data, commentary={})
            last_stored_date = today
            fetched += 1

        if i % 20 == 0:
            log.info("[%s] Progress: %d/%d requested dates processed", etf, i, len(dates))

        time.sleep(args.sleep)

    log.info("[%s] Fetch pass complete: %d trading days stored, %d requests skipped/failed",
              etf, fetched, skipped)

    if not last_stored_date:
        log.error("[%s] Nothing was stored — skipping report generation for this ETF", etf)
        conn.close()
        return None

    regenerate_all_pages(conn, fund_dir, last_stored_date, etf, etf_name)
    conn.close()
    log.info("[%s] Backfill done.", etf)
    return last_stored_date


def main():
    p = argparse.ArgumentParser(description="Backfill Velocity with N months of ETF history")
    p.add_argument("--etf", required=True, choices=sorted(ETF_CONFIG.keys()) + ["ALL"],
                   help="Which configured ETF to backfill, or ALL to backfill every "
                        "configured ETF in one invocation. No default — must be "
                        "specified explicitly.")
    p.add_argument("--db",          default=None,
                   help="DB path override. Only valid for a single --etf, not ALL. "
                        "Default: data/{ETF}.db next to velocity.py")
    p.add_argument("--report-dir",  default=str(DEFAULT_REPORT_DIR),
                   help="Shared report root — this ETF's subfolder is created under it")
    p.add_argument("--months",      type=int, default=12, help="How many trailing months to backfill")
    p.add_argument("--end-date",    default=None, help="YYYY-MM-DD, default today")
    p.add_argument("--sleep",       type=float, default=1.5, help="Seconds between requests")
    p.add_argument("--no-wipe",     action="store_true", help="Keep existing DB/report rows, just fill in")
    p.add_argument("--dry-run",     action="store_true", help="Print the date list and exit, no network calls")
    args = p.parse_args()

    report_root = Path(args.report_dir)
    report_root.mkdir(parents=True, exist_ok=True)

    if args.etf == "ALL":
        if args.db:
            log.error("--db can't be combined with --etf ALL (each ETF needs its own DB). "
                       "Run per-ETF instead, or drop --db to use the default data/{ETF}.db.")
            sys.exit(1)
        any_stored = False
        for etf in sorted(ETF_CONFIG.keys()):
            if backfill_etf(etf, args, report_root):
                any_stored = True
        if not any_stored:
            log.error("Nothing was stored for any ETF — aborting before hub generation")
            sys.exit(1)
    else:
        if not backfill_etf(args.etf, args, report_root):
            sys.exit(1)

    if not args.dry_run:
        hub_html = generate_hub_index(report_root)
        (report_root / "index.html").write_text(hub_html, encoding="utf-8")
        log.info("Hub index.html updated")

    log.info("Backfill done. Run velocity.sh as normal from here on for the daily cron.")


if __name__ == "__main__":
    main()