"""
velocity rebuild — rebuild the HTML reports from the existing databases

Regenerates all HTML pages from existing data in the SQLite database
without fetching any new data from the network. Useful after tweaking
report templates or when you need to regenerate reports from existing data.

Usage:
  velocity rebuild --etf MTUM
  velocity rebuild --etf ALL --strategy double_bottom
"""

import logging
import sqlite3
from pathlib import Path

from ..book import Book
from ..config import ETF_CONFIG
from ..database import get_db, default_db_path, etf_report_dir
from ..day_data import build_day_data
from ..engine import Engine
from ..exceptions import VelocityError
from ..reporter import (get_all_months, load_month_log, generate_monthly_page, generate_hub_index, append_day_to_log)
from ..strategies import get_strategy
from ._common import (add_etf_arg, add_strategy_args, add_report_dir_arg, check_strategy_params, check_report_dir,
                      check_db_arg, check_etf_dbs, etfs_of, open_etf_dbs, default_book_db, is_sqlite_file)

log = logging.getLogger(__name__)
HELP = "Rebuild the HTML reports from the stored databases (no network)"


def rebuild_report_pages_from_db(conn, fund_dir: Path, etf: str, etf_name: str, today: str,
                                 strategy=None, book=None) -> None:
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
        day_data = build_day_data(conn, snapshot_date, etf=etf, strategy=strategy, book=book,
                                  trades=book.trades if book else None)
        append_day_to_log(fund_dir, day_data, {})

    all_months = sorted(get_all_months(fund_dir))
    if not all_months:
        log.warning("[%s] No month logs generated", etf)
        return

    live_ym = all_months[-1]
    latest_day_data = build_day_data(conn, today, etf=etf, strategy=strategy, book=book,
                                     trades=book.trades if book else None)

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


def load_strategy_context(args, report_root: Path):
    """Prepare the chosen strategy on all ETF DBs so every rebuilt day gets its signal state."""
    conns = open_etf_dbs(require=False)
    if not conns:
        return None, None
    strategy = get_strategy(args.strategy)
    book_db = default_book_db()
    if book_db.exists() and not is_sqlite_file(book_db):
        raise VelocityError(f"book database {book_db} is not a SQLite database — restore it from backup or move it aside")
    bc = sqlite3.connect(book_db) if book_db.exists() else None
    Engine(strategy, conns, book_conn=bc)
    book = Book.load(bc, strategy.name) if bc is not None else Book(strategy.name)
    for c in conns.values():
        c.close()
    return strategy, book


def rebuild_etf(etf: str, args, report_root: Path, strategy=None, book=None) -> bool:
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

    rebuild_report_pages_from_db(conn, fund_dir, etf, etf_name, today, strategy, book)
    conn.close()
    log.info("[%s] Done.", etf)
    return True


def configure(p) -> None:
    add_etf_arg(p, required=True, help="Which configured ETF to rebuild, or ALL")
    p.add_argument("--db", default=None, help="DB path override. Default: data/{ETF}.db")
    add_report_dir_arg(p)
    add_strategy_args(p, params=False)


def validate(p, args) -> None:
    check_strategy_params(p, args)
    check_db_arg(p, args)
    check_report_dir(p, args)
    check_etf_dbs(p, etfs_of(args))


def run(args) -> int:
    report_root = Path(args.report_dir)
    strategy, book = load_strategy_context(args, report_root)
    ok = [etf for etf in etfs_of(args) if rebuild_etf(etf, args, report_root, strategy, book)]
    if not ok:
        raise VelocityError("no ETF could be rebuilt (no databases / no snapshots) — run `velocity backfill` first")
    if book is not None:
        book.write_report(report_root / "strategy", strategy.label)
        log.info("Strategy book page written: strategy/%s.html", strategy.name)
    (report_root / "index.html").write_text(generate_hub_index(report_root), encoding="utf-8")
    log.info("Hub index.html updated")
    log.info("Report rebuild complete.")
    return 0
