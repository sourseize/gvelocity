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
    INITIAL_CAPITAL_PER_ETF, MAX_PURCHASE_AMOUNT, ALLOCATION_PCT,
    ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_PAPER
)
from src.database import (
    get_db, default_db_path, etf_report_dir,
    store_snapshot, recent_dates, daily_history,
    weekly_history, detect_new_entries, detect_exits,
    detect_weight_movers, find_rising_stars, load_trading_state,
    save_trading_state
)
from src.fetcher import fetch_csv, parse_csv, NoDataForDateError
from src.analyzer import (
    roc, max_drawdown, momentum_consistency,
    weight_percentile, weight_zscore, confirmation_score,
    compute_metrics, compute_score_transitions, format_buy_reason, BUY_THRESHOLD
)
from src.reporter import (
    month_key, fmt_pct, fmt_f, ticker_link, score_badge,
    sparkline, entry_card, render_day_block, generate_monthly_page,
    generate_hub_index, get_all_months, load_month_log,
    save_month_log, append_day_to_log, rollover_if_needed,
    find_tracked_etf_dirs
)
from src.trader import (
    Portfolio
)
from src.alpaca_client import AlpacaClient

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# Global dictionaries for paper trading (one per ETF)
portfolios: dict[str, Portfolio] = {}


# Global Alpaca client instance
_alpaca_client = None

def get_alpaca_client() -> Optional[AlpacaClient]:
    """Get or create the Alpaca client (lazy initialization)."""
    global _alpaca_client
    if _alpaca_client is None and ALPACA_API_KEY and ALPACA_SECRET_KEY:
        _alpaca_client = AlpacaClient(
            api_key=ALPACA_API_KEY,
            secret_key=ALPACA_SECRET_KEY,
            paper=ALPACA_PAPER
        )
    return _alpaca_client

def get_or_create_portfolio(etf: str, initial_capital: float = 100000.0, alpaca_client=None) -> Portfolio:
    """Get or create a paper trading portfolio for an ETF."""
    if etf not in portfolios:
        portfolios[etf] = Portfolio(
            initial_capital=initial_capital,
            alpaca_client=alpaca_client,
            etf=etf
        )
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
    """Return whether paper trading should run for this invocation.

    Paper trading is opt-in: pass --paper-trade to enable it.
    Omitting the flag (e.g. during manual inspection or testing) is always safe.
    """
    return getattr(args, "paper_trade", False)


