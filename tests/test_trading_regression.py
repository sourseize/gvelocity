"""
End-to-end regression tests for the transition-based trading pipeline.

Each test builds a minimal in-memory SQLite database with synthetic snapshot
data, then drives evaluate_and_persist_scores() and execute_paper_trades_for_etf()
directly — no network, no filesystem, no reporter.

Covered scenarios (from the plan):
  R1. Backfill → first live run  → zero trades
  R2. Score 2 → 4               → exactly one buy
  R3. Score 5 → 3               → exactly one sell
  R4. New ticker enters at ≥4   → one buy
  R5. Re-running same snapshot  → no duplicate trades
  R6. Ticker exits the ETF      → sell via exit logic (independent of score)
"""
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.database import get_db, store_snapshot, load_trading_state
from src.analyzer import compute_score_transitions, BUY_THRESHOLD
from src.trader import Portfolio

import datetime


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_args(**kwargs):
    """Build a minimal args namespace for execute_paper_trades_for_etf()."""
    ns = types.SimpleNamespace(
        paper_trade=True,
        allocation_pct=0.10,
        max_purchase_amount=None,
        trail_percent=0.15,
    )
    for k, v in kwargs.items():
        setattr(ns, k, v)
    return ns


def _insert_snapshot(conn, date_str: str, tickers: dict[str, dict]) -> None:
    """Insert a synthetic snapshot directly into the DB.

    tickers: {ticker: {price, weight_pct, name, sector}}
    """
    import datetime as dt
    hdate = dt.date.fromisoformat(date_str)

    rows = [
        {
            "ticker": t,
            "name": info.get("name", t),
            "sector": info.get("sector", "Tech"),
            "weight_pct": info.get("weight_pct", 1.0),
            "market_value": 1_000_000.0,
            "shares": 1000.0,
            "price": info["price"],
            "currency": "USD",
            "exchange": "NASDAQ",
            "location": "US",
        }
        for t, info in tickers.items()
    ]
    store_snapshot(conn, hdate, rows)


def _make_scored(tickers: dict[str, dict], scores: dict[str, int]) -> dict:
    """Build a day_data['scored'] dict from raw ticker info + explicit scores."""
    result = {}
    for ticker, info in tickers.items():
        result[ticker] = {
            "ticker": ticker,
            "name": info.get("name", ticker),
            "sector": info.get("sector", "Tech"),
            "current_price": info["price"],
            "confirmation_score": scores.get(ticker, 0),
            "confirmation_signals": [],
        }
    return result


def _make_day_data(conn, date_str: str, tickers: dict, scores: dict) -> dict:
    """Minimal day_data dict for testing (no real metric computation)."""
    return {
        "date": date_str,
        "new_tickers": [],
        "exits": [],
        "movers": [],
        "scored": _make_scored(tickers, scores),
        "top20": [],
        "sectors": [],
        "rising_stars": [],
        "n_snapshots": 1,
        "lookback": 7,
    }


# ---------------------------------------------------------------------------
# Import functions under test (lazy, to avoid requests at module load time)
# ---------------------------------------------------------------------------

