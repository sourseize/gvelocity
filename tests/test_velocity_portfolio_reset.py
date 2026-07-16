import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from velocity import reset_portfolio_state
from src.trader import Portfolio


def test_reset_portfolio_state_removes_existing_files_and_returns_fresh_portfolio(tmp_path: Path) -> None:
    portfolio_file = tmp_path / "portfolio.json"
    html_file = tmp_path / "portfolio.html"
    portfolio_file.write_text('{"cash": 42}', encoding="utf-8")
    html_file.write_text("old", encoding="utf-8")

    portfolio = reset_portfolio_state("TEST", portfolio_file, initial_capital=1000.0)

    assert isinstance(portfolio, Portfolio)
    assert portfolio.cash == 1000.0
    assert portfolio.positions == {}
    assert portfolio.trade_history == []
    assert not portfolio_file.exists()
    assert not html_file.exists()
