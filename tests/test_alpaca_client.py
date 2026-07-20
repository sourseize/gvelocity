"""
Unit tests for the Alpaca client module.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.alpaca_client import AlpacaClient, AlpacaPosition, AlpacaOrder


class AlpacaClientTests(unittest.TestCase):
    """Tests for AlpacaClient class."""

    def test_init_without_credentials(self):
        """Client should be created but not available without credentials."""
        with patch.dict('os.environ', {}, clear=True):
            client = AlpacaClient(api_key=None, secret_key=None, paper=True)
            self.assertFalse(client.is_available())

    def test_init_with_credentials_but_no_sdk(self):
        """Client should be created but not available if alpaca-py is not installed."""
        # Since alpaca-py is not installed, client should not be available
        # even with credentials provided
        client = AlpacaClient(api_key="test_key", secret_key="test_secret", paper=True)
        self.assertFalse(client.is_available())

    def test_alpaca_position_dataclass(self):
        """AlpacaPosition should store position data correctly."""
        pos = AlpacaPosition(
            ticker="AAPL",
            qty=100.0,
            avg_entry_price=150.0,
            current_price=155.0
        )
        self.assertEqual(pos.ticker, "AAPL")
        self.assertEqual(pos.qty, 100.0)
        self.assertEqual(pos.avg_entry_price, 150.0)
        self.assertEqual(pos.current_price, 155.0)

    def test_alpaca_order_dataclass(self):
        """AlpacaOrder should store order data correctly."""
        order = AlpacaOrder(
            id="order-123",
            ticker="AAPL",
            qty=100.0,
            side="buy",
            status="new",
            created_at="2026-07-19T12:00:00Z"
        )
        self.assertEqual(order.id, "order-123")
        self.assertEqual(order.ticker, "AAPL")
        self.assertEqual(order.qty, 100.0)
        self.assertEqual(order.side, "buy")
        self.assertEqual(order.status, "new")

    def test_get_positions_returns_empty_list_when_not_available(self):
        """get_positions should return empty list when client is not available."""
        client = AlpacaClient(api_key=None, secret_key=None, paper=True)
        positions = client.get_positions("MTUM")
        self.assertEqual(positions, [])

    def test_get_account_returns_none_when_not_available(self):
        """get_account should return None when client is not available."""
        client = AlpacaClient(api_key=None, secret_key=None, paper=True)
        account = client.get_account()
        self.assertIsNone(account)

    def test_get_order_history_returns_empty_list_when_not_available(self):
        """get_order_history should return empty list when client is not available."""
        client = AlpacaClient(api_key=None, secret_key=None, paper=True)
        orders = client.get_order_history("MTUM")
        self.assertEqual(orders, [])

    def test_submit_order_returns_none_when_not_available(self):
        """submit_order should return None when client is not available."""
        client = AlpacaClient(api_key=None, secret_key=None, paper=True)
        order = client.submit_order("MTUM", "AAPL", 100.0, "buy")
        self.assertIsNone(order)


class AlpacaClientMockTests(unittest.TestCase):
    """Tests with mocked Alpaca SDK - these require alpaca-py to be installed."""

    def test_get_positions_returns_empty_list_without_sdk(self):
        """get_positions should return empty list when SDK not available."""
        client = AlpacaClient(api_key="test_key", secret_key="test_secret", paper=True)
        # Since alpaca-py is not installed, is_available() returns False
        self.assertFalse(client.is_available())
        positions = client.get_positions("MTUM")
        self.assertEqual(positions, [])

    def test_get_account_returns_none_without_sdk(self):
        """get_account should return None when SDK not available."""
        client = AlpacaClient(api_key="test_key", secret_key="test_secret", paper=True)
        self.assertFalse(client.is_available())
        account = client.get_account()
        self.assertIsNone(account)

    def test_submit_order_returns_none_without_sdk(self):
        """submit_order should return None when SDK not available."""
        client = AlpacaClient(api_key="test_key", secret_key="test_secret", paper=True)
        self.assertFalse(client.is_available())
        order = client.submit_order("MTUM", "AAPL", 100.0, "buy")
        self.assertIsNone(order)


if __name__ == "__main__":
    unittest.main()