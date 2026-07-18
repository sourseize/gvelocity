#!/usr/bin/env python3
"""
Velocity — Daily ETF Momentum/Factor Tracker
=============================================

Daily runner: fetches latest holdings, scores positions, executes paper trades,
and generates HTML reports. For manual/one-off operations, see the CLI tools
in the `cli/` directory.

Usage:
  python velocity.py --etf MTUM              # Run daily for MTUM
  python velocity.py --etf ALL               # Run daily for all configured ETFs
"""

import argparse
import datetime
import json
import logging
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional, Dict, List

# Import our modular components
from src.config import (
    ETF_CONFIG, FETCH_HEADERS,
    NEW_ENTRY_LOOKBACK_DAYS, DEFAULT_REPORT_DIR,
    SCORE_META, sc, RISING_STAR_LOOKBACK_DAYS, RISING_STAR_MIN_GROWTH,
    INITIAL_CAPITAL_PER_ETF, MAX_PURCHASE_AMOUNT, ALLOCATION_PCT
)
from src.database import (
    get_db, default_db_path, etf_report_dir,
    store_snapshot, recent_dates, daily_history,
    weekly_history, detect_new_entries, detect_exits,
    detect_weight_movers, find_rising_stars
)
from src.fetcher import fetch_csv, parse_csv, NoDataForDateError
from src.analyzer import (
    roc, max_drawdown, momentum_consistency,
    weight_percentile, weight_zscore, confirmation_score,
    compute_metrics
)
from src.reporter import (
    month_key, fmt_pct, fmt_f, ticker_link, score_badge,
    sparkline, entry_card, render_day_block, generate_monthly_page,
    generate_hub_index, get_all_months, load_month_log,
    save_month_log, append_day_to_log, rollover_if_needed,
    find_tracked_etf_dirs
)
from src.trader import (
    Portfolio, execute_paper_trade
)

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# Global dictionaries for paper trading (one per ETF)
portfolios: dict[str, Portfolio] = {}


def get_or_create_portfolio(etf: str, initial_capital: float = 100000.0) -> Portfolio:
    """Get or create a paper trading portfolio for an ETF."""
    if etf not in portfolios:
        portfolios[etf] = Portfolio(initial_capital=initial_capital)
    return portfolios[etf]


def reset_portfolio_state(etf: str, portfolio_file: Path, initial_capital: float = 100000.0, conn=None) -> Portfolio:
    """Reset the paper-trading portfolio state for an ETF."""
    portfolio = Portfolio(initial_capital=initial_capital)
    portfolios[etf] = portfolio

    for path in (portfolio_file, portfolio_file.with_suffix(".html")):
        if path.exists():
            path.unlink()

    if conn is not None:
        conn.execute("DELETE FROM portfolio_state WHERE etf=?", (etf,))
        conn.commit()

    log.info(f"[{etf}] Reset portfolio state")
    return portfolio


def load_portfolio_state(etf: str, portfolio: Portfolio, portfolio_file: Path, conn=None) -> Portfolio:
    """Load an existing portfolio state from disk or SQLite when available."""
    if not portfolio_file.exists() and conn is None:
        portfolios[etf] = portfolio
        return portfolio

    try:
        portfolio = Portfolio.load_state(portfolio_file, conn=conn, etf=etf)
        portfolios[etf] = portfolio
        log.info(f"[{etf}] Loaded existing portfolio state")
    except Exception as e:
        log.warning(f"[{etf}] Could not load portfolio state: {e}")

    return portfolio


def should_execute_paper_trades(args) -> bool:
    """Return whether paper trading should run for this invocation."""
    return False


