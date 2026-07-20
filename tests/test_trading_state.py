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
        """Scores + signals written by save_trading_state come back intact."""
        save_trading_state(
            self.conn, "TEST", "2026-07-16",
            {"AAA": 2, "BBB": 4},
            signals={"AAA": [True, False, True, False, True], "BBB": [True] * 5},
        )

        result = load_trading_state(self.conn, "TEST")
        self.assertEqual(result["last_evaluated_snapshot"], "2026-07-16")
        self.assertEqual(result["scores"], {"AAA": 2, "BBB": 4})
        self.assertEqual(result["signals"]["AAA"], [True, False, True, False, True])
        self.assertEqual(result["signals"]["BBB"], [True, True, True, True, True])

    def test_load_returns_none_when_no_state(self):
        """load_trading_state returns None if there is no state for the ETF."""
        self.assertIsNone(load_trading_state(self.conn, "MISSING"))

    def test_upsert_overwrites_previous_state(self):
        """A second save for the same ETF replaces the prior state."""
        save_trading_state(self.conn, "TEST", "2026-07-16", {"AAA": 2})
        save_trading_state(
            self.conn, "TEST", "2026-07-17",
            {"AAA": 4, "BBB": 5},
            signals={"AAA": [True] * 5, "BBB": [True, True, True, True, False]},
        )

        result = load_trading_state(self.conn, "TEST")
        self.assertEqual(result["last_evaluated_snapshot"], "2026-07-17")
        self.assertEqual(result["scores"], {"AAA": 4, "BBB": 5})
        self.assertEqual(result["signals"]["AAA"], [True, True, True, True, True])
        self.assertEqual(result["signals"]["BBB"], [True, True, True, True, False])

    def test_separate_etfs_do_not_interfere(self):
        """State for different ETFs is stored independently."""
        save_trading_state(self.conn, "MTUM", "2026-07-16", {"AAPL": 5})
        save_trading_state(self.conn, "QUAL", "2026-07-16", {"MSFT": 3})

        self.assertEqual(load_trading_state(self.conn, "MTUM")["scores"], {"AAPL": 5})
        self.assertEqual(load_trading_state(self.conn, "QUAL")["scores"], {"MSFT": 3})

    def test_legacy_int_payload_loads_with_empty_bits(self):
        """A pre-upgrade {ticker: int} payload loads with empty signal bits.

        The persisted shape changed to {ticker: {"score", "bits"}}; older rows
        store plain ints and must coerce without raising so existing DBs load
        after upgrade.
        """
        # Write a legacy-style payload directly (hand-rolled, no `signals=`).
        import json, datetime
        conn = self.conn
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO trading_state (etf, last_evaluated_snapshot, scores_json, updated_at) "
            "VALUES (?, ?, ?, ?)",
            ("LEG", "2026-07-15", json.dumps({"X": 3, "Y": 1}, sort_keys=True), now),
        )
        conn.commit()

        result = load_trading_state(conn, "LEG")
        self.assertEqual(result["scores"], {"X": 3, "Y": 1})
        self.assertEqual(result["signals"], {"X": [], "Y": []})

    def test_save_without_signals_loads_empty_bits(self):
        """save_trading_state with no `signals=` stores empty bits per ticker."""
        save_trading_state(self.conn, "TEST", "2026-07-16", {"AAA": 2, "BBB": 4})

        result = load_trading_state(self.conn, "TEST")
        self.assertEqual(result["scores"], {"AAA": 2, "BBB": 4})
        self.assertEqual(result["signals"], {"AAA": [], "BBB": []})


if __name__ == "__main__":
    unittest.main()
