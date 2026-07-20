"""
Alpaca API client for paper trading integration.
"""

import os
import logging
from typing import Optional, Dict, List
from dataclasses import dataclass

log = logging.getLogger(__name__)

# Alpaca API endpoints
PAPER_API_URL = "https://paper-api.alpaca.markets"
LIVE_API_URL = "https://api.alpaca.markets"


@dataclass
class AlpacaPosition:
    """Represents a position in Alpaca."""
    ticker: str
    qty: float
    avg_entry_price: float
    current_price: float


@dataclass
class AlpacaOrder:
    """Represents an order in Alpaca."""
    id: str
    ticker: str
    qty: float
    side: str
    status: str
    created_at: str


class AlpacaClient:
    """Client for interacting with Alpaca trading API."""

    def __init__(self, api_key: Optional[str] = None, secret_key: Optional[str] = None,
                 paper: bool = True):
        """
        Initialize Alpaca client.

        Args:
            api_key: Alpaca API key (defaults to ALPACA_API_KEY env var)
            secret_key: Alpaca secret key (defaults to ALPACA_SECRET_KEY env var)
            paper: Use paper trading API (default True)
        """
        self.api_key = api_key or os.getenv("ALPACA_API_KEY")
        self.secret_key = secret_key or os.getenv("ALPACA_SECRET_KEY")
        self.paper = paper
        self.base_url = PAPER_API_URL if paper else LIVE_API_URL

        if not self.api_key or not self.secret_key:
            log.warning("Alpaca API credentials not configured. Set ALPACA_API_KEY and ALPACA_SECRET_KEY environment variables.")

        # Import alpaca-py here to allow the module to load without the dependency
        try:
            from alpaca.trading.client import TradingClient
            from alpaca.trading.requests import MarketOrderRequest
            from alpaca.trading.enums import OrderSide, OrderType
            self._trading_client = TradingClient(self.api_key, self.secret_key, paper=paper)
            self._MarketOrderRequest = MarketOrderRequest
            self._OrderSide = OrderSide
            self._OrderType = OrderType
            self._available = True
        except ImportError:
            log.warning("alpaca-py not installed. Install with: pip install alpaca-py")
            self._trading_client = None
            self._available = False

    def is_available(self) -> bool:
        """Check if Alpaca client is properly configured and available."""
        return self._available and self.api_key and self.secret_key

    def get_or_create_portfolio(self, etf: str) -> Optional[str]:
        """
        Get or create an Alpaca portfolio for the given ETF.

        Note: Alpaca's Portfolio API may not be available in all account types.
        This method returns a portfolio ID or None if portfolios aren't supported.

        Args:
            etf: ETF ticker symbol (e.g., "MTUM", "QUAL", "DYNF")

        Returns:
            Portfolio ID or None
        """
        if not self.is_available():
            return None

        portfolio_name = f"velocity-{etf.lower()}"

        try:
            # Try to get existing portfolio
            from alpaca.trading.requests import PortfolioCreationRequest
            try:
                portfolios = self._trading_client.get_portfolios()
                for p in portfolios:
                    if p.name == portfolio_name:
                        return p.id
            except Exception:
                # Portfolios may not be supported in this account type
                log.info("Alpaca portfolios not available, using default account")
                return None

            # Create new portfolio
            request = PortfolioCreationRequest(name=portfolio_name)
            portfolio = self._trading_client.create_portfolio(request)
            log.info(f"Created Alpaca portfolio: {portfolio_name}")
            return portfolio.id

        except Exception as e:
            log.error(f"Failed to get/create portfolio for {etf}: {e}")
            return None

    def submit_order(self, etf: str, ticker: str, qty: float, side: str,
                    order_type: str = "market") -> Optional[AlpacaOrder]:
        """
        Submit an order to Alpaca.

        Args:
            etf: ETF symbol for portfolio context
            ticker: Stock ticker to trade
            qty: Quantity to trade
            side: "buy" or "sell"
            order_type: "market" or "limit" (default: market)

        Returns:
            AlpacaOrder if successful, None otherwise
        """
        if not self.is_available():
            log.warning("Alpaca client not available, skipping order submission")
            return None

        try:
            order_side = self._OrderSide.BUY if side.lower() == "buy" else self._OrderSide.SELL
            order_type_enum = self._OrderType.MARKET if order_type.lower() == "market" else self._OrderType.LIMIT

            request = self._MarketOrderRequest(
                symbol=ticker,
                qty=qty,
                side=order_side,
                type=order_type_enum,
            )

            order = self._trading_client.submit_order(request)
            log.info(f"Alpaca order submitted: {side.upper()} {qty} {ticker} (ID: {order.id})")

            return AlpacaOrder(
                id=order.id,
                ticker=ticker,
                qty=qty,
                side=side,
                status=order.status,
                created_at=str(order.created_at),
            )

        except Exception as e:
            log.error(f"Failed to submit Alpaca order for {ticker}: {e}")
            return None

    def get_positions(self, etf: Optional[str] = None) -> List[AlpacaPosition]:
        """
        Get current positions from Alpaca.

        Args:
            etf: Optional ETF filter (not used if portfolios not available)

        Returns:
            List of AlpacaPosition objects
        """
        if not self.is_available():
            return []

        try:
            positions = self._trading_client.get_positions()
            return [
                AlpacaPosition(
                    ticker=p.symbol,
                    qty=float(p.qty),
                    avg_entry_price=float(p.avg_entry_price),
                    current_price=float(p.current_price),
                )
                for p in positions
            ]
        except Exception as e:
            log.error(f"Failed to get Alpaca positions: {e}")
            return []

    def get_account(self) -> Optional[Dict]:
        """
        Get account information from Alpaca.

        Returns:
            Account info dict or None
        """
        if not self.is_available():
            return None

        try:
            account = self._trading_client.get_account()
            return {
                "cash": float(account.cash),
                "buying_power": float(account.buying_power),
                "portfolio_value": float(account.portfolio_value),
                "status": account.status,
            }
        except Exception as e:
            log.error(f"Failed to get Alpaca account: {e}")
            return None

    def get_order_history(self, etf: Optional[str] = None) -> List[AlpacaOrder]:
        """
        Get order history from Alpaca.

        Args:
            etf: Optional ETF filter (not used if portfolios not available)

        Returns:
            List of AlpacaOrder objects
        """
        if not self.is_available():
            return []

        try:
            orders = self._trading_client.get_orders()
            return [
                AlpacaOrder(
                    id=o.id,
                    ticker=o.symbol,
                    qty=float(o.qty),
                    side=o.side.value,
                    status=o.status.value,
                    created_at=str(o.created_at),
                )
                for o in orders
            ]
        except Exception as e:
            log.error(f"Failed to get Alpaca order history: {e}")
            return []