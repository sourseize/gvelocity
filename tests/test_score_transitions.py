"""
Tests for compute_score_transitions() in src/analyzer.py.

Covers the transition rules from the plan:
  - Buy  : prior < threshold AND current >= threshold
  - Sell : prior >= threshold AND current < threshold
  - Unchanged : everything else
  - New ticker (absent from prior) → treated as prior=0
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.analyzer import compute_score_transitions, BUY_THRESHOLD, SIGNAL_LABELS


THRESHOLD = BUY_THRESHOLD  # 4


class ScoreTransitionsTests(unittest.TestCase):

    def _transitions(self, prior, current):
        return compute_score_transitions(prior, current, threshold=THRESHOLD)

    # --- Buy signals ---

    def test_buy_signal_on_threshold_crossing(self):
        """2 → 4 produces a buy signal."""
        result = self._transitions({"AAA": 2}, {"AAA": 4})
        self.assertEqual(len(result["buy_signals"]), 1)
        self.assertEqual(result["buy_signals"][0]["ticker"], "AAA")
        self.assertEqual(result["buy_signals"][0]["prior"], 2)
        self.assertEqual(result["buy_signals"][0]["current"], 4)
        self.assertEqual(len(result["sell_signals"]), 0)

    def test_buy_signal_on_score_above_threshold(self):
        """3 → 5 also produces a buy signal (prior < threshold, current >= threshold)."""
        result = self._transitions({"AAA": 3}, {"AAA": 5})
        self.assertEqual(len(result["buy_signals"]), 1)

    def test_new_ticker_at_threshold_buys(self):
        """New ticker not in prior is treated as prior=0, so score=4 triggers a buy."""
        result = self._transitions({}, {"AAA": 4})
        self.assertEqual(len(result["buy_signals"]), 1)
        self.assertEqual(result["buy_signals"][0]["prior"], 0)

    def test_new_ticker_below_threshold_does_not_buy(self):
        """New ticker at score=3 does not trigger a buy."""
        result = self._transitions({}, {"AAA": 3})
        self.assertEqual(len(result["buy_signals"]), 0)
        self.assertEqual(len(result["unchanged"]), 1)

    # --- Sell signals ---

    def test_sell_signal_on_threshold_drop(self):
        """5 → 3 produces a sell signal."""
        result = self._transitions({"AAA": 5}, {"AAA": 3})
        self.assertEqual(len(result["sell_signals"]), 1)
        self.assertEqual(result["sell_signals"][0]["ticker"], "AAA")

    def test_sell_signal_on_falling_to_below_threshold(self):
        """4 → 0 produces a sell signal."""
        result = self._transitions({"AAA": 4}, {"AAA": 0})
        self.assertEqual(len(result["sell_signals"]), 1)

    # --- Unchanged ---

    def test_no_signal_when_score_stays_above_threshold(self):
        """5 → 5 is unchanged (already above threshold)."""
        result = self._transitions({"AAA": 5}, {"AAA": 5})
        self.assertEqual(len(result["buy_signals"]), 0)
        self.assertEqual(len(result["sell_signals"]), 0)
        self.assertEqual(len(result["unchanged"]), 1)

    def test_no_signal_when_score_stays_below_threshold(self):
        """2 → 3 is unchanged (both below threshold)."""
        result = self._transitions({"AAA": 2}, {"AAA": 3})
        self.assertEqual(len(result["buy_signals"]), 0)
        self.assertEqual(len(result["sell_signals"]), 0)
        self.assertEqual(len(result["unchanged"]), 1)

    # --- Mixed signals ---

    def test_mixed_signals_across_multiple_tickers(self):
        prior = {"AAA": 2, "BBB": 5, "CCC": 3}
        current = {"AAA": 4, "BBB": 2, "CCC": 3, "DDD": 4}

        result = self._transitions(prior, current)

        buy_tickers = {s["ticker"] for s in result["buy_signals"]}
        sell_tickers = {s["ticker"] for s in result["sell_signals"]}
        unchanged_tickers = {s["ticker"] for s in result["unchanged"]}

        self.assertEqual(buy_tickers, {"AAA", "DDD"})   # 2→4 and new→4
        self.assertEqual(sell_tickers, {"BBB"})           # 5→2
        self.assertEqual(unchanged_tickers, {"CCC"})      # 3→3

    def test_ticker_absent_from_current_is_not_reported(self):
        """Tickers that disappeared from current are not in any transition bucket."""
        result = self._transitions({"AAA": 5, "BBB": 2}, {"BBB": 2})
        all_tickers = {
            s["ticker"]
            for bucket in result.values()
            for s in bucket
        }
        self.assertNotIn("AAA", all_tickers)

    def test_output_lists_are_sorted_by_ticker(self):
        prior = {}
        current = {"ZZZ": 4, "AAA": 4, "MMM": 4}
        result = self._transitions(prior, current)
        tickers = [s["ticker"] for s in result["buy_signals"]]
        self.assertEqual(tickers, sorted(tickers))


class FlippedSignalsTests(unittest.TestCase):
    """Per-signal flip derivation via optional prior_signals/current_signals."""

    def test_buy_signal_names_signal_that_flipped_on(self):
        """A single bit going False→True is reported in `flipped`."""
        # AAA 3→4; the only signal that flipped is the 1st (price ROC 20d).
        prior_signals = {"AAA": [False, True, True, True, False]}   # score 3
        current_signals = {"AAA": [True, True, True, True, False]}  # score 4
        result = compute_score_transitions(
            {"AAA": 3}, {"AAA": 4},
            threshold=BUY_THRESHOLD,
            prior_signals=prior_signals, current_signals=current_signals,
        )
        buy = result["buy_signals"][0]
        self.assertEqual(buy["ticker"], "AAA")
        self.assertEqual(buy["prior"], 3)
        self.assertEqual(buy["current"], 4)
        self.assertFalse(buy["is_new"], "AAA had a prior score, so it's not new")
        self.assertEqual(buy["flipped"], [SIGNAL_LABELS[0]])

    def test_multiple_flips_listed_in_signal_order(self):
        """Two bits flipping report both labels, in fixed signal order."""
        # AAA 2→4: signals 1 (price ROC) and 4 (price vs entry) both turn on.
        prior_signals = {"AAA": [False, True, True, False, False]}   # score 3
        current_signals = {"AAA": [True, True, True, True, False]}  # score 4
        result = compute_score_transitions(
            {"AAA": 3}, {"AAA": 4},
            prior_signals=prior_signals, current_signals=current_signals,
        )
        buy = result["buy_signals"][0]
        self.assertEqual(
            buy["flipped"],
            [SIGNAL_LABELS[0], SIGNAL_LABELS[3]],
        )

    def test_new_entrant_treats_absent_prior_as_all_false(self):
        """A ticker absent from prior_signals is diffed against all-False."""
        # AAA is brand new (no prior score), entering at score 4 with 4 signals ON.
        current_signals = {"AAA": [True, True, True, True, False]}
        result = compute_score_transitions(
            {}, {"AAA": 4},
            prior_signals={}, current_signals=current_signals,
        )
        buy = result["buy_signals"][0]
        self.assertTrue(buy["is_new"])
        self.assertEqual(buy["prior"], 0)
        # All 4 on-signals are "flips" vs an absent (all-False) prior.
        self.assertEqual(
            buy["flipped"],
            [SIGNAL_LABELS[0], SIGNAL_LABELS[1], SIGNAL_LABELS[2], SIGNAL_LABELS[3]],
        )

    def test_no_signal_maps_yields_empty_flipped(self):
        """Without prior/current signal maps, flipped is empty (bootstrap run)."""
        result = compute_score_transitions({"AAA": 2}, {"AAA": 4})
        # No kwargs → can_diff is False → flipped stays [].
        self.assertEqual(result["buy_signals"][0]["flipped"], [])
        self.assertIn("is_new", result["buy_signals"][0])

    def test_sell_carries_empty_flipped(self):
        """Sell entries have a `flipped` key too (uniform shape), empty."""
        result = compute_score_transitions(
            {"AAA": 5}, {"AAA": 3},
            prior_signals={"AAA": [True] * 5}, current_signals={"AAA": [True, True, True, False, False]},
        )
        sell = result["sell_signals"][0]
        self.assertEqual(sell["ticker"], "AAA")
        self.assertEqual(sell["flipped"], [])


if __name__ == "__main__":
    unittest.main()
