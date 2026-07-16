import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from velocity import load_portfolio_state
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


if __name__ == "__main__":
    unittest.main()
