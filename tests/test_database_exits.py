import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.database import detect_exits, get_db, store_snapshot


class DetectExitsTests(unittest.TestCase):
    def test_detect_exits_uses_requested_date_and_preceding_snapshot(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            conn = get_db(db_path)

            store_snapshot(conn, date(2024, 1, 1), [
                {"ticker": "A", "name": "Alpha", "sector": "Tech", "weight_pct": 1.0, "market_value": None, "shares": None, "price": None, "currency": "USD", "exchange": None, "location": None},
                {"ticker": "B", "name": "Beta", "sector": "Health", "weight_pct": 1.0, "market_value": None, "shares": None, "price": None, "currency": "USD", "exchange": None, "location": None},
            ])
            store_snapshot(conn, date(2024, 1, 2), [
                {"ticker": "A", "name": "Alpha", "sector": "Tech", "weight_pct": 1.0, "market_value": None, "shares": None, "price": None, "currency": "USD", "exchange": None, "location": None},
                {"ticker": "C", "name": "Gamma", "sector": "Finance", "weight_pct": 1.0, "market_value": None, "shares": None, "price": None, "currency": "USD", "exchange": None, "location": None},
            ])
            store_snapshot(conn, date(2024, 1, 3), [
                {"ticker": "A", "name": "Alpha", "sector": "Tech", "weight_pct": 1.0, "market_value": None, "shares": None, "price": None, "currency": "USD", "exchange": None, "location": None},
                {"ticker": "D", "name": "Delta", "sector": "Energy", "weight_pct": 1.0, "market_value": None, "shares": None, "price": None, "currency": "USD", "exchange": None, "location": None},
            ])

            exits = detect_exits(conn, "2024-01-02")

            self.assertEqual([row["ticker"] for row in exits], ["B"])
            conn.close()


if __name__ == "__main__":
    unittest.main()
