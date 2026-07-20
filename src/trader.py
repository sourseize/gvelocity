"""
Paper trading functionality for Velocity tracker
"""

import json
import datetime
from pathlib import Path
from typing import Dict, List, Optional
from enum import Enum


class OrderStatus(Enum):
    OPEN = "open"
    CLOSED = "closed"
    CANCELLED = "cancelled"

class OrderType(Enum):
    BUY = "buy"
    SELL = "sell"

class Trade:
    """Represents a single trade (buy or sell)"""
    def __init__(self, ticker: str, order_type: OrderType, price: float,
                 date: str, shares: float, commission: float = 0.0, reason: str = ""):
        self.ticker = ticker
        self.type = order_type
        self.price = price
        self.date = date
        self.shares = shares
        self.commission = commission
        self.reason = reason
        self.value = price * shares
        self.total = self.value + commission  # For buys, this is negative cash flow

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "type": self.type.value,
            "price": self.price,
            "date": self.date,
            "shares": self.shares,
            "commission": self.commission,
            "reason": self.reason,
            "value": self.value,
            "total": self.total
        }

    @classmethod
    def from_dict(cls, data: dict):
        trade = cls(
            ticker=data["ticker"],
            order_type=OrderType(data["type"]),
            price=data["price"],
            date=data["date"],
            shares=data["shares"],
            commission=data.get("commission", 0.0),
            reason=data.get("reason", "")
        )
        return trade

class Position:
    """Represents a current position in a stock"""
    def __init__(self, ticker: str, entry_price: float, entry_date: str,
                 shares: float, name: str = "", sector: str = ""):
        self.ticker = ticker
        self.name = name
        self.sector = sector
        self.entry_price = entry_price
        self.entry_date = entry_date
        self.shares = shares
        # Track highest price since entry for trailing stop
        self.highest_price = entry_price
        self.current_price = entry_price

    def update_price(self, price: float, date: str):
        """Update current price and track high for trailing stop"""
        self.current_price = price
        if price > self.highest_price:
            self.highest_price = price

    def unrealized_pnl(self) -> float:
        """Calculate unrealized P&L"""
        return (self.current_price - self.entry_price) * self.shares

    def unrealized_pnl_percent(self) -> float:
        """Calculate unrealized P&L as percentage"""
        if self.entry_price == 0:
            return 0.0
        return (self.current_price - self.entry_price) / self.entry_price * 100

    def trailing_stop_price(self, trail_percent: float = 0.15) -> float:
        """Calculate trailing stop price"""
        return self.highest_price * (1 - trail_percent)

    def should_stop_out(self, trail_percent: float = 0.15) -> bool:
        """Check if price has hit trailing stop"""
        return self.current_price <= self.trailing_stop_price(trail_percent)

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "name": self.name,
            "sector": self.sector,
            "entry_price": self.entry_price,
            "entry_date": self.entry_date,
            "shares": self.shares,
            "highest_price": self.highest_price,
            "current_price": self.current_price,
            "unrealized_pnl": self.unrealized_pnl(),
            "unrealized_pnl_percent": self.unrealized_pnl_percent()
        }

    @classmethod
    def from_dict(cls, data: dict):
        pos = cls(
            ticker=data["ticker"],
            entry_price=data["entry_price"],
            entry_date=data["entry_date"],
            shares=data["shares"],
            name=data.get("name", ""),
            sector=data.get("sector", "")
        )
        pos.highest_price = data.get("highest_price", pos.entry_price)
        pos.current_price = data.get("current_price", pos.entry_price)
        return pos

