"""
velocity trading — the daily run: fetch holdings, run ONE strategy session, render the reports.

  1. fetches the latest holdings file of each requested ETF and stores the snapshot
     (skipped with --offline)
  2. runs ONE strategy session (--strategy) on the latest date that every tracked ETF has a
     snapshot for, against that strategy's global position book
  3. renders the per-ETF HTML report (holdings changes + the strategy's signal state per holding)
     and the hub page
"""
import logging
import sqlite3
from pathlib import Path
from typing import Dict, Optional

from ..book import Book
from ..config import (ETF_CONFIG, INITIAL_CAPITAL, MAX_PURCHASE_AMOUNT, MAX_POSITIONS, RISK_PER_TRADE,
                      COMMISSION_PER_TRADE, ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_PAPER)
from ..database import etf_report_dir
from ..day_data import build_day_data
from ..engine import Engine, EngineConfig, MaxPositionsPolicy
from ..exceptions import VelocityError
from ..reporter import (month_key, generate_monthly_page, generate_hub_index, get_all_months, load_month_log,
                        append_day_to_log, rollover_if_needed)
from ..strategies import get_strategy, parse_params
from . import fetch as fetch_cmd, reset_book as reset_cmd
from ._common import (add_etf_arg, add_strategy_args, add_report_dir_arg, positive, fraction, check_strategy_params,
                      check_report_dir, check_db_arg, check_etf_dbs, etfs_of, open_etf_dbs, open_book_db)

log = logging.getLogger(__name__)
HELP = "Daily run: fetch holdings, run a strategy session, render the reports (the main action)"


def configure(p) -> None:
    add_etf_arg(p, help="Which ETF to fetch, or ALL")
    p.add_argument("--db", default=None, help="DB path override (single --etf only)")
    add_report_dir_arg(p)
    p.add_argument("--mock-csv", help="Use a local CSV instead of fetching (single --etf only)")
    p.add_argument("--offline", action="store_true",
                   help="Do not fetch holdings: run the session / report on the stored history only")
    add_strategy_args(p)
    # book / risk
    p.add_argument("--capital", type=positive(float), default=INITIAL_CAPITAL, help="Book capital when first created")
    p.add_argument("--max-purchase-amount", type=positive(float), default=MAX_PURCHASE_AMOUNT, help="Hard $ cap per issuer")
    p.add_argument("--max-positions", type=positive(int), default=MAX_POSITIONS)
    p.add_argument("--max-positions-policy", choices=["skip", "replace"], default="skip",
                   help="When the book is full: skip new signals, or replace the weakest losing position")
    p.add_argument("--risk-per-trade", type=fraction, default=RISK_PER_TRADE, help="Fraction of equity at risk per trade (0–1]")
    p.add_argument("--rising-priority", action="store_true",
                   help="Give waiting rising stars priority over fresh signals for free slots (default: strategy rank)")
    # execution
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--paper-trade", action="store_true", help="Execute the session in the local paper book")
    mode.add_argument("--no-trade", action="store_true", help="Report only — do not execute the session (default)")
    p.add_argument("--alpaca-trade", action="store_true", help="Also submit the session's orders to Alpaca (needs --paper-trade)")
    p.add_argument("--reset-book", action="store_true",
                   help="Delete this strategy's book first (without --paper-trade this is the same as `velocity reset-book`)")


def validate(p, args) -> None:
    """Reject bad input BEFORE anything is fetched, written or deleted (usage error, exit code 2)."""
    if not args.etf:
        p.error("--etf is required (one of %s)" % ", ".join(sorted(ETF_CONFIG) + ["ALL"]))
    if args.etf == "ALL" and args.mock_csv:
        p.error("--mock-csv needs a single --etf")
    if args.offline and args.mock_csv:
        p.error("--offline and --mock-csv are exclusive (a mock CSV IS the holdings fetch)")
    if args.alpaca_trade and not args.paper_trade:
        p.error("--alpaca-trade requires --paper-trade (Alpaca orders mirror the paper book's fills)")
    if not args.paper_trade and not args.no_trade:
        if not args.reset_book:
            log.info("neither --paper-trade nor --no-trade given: running report-only (--no-trade)")
        args.no_trade = True
    check_strategy_params(p, args)
    if args.mock_csv and not Path(args.mock_csv).is_file():
        p.error(f"--mock-csv: {args.mock_csv} is not a readable file")
    check_db_arg(p, args)
    check_report_dir(p, args)
    check_etf_dbs(p, etfs_of(args))


# ── step 2: strategy session ────────────────────────────────────────────────
def session_date(conns: Dict[str, sqlite3.Connection]) -> Optional[str]:
    """Latest date every tracked ETF has a snapshot for."""
    latest = [c.execute("SELECT MAX(fetch_date) FROM snapshots").fetchone()[0] for c in conns.values()]
    return None if (not latest or any(d is None for d in latest)) else min(latest)


def run_strategy_session(args, conns, report_root: Path):
    """Returns (strategy, engine, book, session_result|None)."""
    strategy = get_strategy(args.strategy, **parse_params(args.param))
    book_conn = open_book_db()
    try:
        book = Book.load(book_conn, strategy.name, args.capital)
    except (sqlite3.Error, ValueError, TypeError) as e:
        raise VelocityError(f"book database is unusable ({e}) — restore it from backup or move it aside") from e
    cfg = EngineConfig(max_positions=args.max_positions, max_amount=args.max_purchase_amount,
                       risk_per_trade=args.risk_per_trade, commission=COMMISSION_PER_TRADE,
                       policy=MaxPositionsPolicy(mode=args.max_positions_policy),
                       rising_priority=args.rising_priority)
    engine = Engine(strategy, conns, cfg, book_conn=book_conn)
    if args.reset_book:
        Book.delete(book_conn, strategy.name)
        book = Book.load(book_conn, strategy.name, args.capital)
        log.info("[%s] book reset (capital $%s) — today's session starts it", strategy.name, f"{args.capital:,.0f}")
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


def get_alpaca_client():
    if not (ALPACA_API_KEY and ALPACA_SECRET_KEY):
        log.warning("Alpaca credentials not configured — orders will not be submitted")
        return None
    from ..alpaca_client import AlpacaClient
    client = AlpacaClient(api_key=ALPACA_API_KEY, secret_key=ALPACA_SECRET_KEY, paper=ALPACA_PAPER)
    return client if client.is_available() else None


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


def run(args) -> int:
    report_root = Path(args.report_dir)
    etfs = etfs_of(args)

    if args.reset_book and not args.paper_trade:          # pure book operation: no fetch, no session
        strategy = get_strategy(args.strategy, **parse_params(args.param))
        reset_cmd.reset(strategy.name, strategy.label, args.capital, report_root)
        return 0

    # 1. holdings (skipped with --offline)
    fetched: Dict[str, str] = {}
    if args.offline:
        log.info("--offline: holdings not fetched, using the stored history")
    else:
        fetched = fetch_cmd.fetch_all(etfs, args.db, args.mock_csv)
        if not fetched:
            log.warning("the session runs on the stored history")

    # 2. strategy session (one global book; date = latest common snapshot)
    conns = open_etf_dbs(args.db, args.etf)
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
    return 0
