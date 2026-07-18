#!/usr/bin/env python3
"""
report_rebuild.py — rebuild HTML reports from existing database (CLI Tool)
===========================================================================

Regenerates all HTML pages from existing data in the SQLite database
without fetching any new data from the network. Useful after tweaking
report templates or when you need to regenerate reports from existing data.

Usage (run from the velocity directory):
  python cli/report_rebuild.py --etf MTUM
  python cli/report_rebuild.py --etf ALL
"""

import argparse
import datetime
import logging
import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import ETF_CONFIG, DEFAULT_REPORT_DIR, NEW_ENTRY_LOOKBACK_DAYS
from src.database import get_db, default_db_path, etf_report_dir, recent_dates
from src.reporter import (
    get_all_months, load_month_log, generate_monthly_page,
    generate_hub_index, append_day_to_log
)
from src.analyzer import compute_metrics

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("report_rebuild")


def build_day_data(conn, today: str) -> dict:
    """Build today's analysis data using modular functions."""
    from src.database import detect_new_entries, detect_exits, detect_weight_movers, find_rising_stars

    new_tickers = detect_new_entries(conn, today, NEW_ENTRY_LOOKBACK_DAYS)
    exits = detect_exits(conn, today)
    movers = detect_weight_movers(conn, today, NEW_ENTRY_LOOKBACK_DAYS)

    current_holdings = [r["ticker"] for r in conn.execute(
        "SELECT ticker FROM holdings WHERE snapshot_date=?",
        (today,)
    )]
    to_score = list(dict.fromkeys(new_tickers + current_holdings))
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


def rebuild_report_pages_from_db(conn, fund_dir: Path, etf: str, etf_name: str, today: str) -> None:
    """Rebuild monthly log JSONs and HTML pages from historical snapshots in the DB."""
    # Clean up existing month HTML/JSON files
    for pattern in ("????-??.json", "????-??.html"):
        for path in fund_dir.glob(pattern):
            path.unlink(missing_ok=True)
    index_path = fund_dir / "index.html"
    if index_path.exists():
        index_path.unlink()

    # Get all snapshot dates from DB
    snapshot_dates = [
        row["fetch_date"]
        for row in conn.execute(
            "SELECT fetch_date FROM snapshots ORDER BY fetch_date ASC"
        )
    ]

    if not snapshot_dates:
        log.warning("[%s] No snapshots found in DB", etf)
        return

    log.info("[%s] Rebuilding %d dates from DB", etf, len(snapshot_dates))

    # Replay each date through the log
    for snapshot_date in snapshot_dates:
        day_data = build_day_data(conn, snapshot_date)
        append_day_to_log(fund_dir, day_data, {})

    all_months = sorted(get_all_months(fund_dir))
    if not all_months:
        log.warning("[%s] No month logs generated", etf)
        return

    live_ym = all_months[-1]
    latest_day_data = build_day_data(conn, today)

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
        log.info("[%s] Wrote %s (%d day entries)", etf, filename, len(entries))


def rebuild_etf(etf: str, args, report_root: Path) -> bool:
    """Rebuild reports for a single ETF. Returns True if successful."""
    etf_cfg = ETF_CONFIG[etf]
    etf_name = etf_cfg["name"]

    db_path = Path(args.db) if args.db else default_db_path(etf)
    fund_dir = etf_report_dir(report_root, etf)

    if not db_path.exists():
        log.error("[%s] Database not found at %s", etf, db_path)
        return False

    fund_dir.mkdir(parents=True, exist_ok=True)
    conn = get_db(db_path)

    # Find the latest date in DB
    latest = conn.execute(
        "SELECT fetch_date FROM snapshots ORDER BY fetch_date DESC LIMIT 1"
    ).fetchone()

    if not latest:
        log.error("[%s] No data in DB", etf)
        conn.close()
        return False

    today = latest["fetch_date"]
    log.info("[%s] Rebuilding reports from DB, latest date: %s", etf, today)

    rebuild_report_pages_from_db(conn, fund_dir, etf, etf_name, today)
    conn.close()
    log.info("[%s] Done.", etf)
    return True


def main():
    """Main entry point."""
    p = argparse.ArgumentParser(
        description="Rebuild HTML reports from existing database (MANUAL TOOL)",
        epilog="Examples:\n"
               "  python cli/report_rebuild.py --etf MTUM\n"
               "  python cli/report_rebuild.py --etf ALL\n",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--etf", required=True, choices=sorted(ETF_CONFIG.keys()) + ["ALL"],
                   help="Which configured ETF to rebuild, or ALL for all ETFs. Required.")
    p.add_argument("--db", default=None,
                   help="DB path override. Default: data/{ETF}.db")
    p.add_argument("--report-dir", default=str(DEFAULT_REPORT_DIR),
                   help="Shared report root. Default: velocity/report")
    args = p.parse_args()

    report_root = Path(args.report_dir)
    report_root.mkdir(parents=True, exist_ok=True)

    if args.etf == "ALL":
        if args.db:
            log.error("--db can't be combined with --etf ALL (each ETF needs its own DB).")
            sys.exit(1)
        for etf in sorted(ETF_CONFIG.keys()):
            rebuild_etf(etf, args, report_root)
    else:
        if not rebuild_etf(args.etf, args, report_root):
            sys.exit(1)

    # Regenerate hub
    hub_html = generate_hub_index(report_root)
    (report_root / "index.html").write_text(hub_html, encoding="utf-8")
    log.info("Hub index.html updated")

    log.info("Report rebuild complete.")


if __name__ == "__main__":
    main()