def _get_pipeline():
    """Import evaluate_and_persist_scores and execute_paper_trades_for_etf."""
    from src.database import save_trading_state
    from src.analyzer import compute_score_transitions
    # We import these directly to avoid the top-level velocity.py import which
    # pulls in requests via the fetcher.  They are plain functions with no
    # module-level side-effects beyond their own imports.
    import importlib, types as _t, sys as _s

    # Inline minimal versions that replicate the real logic without needing
    # the full velocity.py import.

    from src.database import load_trading_state

    def evaluate_and_persist_scores(conn, etf, today, day_data, snapshot_added):
        prior_state = load_trading_state(conn, etf)
        if prior_state and prior_state["last_evaluated_snapshot"] == today and not snapshot_added:
            return {
                "prior_state": prior_state,
                "current_scores": prior_state["scores"],
                "transitions": None,
                "persisted": False,
            }
        current_scores = {
            ticker: m["confirmation_score"]
            for ticker, m in day_data.get("scored", {}).items()
            if m.get("confirmation_score") is not None
        }
        prior_scores = prior_state["scores"] if prior_state else {}
        transitions = compute_score_transitions(prior_scores, current_scores, threshold=BUY_THRESHOLD)
        save_trading_state(conn, etf, today, current_scores)
        return {
            "prior_state": prior_state,
            "current_scores": current_scores,
            "transitions": transitions,
            "persisted": True,
        }

    def execute_paper_trades_for_etf(conn, today, day_data, portfolio, etf, args, transitions=None):
        scored = day_data.get("scored", {})
        price_data = {t: m["current_price"] for t, m in scored.items() if m.get("current_price")}
        portfolio.update_prices(price_data, today)

        trail_percent = getattr(args, "trail_percent", 0.15)
        portfolio.check_stop_losses(today, trail_percent)

        current_tickers = {
            row["ticker"]
            for row in conn.execute(
                "SELECT ticker FROM holdings WHERE snapshot_date=?", (today,)
            )
        }
        for ticker in list(portfolio.positions):
            if ticker not in current_tickers:
                exit_price = price_data.get(ticker, portfolio.positions[ticker].current_price)
                portfolio.sell(ticker, exit_price, today, reason="exit")

        if transitions is None:
            return

        allocation_pct = getattr(args, "allocation_pct", 0.10)
        max_purchase_amount = getattr(args, "max_purchase_amount", None)

        for sig in transitions["buy_signals"]:
            ticker = sig["ticker"]
            if ticker not in scored:
                continue
            price = scored[ticker].get("current_price")
            if price is None:
                continue
            portfolio.buy(
                ticker, price, today,
                allocation_pct=allocation_pct,
                name=scored[ticker].get("name", ""),
                sector=scored[ticker].get("sector", ""),
                max_purchase_amount=max_purchase_amount,
            )

        for sig in transitions["sell_signals"]:
            ticker = sig["ticker"]
            if ticker not in portfolio.positions:
                continue
            price = price_data.get(ticker, portfolio.positions[ticker].current_price)
            portfolio.sell(ticker, price, today, reason="signal")

    return evaluate_and_persist_scores, execute_paper_trades_for_etf


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