class Portfolio:
    """Manages the paper trading portfolio"""
    def __init__(self, initial_capital: float = 100000.0, commission_per_trade: float = 1.0,
                 alpaca_client=None, etf: str = ""):
        self.initial_capital = initial_capital
        self.cash = initial_capital
        self.commission_per_trade = commission_per_trade
        self.positions: Dict[str, Position] = {}  # ticker -> Position
        self.trade_history: List[Trade] = []
        self.daily_values: List[Dict] = []  # Track portfolio value over time
        self.alpaca_client = alpaca_client
        self.etf = etf

    def buy(self, ticker: str, price: float, date: str,
            allocation_pct: float = 0.1, name: str = "", sector: str = "",
            max_purchase_amount: Optional[float] = None,
            reason: str = "") -> Optional[Trade]:
        """
        Buy a position

        Args:
            ticker: Stock ticker
            price: Current price
            date: Date of transaction
            allocation_pct: Percentage of portfolio to allocate (0.1 = 10%)
            name: Company name
            sector: Sector name
            max_purchase_amount: Maximum dollar amount to spend on this ticker (optional cap)
            reason: Human-readable reason for the buy (e.g. flipped confirmation signal)

        Returns:
            Trade object if successful, None if insufficient funds or position exists
        """
        import logging
        log = logging.getLogger(__name__)

        # Don't buy if we already have a position
        if ticker in self.positions:
            return None

        # Calculate position size based on percentage
        target_value = self.cash * allocation_pct
        
        # Apply max purchase cap if specified
        if max_purchase_amount is not None:
            target_value = min(target_value, max_purchase_amount)
        
        shares = target_value / price
        commission = self.commission_per_trade
        total_cost = (shares * price) + commission

        # Check if we have enough cash
        if total_cost > self.cash:
            return None

        # Execute trade
        self.cash -= total_cost
        position = Position(ticker, price, date, shares, name, sector)
        self.positions[ticker] = position

        trade = Trade(ticker, OrderType.BUY, price, date, shares, commission, reason=reason)
        self.trade_history.append(trade)

        # Submit order to Alpaca if client is configured
        if self.alpaca_client and self.alpaca_client.is_available():
            try:
                alpaca_order = self.alpaca_client.submit_order(
                    etf=self.etf,
                    ticker=ticker,
                    qty=shares,
                    side="buy"
                )
                if alpaca_order:
                    log.info(f"Alpaca order submitted: BUY {shares} {ticker} (ID: {alpaca_order.id})")
            except Exception as e:
                log.error(f"Failed to submit Alpaca order for {ticker}: {e}")

        return trade

    def sell(self, ticker: str, price: float, date: str, reason: str = "") -> Optional[Trade]:
        """
        Sell a position

        Args:
            ticker: Stock ticker
            price: Current price
            date: Date of transaction
            reason: Reason for selling (signal, stop loss, etc.)

        Returns:
            Trade object if successful, None if no position
        """
        import logging
        log = logging.getLogger(__name__)

        if ticker not in self.positions:
            return None

        position = self.positions[ticker]
        proceeds = position.shares * price
        commission = self.commission_per_trade
        net_proceeds = proceeds - commission

        # Execute trade
        self.cash += net_proceeds
        del self.positions[ticker]

        trade = Trade(ticker, OrderType.SELL, price, date, position.shares, commission, reason=reason)
        self.trade_history.append(trade)

        # Submit order to Alpaca if client is configured
        if self.alpaca_client and self.alpaca_client.is_available():
            try:
                alpaca_order = self.alpaca_client.submit_order(
                    etf=self.etf,
                    ticker=ticker,
                    qty=position.shares,
                    side="sell"
                )
                if alpaca_order:
                    log.info(f"Alpaca order submitted: SELL {position.shares} {ticker} (ID: {alpaca_order.id})")
            except Exception as e:
                log.error(f"Failed to submit Alpaca order for {ticker}: {e}")

        return trade

    def update_prices(self, price_data: Dict[str, float], date: str):
        """Update current prices for all positions"""
        for ticker, position in self.positions.items():
            if ticker in price_data:
                position.update_price(price_data[ticker], date)

    def sync_alpaca_positions(self, etf: str) -> int:
        """
        Sync positions from Alpaca to local portfolio state.

        Args:
            etf: ETF symbol to filter positions

        Returns:
            Number of positions synced
        """
        import logging
        log = logging.getLogger(__name__)

        if not self.alpaca_client or not self.alpaca_client.is_available():
            log.warning("Alpaca client not available, cannot sync positions")
            return 0

        try:
            alpaca_positions = self.alpaca_client.get_positions(etf)
            synced = 0
            for pos in alpaca_positions:
                if pos.ticker not in self.positions:
                    # Add position to local state
                    self.positions[pos.ticker] = Position(
                        ticker=pos.ticker,
                        entry_price=pos.avg_entry_price,
                        entry_date="",  # Alpaca doesn't provide entry date
                        shares=pos.qty,
                        name=pos.ticker,
                        sector=""
                    )
                    self.positions[pos.ticker].current_price = pos.current_price
                    synced += 1
                    log.info(f"Synced position: {pos.ticker} ({pos.qty} shares)")
            return synced
        except Exception as e:
            log.error(f"Failed to sync Alpaca positions: {e}")
            return 0

    def check_stop_losses(self, date: str, trail_percent: float = 0.15) -> List[str]:
        """
        Check for positions that should be sold due to trailing stop loss

        Returns:
            List of tickers that were sold due to stop loss
        """
        stopped_out = []
        for ticker, position in list(self.positions.items()):
            if position.should_stop_out(trail_percent):
                # Sell at current price (or slightly below for slippage?)
                sell_price = position.current_price
                self.sell(ticker, sell_price, date, reason="stop_loss")
                stopped_out.append(ticker)
        return stopped_out

    def total_value(self) -> float:
        """Calculate total portfolio value (cash + positions)"""
        positions_value = sum(
            pos.shares * pos.current_price
            for pos in self.positions.values()
        )
        return self.cash + positions_value

    def total_return(self) -> float:
        """Calculate total return percentage"""
        if self.initial_capital == 0:
            return 0.0
        return (self.total_value() - self.initial_capital) / self.initial_capital * 100

    def get_positions_summary(self) -> List[dict]:
        """Get summary of all current positions"""
        return [pos.to_dict() for pos in self.positions.values()]

    def get_trade_history(self) -> List[dict]:
        """Get history of all trades"""
        return [trade.to_dict() for trade in self.trade_history]

    def save_state(self, filepath: Path, conn=None, etf: str = ""):
        """Save portfolio state to file and SQLite."""
        state = {
            "initial_capital": self.initial_capital,
            "cash": self.cash,
            "commission_per_trade": self.commission_per_trade,
            "positions": {ticker: pos.to_dict() for ticker, pos in self.positions.items()},
            "trade_history": [trade.to_dict() for trade in self.trade_history],
            "daily_values": self.daily_values
        }
        filepath.write_text(json.dumps(state, indent=2))

        if conn is not None:
            now = datetime.datetime.now(datetime.timezone.utc).isoformat()
            conn.execute(
                """
                INSERT INTO portfolio_state (
                    etf, initial_capital, cash, commission_per_trade,
                    positions_json, trade_history_json, daily_values_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(etf) DO UPDATE SET
                    initial_capital=excluded.initial_capital,
                    cash=excluded.cash,
                    commission_per_trade=excluded.commission_per_trade,
                    positions_json=excluded.positions_json,
                    trade_history_json=excluded.trade_history_json,
                    daily_values_json=excluded.daily_values_json,
                    updated_at=excluded.updated_at
                """,
                (
                    etf or filepath.stem,
                    self.initial_capital,
                    self.cash,
                    self.commission_per_trade,
                    json.dumps(state["positions"], indent=2),
                    json.dumps(state["trade_history"], indent=2),
                    json.dumps(state["daily_values"], indent=2),
                    now,
                ),
            )
            conn.commit()
        
        # Also generate HTML portfolio report
        html_path = filepath.with_suffix('.html')
        html_path.write_text(self.generate_portfolio_html(), encoding="utf-8")

    def generate_portfolio_html(self) -> str:
        """Generate HTML report for portfolio performance"""
        from src.reporter import ticker_link, fmt_f
        
        positions_rows = ""
        for pos in self.positions.values():
            pnl = pos.unrealized_pnl()
            pnl_pct = pos.unrealized_pnl_percent()
            pnl_class = "pos" if pnl >= 0 else "neg"
            pnl_sign = "+" if pnl >= 0 else ""
            
            positions_rows += f"""
            <tr>
                <td><b>{ticker_link(pos.ticker, pos.name)}</b></td>
                <td>{pos.entry_date}</td>
                <td>${fmt_f(pos.entry_price)}</td>
                <td>${fmt_f(pos.current_price)}</td>
                <td>{fmt_f(pos.shares, 4)}</td>
                <td>${fmt_f(pos.shares * pos.current_price)}</td>
                <td class="{pnl_class}">{pnl_sign}${fmt_f(abs(pnl))} ({pnl_sign}{fmt_f(abs(pnl_pct), 2)}%)</td>
            </tr>"""
        
        if not positions_rows:
            positions_rows = '<tr><td colspan="7" style="text-align:center;color:var(--mt)">No open positions</td></tr>'
        
        trades_rows = ""
        for trade in reversed(self.trade_history[-50:]):  # Last 50 trades
            trade_type = trade.type.value.upper()
            type_class = "pos" if trade.type == OrderType.BUY else "neg"
            reason_text = trade.reason or "—"
            trades_rows += f"""
            <tr>
                <td>{trade.date}</td>
                <td><b>{ticker_link(trade.ticker)}</b></td>
                <td><span class="{type_class}">{trade_type}</span></td>
                <td>${fmt_f(trade.price)}</td>
                <td>{fmt_f(trade.shares, 4)}</td>
                <td>${fmt_f(trade.value)}</td>
                <td>${fmt_f(trade.commission)}</td>
                <td nowrap style="max-width:340px" title="{reason_text}">{reason_text}</td>
            </tr>"""

        if not trades_rows:
            trades_rows = '<tr><td colspan="8" style="text-align:center;color:var(--mt)">No trades yet</td></tr>'
        
        total_value = self.total_value()
        total_return = self.total_return()
        return_sign = "+" if total_return >= 0 else ""
        
        css = """
        /* Purple/Lilac Theme - Flat Design */
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
        
        :root{--bg:#1a1025;--sf:#2d1f3e;--bd:#4a3b5c;--mt:#b8a9c9;--ac:#a78bfa;--ac-hover:#c4b5fd;--tx:#f5f3ff;--pos:#34d399;--neg:#f87171;--lilac-light:#ddd6fe;--purple-glow:rgba(167,139,250,0.15)}
        *{box-sizing:border-box;margin:0;padding:0}
        body{background:linear-gradient(135deg,var(--bg) 0%,#241836 100%);color:var(--tx);font-family:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:14px;line-height:1.6;min-height:100vh}
        a{color:var(--ac);text-decoration:none;transition:color 0.2s ease}
        a:hover{color:var(--ac-hover)}
        .wrap{max-width:1100px;margin:0 auto;padding:32px 20px}
        h1{font-size:1.75rem;font-weight:700;margin-bottom:8px;background:linear-gradient(135deg,var(--lilac-light) 0%,var(--ac) 100%);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
        h2{font-size:.85rem;font-weight:600;color:var(--ac);margin:28px 0 10px;border-bottom:2px solid var(--bd);padding-bottom:8px;text-transform:uppercase;letter-spacing:.08em}
        .sub{color:var(--mt);font-size:.85rem;margin-bottom:24px}
        .stats{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:24px}
        .stat{background:var(--sf);border:1px solid var(--bd);border-radius:10px;padding:14px 18px;min-width:130px;box-shadow:0 4px 12px var(--purple-glow)}
        .sv{font-size:1.5rem;font-weight:700;color:var(--ac)}
        .sl{font-size:.7rem;color:var(--mt);text-transform:uppercase;letter-spacing:.06em}
        table{width:100%;border-collapse:collapse;margin-top:12px}
        th{text-align:left;padding:10px 12px;color:var(--mt);font-size:.72rem;text-transform:uppercase;letter-spacing:.06em;border-bottom:2px solid var(--bd)}
        td{padding:8px 12px;border-bottom:1px solid #2d1f3e;vertical-align:middle;font-size:.82rem}
        tr:hover td{background:rgba(167,139,250,0.08)}
        .pos{color:var(--pos);font-weight:600}
        .neg{color:var(--neg);font-weight:600}
        footer{margin-top:48px;color:var(--mt);font-size:.75rem;border-top:1px solid var(--bd);padding-top:16px;text-align:center}
        """
        
        return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Portfolio — Paper Trading</title>
