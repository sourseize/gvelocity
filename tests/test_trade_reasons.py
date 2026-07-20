"""
Tests that a trade carries a human-readable reason end-to-end.

Two layers:
  1. Trade.to_dict / from_dict round-trip the `reason` field (and tolerate old,
     pre-reason JSON via the `.get` default).
  2. The REAL velocity execute path (execute_paper_trades_for_etf) builds a buy
     reason naming the confirmation signal that flipped, and records it on the
     Trade. The regression file inlines its own execute() copy, so reason logic
     in velocity.py can only be exercised against the real function — done here.
"""
import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.database import get_db, store_snapshot, save_trading_state
from src.analyzer import compute_score_transitions, format_buy_reason, BUY_THRESHOLD, SIGNAL_LABELS
from src.trader import Trade, Portfolio, OrderType
from velocity import build_day_data, execute_paper_trades_for_etf


def _make_args(**overrides):
    base = SimpleNamespace(
        allocation_pct=0.10,
        max_purchase_amount=None,
        trail_percent=0.15,
    )
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


class TradeReasonRoundTripTests(unittest.TestCase):
    def test_to_dict_includes_reason(self):
        trade = Trade("AAA", OrderType.BUY, 100.0, "2026-07-17", 10.0, 1.0, reason="score 3→4 · Price ROC 20d turned positive")
        d = trade.to_dict()
        self.assertEqual(d["reason"], "score 3→4 · Price ROC 20d turned positive")

    def test_from_dict_without_reason_defaults_to_empty(self):
        """Old JSON (no reason key) loads with an empty reason."""
        old = {
            "ticker": "AAA", "type": "buy", "price": 100.0,
            "date": "2026-07-17", "shares": 10.0, "commission": 1.0,
            "value": 1000.0, "total": 1001.0,
        }
        trade = Trade.from_dict(old)
        self.assertEqual(trade.reason, "")

    def test_from_dict_with_reason_loaded(self):
        d = {
            "ticker": "AAA", "type": "sell", "price": 100.0,
            "date": "2026-07-17", "shares": 10.0, "commission": 1.0,
            "reason": "signal · score 5→3",
            "value": 1000.0, "total": 999.0,
        }
        trade = Trade.from_dict(d)
        self.assertEqual(trade.reason, "signal · score 5→3")
        self.assertEqual(trade.type, OrderType.SELL)


class FormatBuyReasonTests(unittest.TestCase):
    def test_crossing_with_flipped_signals(self):
        reason = format_buy_reason(3, 4, [SIGNAL_LABELS[0]])
        self.assertEqual(reason, "score 3→4 · Price ROC 20d turned positive")

    def test_crossing_with_multiple_flips(self):
        reason = format_buy_reason(2, 4, [SIGNAL_LABELS[1], SIGNAL_LABELS[3]])
        self.assertEqual(reason, "score 2→4 · Weight growing since entry + Price above entry")

    def test_new_entrant_with_signals(self):
        reason = format_buy_reason(0, 4, [SIGNAL_LABELS[0], SIGNAL_LABELS[2]], is_new=True)
        self.assertEqual(
            reason,
            "New entrant · score 0→4 · Price ROC 20d turned positive + Weight rank entered top half",
        )

    def test_bootstrap_initial_buy_has_no_flips(self):
        """No prior signal history → an honest 'Initial buy' reason, no flips."""
        reason = format_buy_reason(0, 4, [], is_new=False)
        self.assertEqual(reason, "score 0→4")

    def test_bootstrap_initial_buy_new_entrant_no_history(self):
        reason = format_buy_reason(0, 4, [], is_new=True)
        self.assertEqual(reason, "Initial buy · score 4/5")