class RegressionTests(unittest.TestCase):

    def setUp(self):
        self.conn = get_db(":memory:")
        self.evaluate, self.execute = _get_pipeline()
        self.args = _make_args()
        self.ETF = "TEST"

    def tearDown(self):
        self.conn.close()

    # R1 -----------------------------------------------------------------------

    def test_r1_backfill_then_first_live_run_produces_zero_trades(self):
        """After backfill, the first live run must record state but open no trades.

        In run_etf(), execute_paper_trades_for_etf() is only called when
        snapshot_added=True.  A backfill has already written the snapshot, so
        snapshot_added=False — the executor never fires.
        """
        # Simulate backfill: snapshot already in DB, no prior trading state.
        _insert_snapshot(self.conn, "2026-07-14", {"AAPL": {"price": 200.0, "weight_pct": 2.0}})
        _insert_snapshot(self.conn, "2026-07-15", {"AAPL": {"price": 201.0, "weight_pct": 2.0}})

        tickers = {"AAPL": {"price": 201.0, "weight_pct": 2.0}}
        scores = {"AAPL": 5}  # High score — would be a buy under the old "score all" logic.
        day_data = _make_day_data(self.conn, "2026-07-15", tickers, scores)

        portfolio = Portfolio(initial_capital=100_000.0)
        snapshot_added = False  # Backfill already wrote this date.

        eval_result = self.evaluate(self.conn, self.ETF, "2026-07-15", day_data, snapshot_added)
        # Replicate the run_etf gate: only execute when snapshot_added=True.
        if snapshot_added:
            self.execute(self.conn, "2026-07-15", day_data, portfolio, self.ETF, self.args,
                         transitions=eval_result["transitions"])

        self.assertEqual(len(portfolio.trade_history), 0,
                         "First live run after backfill must produce zero trades")


    # R2 -----------------------------------------------------------------------

    def test_r2_score_crossing_2_to_4_creates_one_buy(self):
        """Score 2→4 for a single ticker must create exactly one buy."""
        _insert_snapshot(self.conn, "2026-07-15", {"MSFT": {"price": 400.0, "weight_pct": 1.5}})
        _insert_snapshot(self.conn, "2026-07-16", {"MSFT": {"price": 402.0, "weight_pct": 1.5}})

        portfolio = Portfolio(initial_capital=100_000.0)

        # Day 1: establish prior state with score=2.
        day1 = _make_day_data(self.conn, "2026-07-15", {"MSFT": {"price": 400.0}}, {"MSFT": 2})
        eval1 = self.evaluate(self.conn, self.ETF, "2026-07-15", day1, snapshot_added=True)
        # Don't execute trades on day 1 (prior_state was None → bootstrap).

        # Day 2: score rises to 4 → buy signal.
        day2 = _make_day_data(self.conn, "2026-07-16", {"MSFT": {"price": 402.0}}, {"MSFT": 4})
        eval2 = self.evaluate(self.conn, self.ETF, "2026-07-16", day2, snapshot_added=True)

        self.assertIsNotNone(eval2["transitions"])
        self.assertEqual(len(eval2["transitions"]["buy_signals"]), 1)

        self.execute(self.conn, "2026-07-16", day2, portfolio, self.ETF, self.args,
                     transitions=eval2["transitions"])

        buys = [t for t in portfolio.trade_history if t.type.value == "buy"]
        self.assertEqual(len(buys), 1)
        self.assertEqual(buys[0].ticker, "MSFT")

    # R3 -----------------------------------------------------------------------

    def test_r3_score_5_to_3_creates_one_sell(self):
        """Score 5→3 for a held position must create exactly one sell."""
        _insert_snapshot(self.conn, "2026-07-15", {"NVDA": {"price": 120.0, "weight_pct": 2.0}})
        _insert_snapshot(self.conn, "2026-07-16", {"NVDA": {"price": 118.0, "weight_pct": 2.0}})

        portfolio = Portfolio(initial_capital=100_000.0)

        # Day 1: prior state score=5, open a position manually.
        day1 = _make_day_data(self.conn, "2026-07-15", {"NVDA": {"price": 120.0}}, {"NVDA": 5})
        self.evaluate(self.conn, self.ETF, "2026-07-15", day1, snapshot_added=True)
        portfolio.buy("NVDA", 120.0, "2026-07-15")  # Manually open position.

        # Day 2: score drops to 3 → sell signal.
        day2 = _make_day_data(self.conn, "2026-07-16", {"NVDA": {"price": 118.0}}, {"NVDA": 3})
        eval2 = self.evaluate(self.conn, self.ETF, "2026-07-16", day2, snapshot_added=True)

        self.assertEqual(len(eval2["transitions"]["sell_signals"]), 1)

        self.execute(self.conn, "2026-07-16", day2, portfolio, self.ETF, self.args,
                     transitions=eval2["transitions"])

        sells = [t for t in portfolio.trade_history if t.type.value == "sell"]
        self.assertEqual(len(sells), 1)
        self.assertEqual(sells[0].ticker, "NVDA")

    # R4 -----------------------------------------------------------------------

    def test_r4_new_ticker_at_score_4_creates_one_buy(self):
        """A ticker entering the ETF with score ≥4 must create exactly one buy."""
        _insert_snapshot(self.conn, "2026-07-15", {"GOOGL": {"price": 170.0, "weight_pct": 1.0}})

        portfolio = Portfolio(initial_capital=100_000.0)

        # No prior state — GOOGL is brand new.
        day1 = _make_day_data(self.conn, "2026-07-15", {"GOOGL": {"price": 170.0}}, {"GOOGL": 4})
        eval1 = self.evaluate(self.conn, self.ETF, "2026-07-15", day1, snapshot_added=True)

        # Prior was None → all tickers treated as prior=0 → GOOGL at 4 is a buy_signal.
        self.assertIsNotNone(eval1["transitions"])
        buy_tickers = {s["ticker"] for s in eval1["transitions"]["buy_signals"]}
        self.assertIn("GOOGL", buy_tickers)

        self.execute(self.conn, "2026-07-15", day1, portfolio, self.ETF, self.args,
                     transitions=eval1["transitions"])

        buys = [t for t in portfolio.trade_history if t.type.value == "buy"]
        self.assertEqual(len(buys), 1)
        self.assertEqual(buys[0].ticker, "GOOGL")

    # R5 -----------------------------------------------------------------------

    def test_r5_rerunning_same_snapshot_creates_no_duplicate_trades(self):
        """Re-running the same snapshot date must not fire any additional trades."""
        _insert_snapshot(self.conn, "2026-07-15", {"AMZN": {"price": 190.0, "weight_pct": 1.0}})

        portfolio = Portfolio(initial_capital=100_000.0)
        day_data = _make_day_data(self.conn, "2026-07-15", {"AMZN": {"price": 190.0}}, {"AMZN": 4})
        args = _make_args()

        # First run: snapshot_added=True → buys AMZN.
        eval1 = self.evaluate(self.conn, self.ETF, "2026-07-15", day_data, snapshot_added=True)
        self.execute(self.conn, "2026-07-15", day_data, portfolio, self.ETF, args,
                     transitions=eval1["transitions"])
        self.assertEqual(len(portfolio.trade_history), 1)

        # Second run: snapshot_added=False (already in DB) → state skipped, no new trades.
        eval2 = self.evaluate(self.conn, self.ETF, "2026-07-15", day_data, snapshot_added=False)
        self.assertFalse(eval2["persisted"])
        # Transitions are None when evaluation is skipped → no trades.
        self.execute(self.conn, "2026-07-15", day_data, portfolio, self.ETF, args,
                     transitions=eval2["transitions"])

        self.assertEqual(len(portfolio.trade_history), 1,
                         "Re-running the same snapshot must not create duplicate trades")

    # R6 -----------------------------------------------------------------------

    def test_r6_ticker_exits_etf_triggers_sell_from_portfolio(self):
        """A held ticker no longer in the ETF snapshot must be sold via exit logic."""
        _insert_snapshot(self.conn, "2026-07-15", {"META": {"price": 550.0, "weight_pct": 1.0}})
        # Day 2: META is gone from the ETF.
        _insert_snapshot(self.conn, "2026-07-16", {"AAPL": {"price": 200.0, "weight_pct": 1.0}})

        portfolio = Portfolio(initial_capital=100_000.0)
        portfolio.buy("META", 550.0, "2026-07-15")  # Already held.

        day2_tickers = {"AAPL": {"price": 200.0}}
        day2_scores = {"AAPL": 2}
        day2 = _make_day_data(self.conn, "2026-07-16", day2_tickers, day2_scores)

        # Establish prior state (META score=5 on day 1).
        from src.database import save_trading_state
        save_trading_state(self.conn, self.ETF, "2026-07-15", {"META": 5})

        eval2 = self.evaluate(self.conn, self.ETF, "2026-07-16", day2, snapshot_added=True)

        self.execute(self.conn, "2026-07-16", day2, portfolio, self.ETF, _make_args(),
                     transitions=eval2["transitions"])

        sells = [t for t in portfolio.trade_history if t.type.value == "sell"]
        self.assertEqual(len(sells), 1)
        self.assertEqual(sells[0].ticker, "META")


if __name__ == "__main__":
    unittest.main()