<style>{css}</style>
</head>
<body>
<div class="wrap">
<h1><span class="icon icon-portfolio"></span>Portfolio Performance</h1>
<p class="sub">Paper trading results</p>

<div class="stats">
  <div class="stat"><div class="sv">${fmt_f(total_value)}</div><div class="sl">Total Value</div></div>
  <div class="stat"><div class="sv">${fmt_f(self.cash)}</div><div class="sl">Cash</div></div>
  <div class="stat"><div class="sv">{return_sign}{fmt_f(total_return, 2)}%</div><div class="sl">Total Return</div></div>
  <div class="stat"><div class="sv">{len(self.positions)}</div><div class="sl">Positions</div></div>
  <div class="stat"><div class="sv">{len(self.trade_history)}</div><div class="sl">Trades</div></div>
</div>

<h2>Current Positions</h2>
<div style="overflow-x:auto">
<table>
<tr><th>Ticker</th><th>Entry Date</th><th>Entry Price</th><th>Current Price</th><th>Shares</th><th>Value</th><th>P&L</th></tr>
{positions_rows}
</table>
</div>

<h2>Recent Trades</h2>
<div style="overflow-x:auto">
<table>
<tr><th>Date</th><th>Ticker</th><th>Type</th><th>Price</th><th>Shares</th><th>Value</th><th>Commission</th><th>Reason</th></tr>
{trades_rows}
</table>
</div>

