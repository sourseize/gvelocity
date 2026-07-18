import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import backfill
from velocity import load_portfolio_state, should_execute_paper_trades
from src.database import get_db
from src.trader import Portfolio


class PortfolioStateTests(unittest.TestCase):
    def test_load_portfolio_state_uses_saved_state_when_file_exists(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            portfolio_file = Path(tmpdir) / "portfolio.json"
            original = Portfolio(initial_capital=1000.0)
            original.cash = 500.0
            original.save_state(portfolio_file)

            loaded = load_portfolio_state("TEST", original, portfolio_file)

            self.assertEqual(loaded.cash, 500.0)
            self.assertEqual(loaded.initial_capital, 1000.0)

    def test_save_state_persists_portfolio_to_database(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "TEST.db"
            conn = get_db(db_path)
            portfolio_file = Path(tmpdir) / "portfolio.json"
            original = Portfolio(initial_capital=1000.0)
            original.cash = 250.0

            original.save_state(portfolio_file, conn=conn)

            loaded = Portfolio.load_state(portfolio_file, conn=conn)

            self.assertEqual(loaded.cash, 250.0)
            self.assertEqual(loaded.initial_capital, 1000.0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM portfolio_state").fetchone()[0], 1)
            conn.close()

    def test_wipe_existing_clears_portfolio_state_table(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "TEST.db"
            conn = get_db(db_path)
            portfolio_file = Path(tmpdir) / "portfolio.json"
            portfolio = Portfolio(initial_capital=1000.0)
            portfolio.save_state(portfolio_file, conn=conn)

            backfill.wipe_existing(conn, Path(tmpdir))

            self.assertEqual(conn.execute("SELECT COUNT(*) FROM portfolio_state").fetchone()[0], 0)
            conn.close()

    def test_report_only_runs_skip_paper_trades(self):
        args = SimpleNamespace(no_paper_trade=False, report_only=True)
        self.assertFalse(should_execute_paper_trades(args))


if __name__ == "__main__":
    unittest.main()
