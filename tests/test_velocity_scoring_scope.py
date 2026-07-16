import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.database import get_db, store_snapshot
from velocity import build_day_data


class ScoringScopeTests(unittest.TestCase):
    def test_build_day_data_scores_all_current_holdings(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            conn = get_db(db_path)

            holdings = []
            for idx in range(31):
                ticker = f"T{idx:02d}"
                holdings.append({
                    "ticker": ticker,
                    "name": f"Name {idx}",
                    "sector": "Tech",
                    "weight_pct": 100.0 - idx,
                    "market_value": None,
                    "shares": None,
                    "price": 100.0 + idx,
                    "currency": "USD",
                    "exchange": None,
                    "location": None,
                })

            store_snapshot(conn, date(2024, 1, 1), holdings)
            store_snapshot(conn, date(2024, 1, 2), holdings)

            day_data = build_day_data(conn, "2024-01-02")

            self.assertIn("T00", day_data["scored"])
            self.assertIn("T30", day_data["scored"])
            conn.close()


if __name__ == "__main__":
    unittest.main()
