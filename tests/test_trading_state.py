import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.database import get_db, load_trading_state, save_trading_state
from velocity import record_trading_baseline


class TradingStateTests(unittest.TestCase):
    def setUp(self):
        self.conn = get_db(":memory:")

    def tearDown(self):
        self.conn.close()

    def test_save_and_load_trading_state(self):
        save_trading_state(self.conn, "TEST", "2026-07-16", {"AAA": 2, "BBB": 4})

        self.assertEqual(
            load_trading_state(self.conn, "TEST"),
            {"last_evaluated_snapshot": "2026-07-16", "scores": {"AAA": 2, "BBB": 4}},
        )

    def test_baseline_is_created_once_for_an_existing_backfill_snapshot(self):
        day_data = {"scored": {"AAA": {"confirmation_score": 2}, "BBB": {"confirmation_score": 4}}}

        self.assertTrue(record_trading_baseline(self.conn, "TEST", "2026-07-16", day_data, snapshot_added=False))
        self.assertFalse(record_trading_baseline(self.conn, "TEST", "2026-07-16", day_data, snapshot_added=False))
        self.assertEqual(load_trading_state(self.conn, "TEST")["scores"], {"AAA": 2, "BBB": 4})

    def test_baseline_is_updated_when_a_new_snapshot_arrives_while_trading_is_disabled(self):
        save_trading_state(self.conn, "TEST", "2026-07-16", {"AAA": 2})
        day_data = {"scored": {"AAA": {"confirmation_score": 4}}}

        self.assertTrue(record_trading_baseline(self.conn, "TEST", "2026-07-17", day_data, snapshot_added=True))
        self.assertEqual(load_trading_state(self.conn, "TEST")["last_evaluated_snapshot"], "2026-07-17")
        self.assertEqual(load_trading_state(self.conn, "TEST")["scores"], {"AAA": 4})


if __name__ == "__main__":
    unittest.main()