def run_etf(etf: str, args, report_root: Path) -> None:
    """Runs the full fetch/analyze/report/paper trade pipeline for a single ETF."""
    etf_cfg = ETF_CONFIG[etf]
    etf_url = etf_cfg["url"]
    etf_name = etf_cfg["name"]

    db_path = Path(args.db) if args.db else default_db_path(etf)
    fund_dir = etf_report_dir(report_root, etf)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    fund_dir.mkdir(parents=True, exist_ok=True)

    conn = get_db(db_path)
    portfolio = get_or_create_portfolio(etf, args.initial_capital)

    # Load existing portfolio state
    portfolio_file = fund_dir / "portfolio.json"
    if getattr(args, "reset_portfolio", False):
        portfolio = reset_portfolio_state(etf, portfolio_file, args.initial_capital, conn=conn)
    else:
        portfolio = load_portfolio_state(etf, portfolio, portfolio_file, conn=conn)

    # Fetch or load data
    if args.mock_csv:
        raw = Path(args.mock_csv).read_text(encoding="utf-8-sig")
        hdate, rows = parse_csv(raw)
    else:
        try:
            hdate, rows = fetch_csv(etf_url, etf)
        except NoDataForDateError as e:
            log.warning(
                "No holdings data available yet for %s (%s). BlackRock publishes "
                "with a lag — this run will simply be retried next cron cycle.",
                etf, e
            )
            conn.close()
            return
    store_snapshot(conn, hdate, rows)
    today = hdate.isoformat()

    today_ym = month_key(today)

    # Rollover check (seal previous month if we've just crossed into new month)
    rollover_if_needed(fund_dir, today)

    # Build today's analysis
    day_data = build_day_data(conn, today)
    log.info(
        "[%s] Analysis: %d new, %d exits, %d movers",
        etf, len(day_data["new_tickers"]), len(day_data["exits"]), len(day_data["movers"])
    )

    # Paper trading: Execute trades based on signals
    should_do_paper_trade = should_execute_paper_trades(args)
    if should_do_paper_trade:
        execute_paper_trades_for_etf(conn, today, day_data, portfolio, etf, args)
        # Save portfolio state after trading
        portfolio_file = fund_dir / "portfolio.json"
        portfolio.save_state(portfolio_file, conn=conn, etf=etf)
        log.info(f"[{etf}] Portfolio value: ${portfolio.total_value():.2f} (Return: {portfolio.total_return():.2f}%)")

    # Append to monthly log
    append_day_to_log(fund_dir, day_data, {})

    # Determine prev/next months for nav links
    all_months = sorted(set(get_all_months(fund_dir) + [today_ym]))
    idx = all_months.index(today_ym)
    prev_ym = all_months[idx - 1] if idx > 0 else None
    next_ym = all_months[idx + 1] if idx < len(all_months) - 1 else None

    # Generate index.html (current month)
    current_entries = load_month_log(fund_dir, today_ym)
    html = generate_monthly_page(
        ym=today_ym,
        entries=current_entries,
        prev_ym=prev_ym,
        next_ym=next_ym,
        is_current=True,
        report_dir=fund_dir,
        day_data=day_data,
        live_ym=today_ym,
        etf=etf,
        etf_name=etf_name,
    )
    (fund_dir / "index.html").write_text(html, encoding="utf-8")
    log.info("[%s] index.html updated (%d day entries for %s)", etf, len(current_entries), today_ym)

    # Save portfolio state
    portfolio.save_state(portfolio_file, conn=conn, etf=etf)

    conn.close()
    log.info("[%s] Done.", etf)


def execute_paper_trades_for_etf(conn, today: str, day_data: dict, portfolio: Portfolio, etf: str, args):
    """Execute paper trades based on today's analysis."""
    # Get current prices for all tickers in scored holdings
    scored = day_data.get("scored", {})
    price_data = {}

    for ticker, metrics in scored.items():
        price = metrics.get("current_price")
        if price is not None:
            price_data[ticker] = price

    # Update portfolio prices
    portfolio.update_prices(price_data, today)

    # Check stop losses first
    trail_percent = getattr(args, 'trail_percent', 0.15)
    stopped_out = portfolio.check_stop_losses(today, trail_percent)
    if stopped_out:
        log.info("[%s] Stop loss triggered for: %s", etf, ", ".join(stopped_out))

    # Process new entries and scored holdings for potential trades
    current_tickers = {row["ticker"] for row in conn.execute(
        "SELECT ticker FROM holdings WHERE snapshot_date=?",
        (today,)
    )}
    tickers_to_consider = set(day_data.get("new_tickers", [])) | set(scored.keys()) | set(current_tickers)

    # Sell any open position that is no longer present in the current snapshot.
    for ticker in list(portfolio.positions):
        if ticker not in current_tickers:
            trade = portfolio.sell(ticker, price_data.get(ticker, portfolio.positions[ticker].current_price), today, reason="exit")
            if trade:
                log.info("[%s] Exit signal for %s: SELL @ $%.2f", etf, ticker, trade.price)

    # Get trading parameters from args
    allocation_pct = getattr(args, 'allocation_pct', 0.1)
    max_purchase_amount = getattr(args, 'max_purchase_amount', None)

    for ticker in tickers_to_consider:
        if ticker not in scored:
            continue

        metrics = scored[ticker]
        score = metrics.get("confirmation_score", 0)
        price = metrics.get("current_price")
        name = metrics.get("name", "")
        sector = metrics.get("sector", "")

        if price is None:
            continue

        # Execute trade based on signal
        trade = execute_paper_trade(
            ticker=ticker,
            score=score,
            price=price,
            date=today,
            portfolio=portfolio,
            name=name,
            sector=sector,
            allocation_pct=allocation_pct,
            max_purchase_amount=max_purchase_amount
        )

        if trade:
            action = "BUY" if trade.type.value == "buy" else "SELL"
            log.info("[%s] %s signal for %s: %s @ $%.2f (Score: %d)",
                    etf, action, ticker, action, price, score)


