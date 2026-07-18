import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.database import get_db, load_trading_state, save_trading_state


class TradingStateTests(unittest.TestCase):
    def setUp(self):
        self.conn = get_db(":memory:")

    def tearDown(self):
        self.conn.close()

    def test_save_and_load_round_trip(self):
        """Scores written by save_trading_state come back intact via load."""
        save_trading_state(self.conn, "TEST", "2026-07-16", {"AAA": 2, "BBB": 4})

        result = load_trading_state(self.conn, "TEST")
        self.assertEqual(result, {
            "last_evaluated_snapshot": "2026-07-16",
            "scores": {"AAA": 2, "BBB": 4},
        })

    def test_load_returns_none_when_no_state(self):
        """load_trading_state returns None if there is no state for the ETF."""
        self.assertIsNone(load_trading_state(self.conn, "MISSING"))

    def test_upsert_overwrites_previous_state(self):
        """A second save for the same ETF replaces the prior state."""
        save_trading_state(self.conn, "TEST", "2026-07-16", {"AAA": 2})
        save_trading_state(self.conn, "TEST", "2026-07-17", {"AAA": 4, "BBB": 5})

        result = load_trading_state(self.conn, "TEST")
        self.assertEqual(result["last_evaluated_snapshot"], "2026-07-17")
        self.assertEqual(result["scores"], {"AAA": 4, "BBB": 5})

    def test_separate_etfs_do_not_interfere(self):
        """State for different ETFs is stored independently."""
        save_trading_state(self.conn, "MTUM", "2026-07-16", {"AAPL": 5})
        save_trading_state(self.conn, "QUAL", "2026-07-16", {"MSFT": 3})

        self.assertEqual(load_trading_state(self.conn, "MTUM")["scores"], {"AAPL": 5})
        self.assertEqual(load_trading_state(self.conn, "QUAL")["scores"], {"MSFT": 3})


if __name__ == "__main__":
    unittest.main()