class ExecuteBuyReasonTests(unittest.TestCase):
    """End-to-end: the real execute_paper_trades_for_etf records a flipped-signal buy reason."""

    def test_buy_reason_names_flipped_signal_when_crossing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            conn = get_db(Path(tmpdir) / "test.db")

            # Two snapshots so compute_metrics has history. MSFT held flat-ish so
            # signals are driven by the bit vectors we inject via day_data.
            row = lambda t, w, p: {
                "ticker": t, "name": t, "sector": "Tech", "weight_pct": w,
                "market_value": None, "shares": None, "price": p,
                "currency": "USD", "exchange": None, "location": None,
            }
            store_snapshot(conn, date(2026, 7, 15), [row("MSFT", 1.5, 400.0)])
            store_snapshot(conn, date(2026, 7, 16), [row("MSFT", 1.5, 402.0)])

            # Establish prior trading state (day 1): score 3 with bits where the
            # price-ROC signal is OFF and the rest ON.
            save_trading_state(
                conn, "TEST", "2026-07-15",
                {"MSFT": 3},
                signals={"MSFT": [False, True, True, True, False]},
            )

            # Day 2: MSFT reaches score 4 with the price-ROC bit now ON.
            scored = {
                "MSFT": {
                    "ticker": "MSFT", "name": "MSFT", "sector": "Tech",
                    "current_price": 402.0,
                    "confirmation_score": 4,
                    "confirmation_signal_bits": [True, True, True, True, False],
                },
            }
            day_data = {
                "date": "2026-07-16", "new_tickers": [], "exits": [],
                "movers": [], "scored": scored, "top20": [], "sectors": [],
                "rising_stars": [], "n_snapshots": 2, "lookback": 20,
            }
            transitions = compute_score_transitions(
                {"MSFT": 3}, {"MSFT": 4},
                threshold=BUY_THRESHOLD,
                prior_signals={"MSFT": [False, True, True, True, False]},
                current_signals={"MSFT": [True, True, True, True, False]},
            )
            portfolio = Portfolio(initial_capital=100_000.0)
            execute_paper_trades_for_etf(
                conn, "2026-07-16", day_data, portfolio, "TEST", _make_args(),
                transitions=transitions,
            )
            conn.close()

            buys = [t for t in portfolio.trade_history if t.type == OrderType.BUY]
            self.assertEqual(len(buys), 1)
            self.assertEqual(buys[0].ticker, "MSFT")
            self.assertIn(SIGNAL_LABELS[0], buys[0].reason,
                          "buy reason should name the signal that flipped on")
            self.assertIn("score 3→4", buys[0].reason)

    def test_sell_reason_includes_score_transition(self):
        """A signal-sell records the score drop in its reason."""
        with tempfile.TemporaryDirectory() as tmpdir:
            conn = get_db(Path(tmpdir) / "test.db")
            row = lambda t, w, p: {
                "ticker": t, "name": t, "sector": "Tech", "weight_pct": w,
                "market_value": None, "shares": None, "price": p,
                "currency": "USD", "exchange": None, "location": None,
            }
            store_snapshot(conn, date(2026, 7, 15), [row("NVDA", 2.0, 120.0)])
            store_snapshot(conn, date(2026, 7, 16), [row("NVDA", 2.0, 118.0)])

            save_trading_state(
                conn, "TEST", "2026-07-15",
                {"NVDA": 5},
                signals={"NVDA": [True] * 5},
            )

            scored = {
                "NVDA": {
                    "ticker": "NVDA", "name": "NVDA", "sector": "Tech",
                    "current_price": 118.0,
                    "confirmation_score": 3,
                    "confirmation_signal_bits": [True, True, True, False, False],
                },
            }
            day_data = {
                "date": "2026-07-16", "new_tickers": [], "exits": [],
                "movers": [], "scored": scored, "top20": [], "sectors": [],
                "rising_stars": [], "n_snapshots": 2, "lookback": 20,
            }
            transitions = compute_score_transitions(
                {"NVDA": 5}, {"NVDA": 3},
                threshold=BUY_THRESHOLD,
                prior_signals={"NVDA": [True] * 5},
                current_signals={"NVDA": [True, True, True, False, False]},
            )
            portfolio = Portfolio(initial_capital=100_000.0)
            # Open the position first so a sell can close it.
            portfolio.buy("NVDA", 120.0, "2026-07-15")

            execute_paper_trades_for_etf(
                conn, "2026-07-16", day_data, portfolio, "TEST", _make_args(),
                transitions=transitions,
            )
            conn.close()

            sells = [t for t in portfolio.trade_history if t.type == OrderType.SELL]
            self.assertEqual(len(sells), 1)
            self.assertEqual(sells[0].ticker, "NVDA")
            self.assertIn("signal", sells[0].reason)
            self.assertIn("score 5→3", sells[0].reason)


if __name__ == "__main__":
    unittest.main()