def build_day_data(conn, today: str) -> dict:
    """Build today's analysis data using modular functions."""
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


def main():
    """Main entry point."""
    p = argparse.ArgumentParser(
        description="Velocity — Daily ETF tracker (fetch, score, trade, report)",
        epilog="MANUAL TOOLS (see cli/ directory):\n"
               "  Backfill history:    python cli/backfill.py --etf MTUM --months 12\n"
               "  Rebuild reports:     python cli/report_rebuild.py --etf MTUM\n"
               "  Ticker history:      python cli/ticker.py NVDA --etf MTUM\n",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--etf", required=True, choices=sorted(ETF_CONFIG.keys()) + ["ALL"],
                   help="Which configured ETF to fetch, or ALL to run every "
                        "configured ETF in one invocation. Required.")
    p.add_argument("--db", default=None,
                   help="DB path override. Only valid for a single --etf, not ALL. "
                        "Default: data/{ETF}.db next to this script")
    p.add_argument("--report-dir", default=str(DEFAULT_REPORT_DIR),
                   help="Shared report root — each ETF gets its own subfolder here")
    p.add_argument("--mock-csv", help="Use local CSV file instead of fetching (for testing)")
    p.add_argument("--initial-capital", type=float, default=INITIAL_CAPITAL_PER_ETF,
                   help=f"Initial capital for paper trading (default: ${INITIAL_CAPITAL_PER_ETF:,.0f})")
    p.add_argument("--allocation-pct", type=float, default=ALLOCATION_PCT,
                   help=f"Percentage of portfolio to allocate per trade (default: {ALLOCATION_PCT*100:.0f}%%)")
    p.add_argument("--max-purchase-amount", type=float, default=MAX_PURCHASE_AMOUNT,
                   help=f"Maximum dollar amount to spend on a single ticker (default: ${MAX_PURCHASE_AMOUNT:,.0f})")
    p.add_argument("--trail-percent", type=float, default=0.15,
                   help="Trailing stop loss percentage (default: 15%%)")
    p.add_argument("--no-paper-trade", action="store_true",
                   help="Disable paper trading functionality")
    p.add_argument("--reset-portfolio", action="store_true",
                   help="Delete the saved portfolio state and start fresh from this run onward")
    args = p.parse_args()

    report_root = Path(args.report_dir)
    report_root.mkdir(parents=True, exist_ok=True)

    if args.etf == "ALL":
        if args.db:
            log.error("--db can't be combined with --etf ALL (each ETF needs its own DB). "
                       "Run per-ETF instead, or drop --db to use the default data/{ETF}.db.")
            sys.exit(1)
        if args.mock_csv:
            log.error("--mock-csv can't be combined with --etf ALL. Run a single --etf instead.")
            sys.exit(1)
        for etf in sorted(ETF_CONFIG.keys()):
            run_etf(etf, args, report_root)
    else:
        run_etf(args.etf, args, report_root)

    # Regenerate the top-level hub page
    hub_html = generate_hub_index(report_root)
    (report_root / "index.html").write_text(hub_html, encoding="utf-8")
    log.info("Hub index.html updated")


if __name__ == "__main__":
    main()