def evaluate_and_persist_scores(
    conn,
    etf: str,
    today: str,
    day_data: dict,
    snapshot_added: bool,
) -> dict:
    """Compute today's scores, diff against prior state, log signal transitions.

    No trades are executed here — that gate stays off while paper trading is
    disabled.  The function still computes and logs every threshold crossing so
    the transition logic can be validated in the logs before buy/sell is
    re-enabled.

    Returns a dict with keys:
        prior_state   : the loaded prior trading state (or None on first run)
        current_scores: {ticker: score} for today
        transitions   : output of compute_score_transitions()
        persisted     : True if state was saved, False if skipped
    """
    prior_state = load_trading_state(conn, etf)

    # Skip evaluation if the snapshot hasn't changed and we already have state.
    # This makes re-running the same date idempotent.
    if prior_state and prior_state["last_evaluated_snapshot"] == today and not snapshot_added:
        log.info(
            "[%s] Score state already up to date for %s; skipping evaluation",
            etf, today,
        )
        return {
            "prior_state": prior_state,
            "current_scores": prior_state["scores"],
            "transitions": None,
            "persisted": False,
        }

    # Build current score map + per-ticker signal-bit map from today's scored holdings.
    scored = day_data.get("scored", {})
    current_scores: dict[str, int] = {
        ticker: metrics["confirmation_score"]
        for ticker, metrics in scored.items()
        if metrics.get("confirmation_score") is not None
    }
    current_signals: dict[str, list[bool]] = {
        ticker: metrics.get("confirmation_signal_bits", [])
        for ticker, metrics in scored.items()
        if metrics.get("confirmation_score") is not None
    }

    prior_scores: dict[str, int] = prior_state["scores"] if prior_state else {}
    prior_signals: dict[str, list[bool]] = prior_state["signals"] if prior_state else {}
    transitions = compute_score_transitions(
        prior_scores, current_scores,
        threshold=BUY_THRESHOLD,
        prior_signals=prior_signals if prior_state else None,
        current_signals=current_signals,
    )

    # Log transitions (informational — no trades yet).
    buy_sigs  = transitions["buy_signals"]
    sell_sigs = transitions["sell_signals"]

    if prior_state is None:
        log.info(
            "[%s] Bootstrap: first score state for %s — %d tickers scored, no trades (buy/sell disabled)",
            etf, today, len(current_scores),
        )
    else:
        log.info(
            "[%s] Score evaluation for %s: %d tickers | %d BUY signals | %d SELL signals (buy/sell disabled)",
            etf, today, len(current_scores), len(buy_sigs), len(sell_sigs),
        )
        for sig in buy_sigs:
            log.info(
                "[%s]   WOULD BUY  %s: score %d → %d (crossed ≥%d)",
                etf, sig["ticker"], sig["prior"], sig["current"], BUY_THRESHOLD,
            )
        for sig in sell_sigs:
            log.info(
                "[%s]   WOULD SELL %s: score %d → %d (fell below %d)",
                etf, sig["ticker"], sig["prior"], sig["current"], BUY_THRESHOLD,
            )

    # Persist the new score + signal-bit map atomically.
    save_trading_state(conn, etf, today, current_scores, signals=current_signals)
    log.info(
        "[%s] Trading state persisted for %s (%d scores)",
        etf, today, len(current_scores),
    )

    return {
        "prior_state": prior_state,
        "current_scores": current_scores,
        "transitions": transitions,
        "persisted": True,
    }


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

    # Initialize Alpaca client if --alpaca-trade is enabled
    alpaca_client = None
    if getattr(args, "alpaca_trade", False):
        alpaca_client = get_alpaca_client()
        if alpaca_client and alpaca_client.is_available():
            log.info(f"[{etf}] Alpaca trading enabled (paper={ALPACA_PAPER})")
        else:
            log.warning(f"[{etf}] Alpaca client not available, check API credentials")

    portfolio = get_or_create_portfolio(etf, args.initial_capital, alpaca_client=alpaca_client)

    # Load existing portfolio state
    portfolio_file = fund_dir / "portfolio.json"
    if getattr(args, "reset_portfolio", False):
        portfolio = reset_portfolio_state(etf, portfolio_file, args.initial_capital, conn=conn)
    else:
        portfolio = load_portfolio_state(etf, portfolio, portfolio_file, conn=conn)

    # Sync Alpaca positions if requested
    if getattr(args, "sync_alpaca_positions", False) and alpaca_client and alpaca_client.is_available():
        synced = portfolio.sync_alpaca_positions(etf)
        log.info(f"[{etf}] Synced {synced} positions from Alpaca")

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
    snapshot_added = store_snapshot(conn, hdate, rows)
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

    # Score evaluation always runs — builds the transition map and persists state.
    eval_result = evaluate_and_persist_scores(conn, etf, today, day_data, snapshot_added)

    # Paper trading: only execute when enabled AND the snapshot is genuinely new.
    should_do_paper_trade = should_execute_paper_trades(args)
    if should_do_paper_trade and snapshot_added:
        execute_paper_trades_for_etf(
            conn, today, day_data, portfolio, etf, args,
            transitions=eval_result["transitions"],
        )
        portfolio_file = fund_dir / "portfolio.json"
        portfolio.save_state(portfolio_file, conn=conn, etf=etf)
        log.info(
            "[%s] Portfolio value: $%.2f (Return: %.2f%%)",
            etf, portfolio.total_value(), portfolio.total_return(),
        )
    elif should_do_paper_trade:
        log.info("[%s] Snapshot %s already processed; skipping paper-trade execution", etf, today)

    # Surface today's executed trades in the day data so the monthly log can
    # show a per-day "Trades executed" block. Sourced from the durable
    # portfolio trade_history (loaded from DB at the start of every run) so a
    # no-op re-run of the same snapshot doesn't erase previously-logged trades
    # — append_day_to_log dedups by date and would otherwise write an empty
    # trades list over the real one if we relied on execute() to populate it.
    day_data["trades"] = [
        t.to_dict() for t in portfolio.trade_history if t.date == today
    ]

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


