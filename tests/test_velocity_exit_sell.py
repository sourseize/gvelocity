import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.database import get_db, store_snapshot
from src.trader import Portfolio
from velocity import build_day_data, execute_paper_trades_for_etf


class ExitSellTests(unittest.TestCase):
    def test_exit_from_snapshot_sells_open_position(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            conn = get_db(db_path)

            store_snapshot(conn, date(2024, 1, 1), [
                {"ticker": "AAA", "name": "Alpha", "sector": "Tech", "weight_pct": 1.0, "market_value": None, "shares": None, "price": 100.0, "currency": "USD", "exchange": None, "location": None},
            ])
            store_snapshot(conn, date(2024, 1, 2), [
                {"ticker": "BBB", "name": "Beta", "sector": "Health", "weight_pct": 1.0, "market_value": None, "shares": None, "price": 100.0, "currency": "USD", "exchange": None, "location": None},
            ])

            portfolio = Portfolio(initial_capital=1000.0)
            portfolio.buy("AAA", 100.0, "2024-01-01", allocation_pct=0.1)

            day_data = build_day_data(conn, "2024-01-02")
            args = type("Args", (), {"trail_percent": 0.15, "allocation_pct": 0.1, "max_purchase_amount": None})()

            execute_paper_trades_for_etf(conn, "2024-01-02", day_data, portfolio, "TEST", args)

            self.assertNotIn("AAA", portfolio.positions)
            conn.close()


if __name__ == "__main__":
    unittest.main()