<footer>Velocity Paper Trading · Updated automatically</footer>
</div>
</body>
</html>"""

    @classmethod
    def load_state(cls, filepath: Path, conn=None, etf: str = ""):
        """Load portfolio state from file and SQLite."""
        if not filepath.exists() and conn is None:
            return cls()

        if not filepath.exists():
            row = conn.execute(
                "SELECT initial_capital, cash, commission_per_trade, positions_json, trade_history_json, daily_values_json FROM portfolio_state WHERE etf=?",
                (etf or filepath.stem,),
            ).fetchone()
            if row:
                return cls._state_from_row(row)
            return cls()  # Return new portfolio if no saved state

        data = json.loads(filepath.read_text())
        portfolio = cls(
            initial_capital=data.get("initial_capital", 100000.0),
            commission_per_trade=data.get("commission_per_trade", 1.0)
        )
        portfolio.cash = data.get("cash", portfolio.initial_capital)

        # Load positions
        for ticker, pos_data in data.get("positions", {}).items():
            portfolio.positions[ticker] = Position.from_dict(pos_data)

        # Load trade history
        portfolio.trade_history = [
            Trade.from_dict(trade_data)
            for trade_data in data.get("trade_history", [])
        ]

        portfolio.daily_values = data.get("daily_values", [])
        return portfolio

    @classmethod
    def _state_from_row(cls, row):
        portfolio = cls(
            initial_capital=row["initial_capital"],
            commission_per_trade=row["commission_per_trade"],
        )
        portfolio.cash = row["cash"]
        positions = json.loads(row["positions_json"] or "{}")
        for ticker, pos_data in positions.items():
            portfolio.positions[ticker] = Position.from_dict(pos_data)
        portfolio.trade_history = [
            Trade.from_dict(trade_data)
            for trade_data in json.loads(row["trade_history_json"] or "[]")
        ]
        portfolio.daily_values = json.loads(row["daily_values_json"] or "[]")
        return portfolio