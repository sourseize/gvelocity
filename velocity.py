#!/usr/bin/env python3
"""
Velocity — Daily ETF holdings tracker + strategy session runner
===============================================================

Each run:
  1. fetches the latest holdings file of each requested ETF and stores the snapshot
  2. runs ONE strategy session (the strategy chosen with --strategy) on the latest date that
     every tracked ETF has a snapshot for, against that strategy's global position book
  3. renders the per-ETF HTML report (holdings changes + the strategy's signal state per holding)
     and the hub page

Usage:
  python velocity.py --etf ALL --paper-trade                       # default strategy
  python velocity.py --etf ALL --paper-trade --strategy range_breakout
  python velocity.py --etf ALL --paper-trade --strategy trend_follow --param breakout_len=40
  python velocity.py --etf MTUM --no-trade                         # report only
  python velocity.py --list-strategies
"""

import argparse
import logging
import sqlite3
import sys
from pathlib import Path
from typing import Dict, Optional

from src.config import (ETF_CONFIG, DEFAULT_REPORT_DIR, INITIAL_CAPITAL, MAX_PURCHASE_AMOUNT, MAX_POSITIONS,
                        RISK_PER_TRADE, COMMISSION_PER_TRADE, ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_PAPER)
from src.database import get_db, default_db_path, etf_report_dir, store_snapshot
from src.fetcher import fetch_csv, parse_csv, NoDataForDateError
from src.reporter import (month_key, generate_monthly_page, generate_hub_index, get_all_months, load_month_log,
                          append_day_to_log, rollover_if_needed)
from src.day_data import build_day_data
from src.strategies import get_strategy, available, parse_params, DEFAULT_STRATEGY, REGISTRY
from src.engine import Engine, EngineConfig, MaxPositionsPolicy
from src.book import Book

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger(__name__)


def default_book_db() -> Path:
    return Path(__file__).resolve().parent / "data" / "books.db"


def get_alpaca_client():
    if not (ALPACA_API_KEY and ALPACA_SECRET_KEY):
        log.warning("Alpaca credentials not configured — orders will not be submitted")
        return None
    from src.alpaca_client import AlpacaClient
    client = AlpacaClient(api_key=ALPACA_API_KEY, secret_key=ALPACA_SECRET_KEY, paper=ALPACA_PAPER)
    return client if client.is_available() else None


# ── step 1: holdings ────────────────────────────────────────────────────────
def fetch_etf(etf: str, args) -> Optional[str]:
    """Fetch + store today's holdings for one ETF.  Returns the holdings date (or None)."""
    db_path = Path(args.db) if args.db else default_db_path(etf)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = get_db(db_path)
    try:
        if args.mock_csv:
            raw = Path(args.mock_csv).read_text(encoding="utf-8-sig")
            hdate, rows = parse_csv(raw)
        else:
            try:
                hdate, rows = fetch_csv(ETF_CONFIG[etf]["url"], etf)
            except NoDataForDateError as e:
                log.warning("No holdings data available yet for %s (%s) — retried next cycle", etf, e)
                return None
        added = store_snapshot(conn, hdate, rows)
        log.info("[%s] holdings %s %s", etf, hdate, "stored" if added else "already stored")
        return hdate.isoformat()
    finally:
        conn.close()


# ── step 2: strategy session ────────────────────────────────────────────────
def open_all_etf_dbs(args) -> Dict[str, sqlite3.Connection]:
    conns = {}
    for etf in sorted(ETF_CONFIG):
        path = Path(args.db) if (args.db and args.etf == etf) else default_db_path(etf)
        if path.exists():
            conns[etf] = get_db(path)
    return conns


def session_date(conns: Dict[str, sqlite3.Connection]) -> Optional[str]:
    """Latest date every tracked ETF has a snapshot for."""
    latest = [c.execute("SELECT MAX(fetch_date) FROM snapshots").fetchone()[0] for c in conns.values()]
    return None if (not latest or any(d is None for d in latest)) else min(latest)


def run_strategy_session(args, conns, report_root: Path):
    """Returns (strategy, engine, book, session_result|None)."""
    strategy = get_strategy(args.strategy, **parse_params(args.param))
    book_conn = sqlite3.connect(default_book_db())
    cfg = EngineConfig(max_positions=args.max_positions, max_amount=args.max_purchase_amount,
                       risk_per_trade=args.risk_per_trade, commission=COMMISSION_PER_TRADE,
                       policy=MaxPositionsPolicy(mode=args.max_positions_policy),
                       rising_priority=args.rising_priority)
    engine = Engine(strategy, conns, cfg, book_conn=book_conn)
    if args.reset_book:
        Book.delete(book_conn, strategy.name)
        log.info("[%s] book reset", strategy.name)
    book = Book.load(book_conn, strategy.name, args.capital)
    date = session_date(conns)
    result = None
    if date is None:
        log.warning("no common snapshot date across ETFs — no session")
    elif args.no_trade:
        # still mark to market so the report shows current P&L
        for pos in book.positions.values():
            px = engine.price(date, pos.ticker)
            if px == px:
                pos.last_price = px
        log.info("[%s] --no-trade: session for %s not executed", strategy.name, date)
    else:
        result = engine.run_session(book, date, log_conn=book_conn)
        book.save(book_conn)
        if args.alpaca_trade and not result.get("skipped_session"):
            submit_to_alpaca(result, book, strategy.name)
    book.write_report(report_root / "strategy", strategy.label)
    book_conn.close()
    return strategy, engine, book, result


