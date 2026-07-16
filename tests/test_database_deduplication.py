import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.database import get_db, store_snapshot


class DatabaseDeduplicationTests(unittest.TestCase):
    def test_store_snapshot_deduplicates_ticker_rows_for_same_day(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            conn = get_db(Path(tmpdir) / "test.db")
            rows = [
                {"ticker": " AAA ", "name": "Alpha", "sector": "Tech", "weight_pct": 1.0, "market_value": None, "shares": None, "price": 10.0, "currency": "USD", "exchange": None, "location": None},
                {"ticker": "AAA", "name": "Alpha", "sector": "Tech", "weight_pct": 1.0, "market_value": None, "shares": None, "price": 10.0, "currency": "USD", "exchange": None, "location": None},
            ]

            stored = store_snapshot(conn, date(2024, 1, 2), rows)
            self.assertTrue(stored)

            count = conn.execute("SELECT COUNT(*) FROM holdings WHERE snapshot_date=?", ("2024-01-02",)).fetchone()[0]
            self.assertEqual(count, 1)
            conn.close()


if __name__ == "__main__":
    unittest.main()