def execute_paper_trades_for_etf(
    conn,
    today: str,
    day_data: dict,
    portfolio: Portfolio,
    etf: str,
    args,
    transitions: Optional[dict] = None,
) -> None:
    """Execute paper trades driven by score transitions.

    Only threshold-crossing events from `transitions` trigger buy/sell orders:
      - buy_signals  : score crossed up through BUY_THRESHOLD → open a position
      - sell_signals : score fell below BUY_THRESHOLD          → close a position

    Exit logic (ticker dropped from ETF) and trailing stop-losses run
    independently of the score-transition system.

    Args:
        conn:        Database connection (used for exit detection).
        today:       ISO date string for this snapshot.
        day_data:    Output of build_day_data().
        portfolio:   Active paper-trading portfolio.
        etf:         ETF key (for logging).
        args:        Parsed CLI args (allocation_pct, max_purchase_amount, trail_percent).
        transitions: Output of compute_score_transitions().  If None (e.g. first
                     bootstrap run) no buy/sell signals are processed.
    """
    scored = day_data.get("scored", {})

    # Build price map from scored holdings.
    price_data: dict[str, float] = {
        ticker: metrics["current_price"]
        for ticker, metrics in scored.items()
        if metrics.get("current_price") is not None
    }

    # Refresh portfolio mark-to-market before any decisions.
    portfolio.update_prices(price_data, today)

    # 1. Trailing stop-losses — independent of score transitions.
    trail_percent = getattr(args, "trail_percent", 0.15)
    stopped_out = portfolio.check_stop_losses(today, trail_percent)
    if stopped_out:
        log.info("[%s] Stop loss triggered for: %s", etf, ", ".join(stopped_out))

    # 2. Exit any position whose ticker has left the ETF entirely.
    current_tickers: set[str] = {
        row["ticker"]
        for row in conn.execute(
            "SELECT ticker FROM holdings WHERE snapshot_date=?", (today,)
        )
    }
    for ticker in list(portfolio.positions):
        if ticker not in current_tickers:
            exit_price = price_data.get(ticker, portfolio.positions[ticker].current_price)
            trade = portfolio.sell(ticker, exit_price, today, reason="exit")
            if trade:
                log.info("[%s] ETF-exit for %s: SELL @ $%.2f", etf, ticker, trade.price)

    # 3. Score-transition buys and sells.
    if transitions is None:
        log.info("[%s] No transitions available (bootstrap run); skipping buy/sell", etf)
        return

    allocation_pct = getattr(args, "allocation_pct", 0.1)
    max_purchase_amount = getattr(args, "max_purchase_amount", None)

    # Buys: score just crossed up through the threshold.
    for sig in transitions["buy_signals"]:
        ticker = sig["ticker"]
        if ticker not in scored:
            continue
        metrics = scored[ticker]
        price = metrics.get("current_price")
        if price is None:
            continue
        reason = format_buy_reason(
            sig["prior"], sig["current"], sig.get("flipped", []),
            is_new=sig.get("is_new", False),
        )
        trade = portfolio.buy(
            ticker, price, today,
            allocation_pct=allocation_pct,
            name=metrics.get("name", ""),
            sector=metrics.get("sector", ""),
            max_purchase_amount=max_purchase_amount,
            reason=reason,
        )
        if trade:
            log.info(
                "[%s] BUY  %s: %s, @ $%.2f",
                etf, ticker, reason, trade.price,
            )

    # Sells: score just fell below the threshold.
    for sig in transitions["sell_signals"]:
        ticker = sig["ticker"]
        if ticker not in portfolio.positions:
            continue
        price = price_data.get(ticker, portfolio.positions[ticker].current_price)
        reason = f"signal · score {sig['prior']}→{sig['current']}"
        trade = portfolio.sell(ticker, price, today, reason=reason)
        if trade:
            log.info(
                "[%s] SELL %s: %s, @ $%.2f",
                etf, ticker, reason, trade.price,
            )


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
    p.add_argument("--paper-trade", action="store_true",
                   help="Enable paper trading (buy/sell signals will be executed)")
    p.add_argument("--alpaca-trade", action="store_true",
                   help="Enable Alpaca order execution (requires ALPACA_API_KEY and ALPACA_SECRET_KEY)")
    p.add_argument("--alpaca-paper", action="store_true", default=True,
                   help="Use Alpaca paper trading API (default: True)")
    p.add_argument("--sync-alpaca-positions", action="store_true",
                   help="Sync existing Alpaca positions to local portfolio state on startup")
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