def submit_to_alpaca(result: dict, book: Book, strategy_name: str) -> None:
    client = get_alpaca_client()
    if client is None:
        return
    for e in result["exits"]:
        t = next((t for t in reversed(book.trades) if t.ticker == e.ticker and t.side == "sell"), None)
        if t:
            client.submit_order(strategy_name, e.ticker, t.shares, "sell")
    for s in result["entries"]:
        t = next((t for t in reversed(book.trades) if t.ticker == s.ticker and t.side == "buy"), None)
        if t:
            client.submit_order(strategy_name, s.ticker, t.shares, "buy")


# ── step 3: reports ─────────────────────────────────────────────────────────
def render_etf_report(etf: str, today: str, conn, report_root: Path, strategy, book) -> None:
    etf_name = ETF_CONFIG[etf]["name"]
    fund_dir = etf_report_dir(report_root, etf)
    fund_dir.mkdir(parents=True, exist_ok=True)
    rollover_if_needed(fund_dir, today)
    day_data = build_day_data(conn, today, etf=etf, strategy=strategy, book=book, trades=book.trades)
    log.info("[%s] %s: %d new, %d exits, %d movers, %d trades",
             etf, today, len(day_data["new_tickers"]), len(day_data["exits"]), len(day_data["movers"]), len(day_data["trades"]))
    append_day_to_log(fund_dir, day_data, {})
    today_ym = month_key(today)
    all_months = sorted(set(get_all_months(fund_dir) + [today_ym]))
    idx = all_months.index(today_ym)
    html = generate_monthly_page(
        ym=today_ym, entries=load_month_log(fund_dir, today_ym),
        prev_ym=all_months[idx - 1] if idx > 0 else None,
        next_ym=all_months[idx + 1] if idx < len(all_months) - 1 else None,
        is_current=True, report_dir=fund_dir, day_data=day_data, live_ym=today_ym, etf=etf, etf_name=etf_name)
    (fund_dir / "index.html").write_text(html, encoding="utf-8")


def main():
    p = argparse.ArgumentParser(description="Velocity — daily ETF tracker + strategy session",
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog="MANUAL TOOLS (cli/):  backfill.py · backtest.py · report_rebuild.py · ticker.py")
    p.add_argument("--etf", choices=sorted(ETF_CONFIG) + ["ALL"], help="Which ETF to fetch, or ALL")
    p.add_argument("--db", default=None, help="DB path override (single --etf only)")
    p.add_argument("--report-dir", default=str(DEFAULT_REPORT_DIR))
    p.add_argument("--mock-csv", help="Use a local CSV instead of fetching (single --etf only)")
    # strategy
    p.add_argument("--strategy", choices=available(), default=DEFAULT_STRATEGY,
                   help=f"Signal strategy for this session (default: {DEFAULT_STRATEGY})")
    p.add_argument("--param", action="append", default=[], metavar="KEY=VALUE",
                   help="Override a strategy parameter (repeatable), e.g. --param breakout_len=40")
    p.add_argument("--list-strategies", action="store_true")
    # book / risk
    p.add_argument("--capital", type=float, default=INITIAL_CAPITAL, help="Book capital when first created")
    p.add_argument("--max-purchase-amount", type=float, default=MAX_PURCHASE_AMOUNT, help="Hard $ cap per issuer")
    p.add_argument("--max-positions", type=int, default=MAX_POSITIONS)
    p.add_argument("--max-positions-policy", choices=["skip", "replace"], default="skip",
                   help="When the book is full: skip new signals, or replace the weakest losing position")
    p.add_argument("--risk-per-trade", type=float, default=RISK_PER_TRADE)
    p.add_argument("--rising-priority", action="store_true",
                   help="Give waiting rising stars priority over fresh signals for free slots (default: strategy rank)")
    # execution
    p.add_argument("--paper-trade", action="store_true", help="Execute the session in the local paper book")
    p.add_argument("--no-trade", action="store_true", help="Report only — do not execute the session")
    p.add_argument("--alpaca-trade", action="store_true", help="Also submit the session's orders to Alpaca")
    p.add_argument("--reset-book", action="store_true", help="Delete this strategy's book and start fresh")
    args = p.parse_args()

    if args.list_strategies:
        for name in available():
            cls = REGISTRY[name]
            print(f"{name:16s} {cls.label:22s} {cls.description}\n{'':16s} params: {cls.defaults}")
        return
    if not args.etf:
        p.error("--etf is required")
    if args.etf == "ALL" and (args.db or args.mock_csv):
        p.error("--db / --mock-csv need a single --etf")
    if not args.paper_trade and not args.no_trade:
        log.info("neither --paper-trade nor --no-trade given: running report-only (--no-trade)")
        args.no_trade = True

    report_root = Path(args.report_dir)
    report_root.mkdir(parents=True, exist_ok=True)
    etfs = sorted(ETF_CONFIG) if args.etf == "ALL" else [args.etf]

    # 1. holdings
    fetched: Dict[str, str] = {}
    for etf in etfs:
        d = fetch_etf(etf, args)
        if d:
            fetched[etf] = d

    # 2. strategy session (one global book; date = latest common snapshot)
    conns = open_all_etf_dbs(args)
    if not conns:
        log.error("no ETF databases — run cli/backfill.py first")
        sys.exit(1)
    strategy, engine, book, _ = run_strategy_session(args, conns, report_root)

    # 3. reports for the ETFs fetched this run (plus the session date, if it lags the holdings date,
    #    so the session's trades land on the right day block)
    sdate = book.last_date
    for etf, today in fetched.items():
        if sdate and sdate != today and any(t.date == sdate for t in book.trades):
            render_etf_report(etf, sdate, conns[etf], report_root, strategy, book)
        render_etf_report(etf, today, conns[etf], report_root, strategy, book)
    for c in conns.values():
        c.close()
    (report_root / "index.html").write_text(generate_hub_index(report_root), encoding="utf-8")
    log.info("done")


if __name__ == "__main__":
    main()
